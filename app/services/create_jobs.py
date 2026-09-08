"""Фоновые jobs Create (Outsee / Kie): очередь ожидания + параллельный пул.

Каждый Generate → отдельный job_id.
status=queued пока ждёт слот семафора провайдера;
status=processing когда реально ушёл в API
(Outsee ≤ CREATE_MAX_PARALLEL_OUTSEE).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from app.services.generation_storage import (
    build_generation_path,
    update_sidecar,
    write_sidecar,
)
from app.settings import settings

GenerateFn = Callable[[Path], Awaitable[Any]]


@dataclass
class CreateJob:
    id: str
    media: str
    model: str
    provider: str
    path: Path
    prompt: str
    status: str = "queued"  # queued | processing | done | failed
    preview_url: str | None = None
    raw_url: str | None = None
    error: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc)
        .astimezone()
        .isoformat(timespec="seconds")
    )
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_sec: int | None = None
    history_id: str = ""
    # позиция в очереди ожидания (1-based), None если уже processing/done
    queue_position: int | None = None

    def to_dict(self) -> dict[str, Any]:
        from app.services.generation_storage import elapsed_from_iso, format_elapsed_min_sec

        live_sec = self.elapsed_sec
        if live_sec is None and self.status in {"queued", "processing"}:
            start_iso = self.created_at or self.started_at
            live_sec = elapsed_from_iso(start_iso, live=True)

        label = (
            format_elapsed_min_sec(live_sec)
            if live_sec is not None
            else None
        )
        return {
            "job_id": self.id,
            "ok": self.status != "failed",
            "status": self.status,
            "media": self.media,
            "model": self.model,
            "provider": self.provider,
            "path": str(self.path.resolve()),
            "history_id": self.history_id or f"gen-{self.path.name}",
            "preview_url": self.preview_url,
            "raw_url": self.raw_url,
            "error": self.error,
            "bytes": self.path.stat().st_size if self.path.is_file() else 0,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_sec": live_sec,
            "elapsed_label": label,
            "queue_position": self.queue_position,
            "prompt_preview": (self.prompt or "")[:120],
        }


_JOBS: dict[str, CreateJob] = {}
_LOCK = asyncio.Lock()
# Сильные ссылки на tasks.
_TASKS: set[asyncio.Task[Any]] = set()
_TASKS_BY_JOB: dict[str, asyncio.Task[Any]] = {}
# Отдельный семафор на провайдера.
_SEMS: dict[str, asyncio.Semaphore] = {}
_SEM_SIZES: dict[str, int] = {}
# Анти-даблклик.
_RECENT_FP: dict[str, tuple[str, float]] = {}
_DEDUP_WINDOW_S = 2.5

_MAX_PARALLEL_CAP = 16


def _job_fingerprint(
    *,
    media: str,
    model: str,
    provider: str,
    prompt: str,
    params: dict[str, Any] | None,
) -> str:
    p = params or {}
    values = p.get("values") if isinstance(p.get("values"), dict) else {}
    nonce = str(p.get("nonce") or p.get("batch_index") or values.get("_nonce") or "")
    return "|".join(
        [
            (provider or "").strip().lower(),
            (media or "").strip().lower(),
            (model or "").strip().lower(),
            (prompt or "").strip(),
            str(p.get("aspect") or ""),
            str(p.get("resolution") or ""),
            str(p.get("duration") or ""),
            str(bool(p.get("generate_audio"))),
            str(bool(p.get("has_first_frame"))),
            str(bool(p.get("has_last_frame"))),
            nonce,
        ]
    )


def max_parallel(provider: str | None = None) -> int:
    """Лимит одновременных Create-jobs для провайдера (outsee=5, grsai=10)."""
    p = (provider or "").strip().lower()
    fallback = int(getattr(settings, "create_max_parallel", 5) or 5)
    if p == "outsee":
        n = int(getattr(settings, "create_max_parallel_outsee", fallback) or fallback)
    else:
        n = fallback
    return max(1, min(n, _MAX_PARALLEL_CAP))


def _semaphore(provider: str) -> asyncio.Semaphore:
    """Ленивый семафор на provider; пересоздаём при смене лимита."""
    key = (provider or "outsee").strip().lower() or "outsee"
    size = max_parallel(key)
    if key not in _SEMS or _SEM_SIZES.get(key) != size:
        _SEMS[key] = asyncio.Semaphore(size)
        _SEM_SIZES[key] = size
    return _SEMS[key]


def get_job(job_id: str) -> CreateJob | None:
    return _JOBS.get(job_id)


def _refresh_queue_positions() -> None:
    """Позиции в ожидании — отдельно по каждому провайдеру."""
    by_provider: dict[str, list[CreateJob]] = {}
    for j in _JOBS.values():
        if j.status == "queued":
            by_provider.setdefault(j.provider, []).append(j)
        else:
            j.queue_position = None
    for waiting in by_provider.values():
        waiting.sort(key=lambda j: j.created_at)
        for i, j in enumerate(waiting, start=1):
            j.queue_position = i


def list_active_jobs(*, provider: str | None = None) -> list[CreateJob]:
    _refresh_queue_positions()
    out = [
        j
        for j in _JOBS.values()
        if j.status in {"queued", "processing"}
        and (provider is None or j.provider == provider)
    ]
    out.sort(key=lambda j: (0 if j.status == "processing" else 1, j.created_at))
    return out


def queue_snapshot(*, provider: str | None = None) -> dict[str, Any]:
    """Сводка для UI: running / waiting / лимит параллели."""
    active = list_active_jobs(provider=provider)
    running = [j for j in active if j.status == "processing"]
    waiting = [j for j in active if j.status == "queued"]
    return {
        "max_parallel": max_parallel(provider),
        "max_parallel_outsee": max_parallel("outsee"),
        "running_count": len(running),
        "waiting_count": len(waiting),
        "total_active": len(active),
        "running": [j.to_dict() for j in running],
        "waiting": [j.to_dict() for j in waiting],
        "jobs": [j.to_dict() for j in active],
    }


async def enqueue_generation(
    *,
    media: str,
    model: str,
    provider: str,
    prompt: str,
    ext: str,
    params: dict[str, Any] | None,
    quote: dict[str, Any] | None,
    run: GenerateFn,
) -> CreateJob:
    """Регистрирует job (queued) и стартует task — слот API берёт семафор."""
    fp = _job_fingerprint(
        media=media, model=model, provider=provider, prompt=prompt, params=params
    )
    now = datetime.now(timezone.utc).timestamp()
    async with _LOCK:
        stale = [k for k, (_, ts) in _RECENT_FP.items() if now - ts > _DEDUP_WINDOW_S]
        for k in stale:
            _RECENT_FP.pop(k, None)
        prev = _RECENT_FP.get(fp)
        if prev is not None:
            prev_id, prev_ts = prev
            if now - prev_ts <= _DEDUP_WINDOW_S:
                existing = _JOBS.get(prev_id)
                if existing is not None and existing.status in {"queued", "processing"}:
                    logger.info(
                        "create_job.dedup id={} fp={} (даблклик / повтор за {:.2f}s)",
                        existing.id,
                        fp[:80],
                        now - prev_ts,
                    )
                    return existing

    job_id = uuid.uuid4().hex[:12]
    out_path = build_generation_path(media=media, model=model, ext=ext)
    history_id = f"gen-{out_path.name}"
    write_sidecar(
        out_path,
        media=media,
        model=model,
        prompt=prompt,
        params=params,
        quote=quote,
        provider=provider,
        status="queued",
        job_id=job_id,
        require_file=False,
    )
    job = CreateJob(
        id=job_id,
        media=media,
        model=model,
        provider=provider,
        path=out_path,
        prompt=prompt,
        status="queued",
        history_id=history_id,
    )
    async with _LOCK:
        _JOBS[job_id] = job
        _RECENT_FP[fp] = (job_id, now)
        _refresh_queue_positions()

    logger.info(
        "create_job.enqueued id={} provider={} media={} model={} wait_pos={} parallel_cap={}",
        job.id,
        provider,
        media,
        model,
        job.queue_position,
        max_parallel(provider),
    )

    task = asyncio.create_task(
        _run_job(job, run=run, params=params, quote=quote),
        name=f"create-job-{job_id}",
    )
    _TASKS.add(task)
    _TASKS_BY_JOB[job_id] = task

    def _on_done(t: asyncio.Task[Any]) -> None:
        _TASKS.discard(t)
        _TASKS_BY_JOB.pop(job_id, None)

    task.add_done_callback(_on_done)
    return job


async def cancel_job(job_id: str) -> bool:
    """Отменить выполнение или ожидание задачи Create по ID, history_id или имени файла."""
    from app.services.generation_storage import (
        generations_root,
        invalidate_generation_list_cache,
        update_sidecar,
    )

    clean = job_id.removeprefix("gen-").strip()
    target_job: CreateJob | None = None
    target_jid: str | None = None

    async with _LOCK:
        for jid, j in list(_JOBS.items()):
            if (
                jid == job_id
                or jid == clean
                or j.history_id == job_id
                or j.history_id == f"gen-{clean}"
                or j.path.name == clean
                or j.path.stem == Path(clean).stem
                or str(j.path) == job_id
            ):
                target_job = j
                target_jid = jid
                break

    if target_job and target_jid:
        task = _TASKS_BY_JOB.get(target_jid)
        if task and not task.done():
            task.cancel()

        target_job.status = "failed"
        target_job.error = "Генерация остановлена пользователем"
        target_job.finished_at = (
            datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        )
        update_sidecar(
            target_job.path,
            status="failed",
            error=target_job.error,
            finished_at=target_job.finished_at,
        )
        invalidate_generation_list_cache()
        _refresh_queue_positions()
        logger.info("create_job.cancelled id={} (matched job)", target_jid)
        return True

    # Если задачи нет в памяти (например, после перезапуска сервера), ищем sidecar на диске
    root = generations_root()
    found = False
    stem = Path(clean).stem
    for pattern in (f"{stem}*.json", f"{clean}*.json"):
        for sp in root.rglob(pattern):
            if sp.is_file():
                try:
                    update_sidecar(
                        sp,
                        status="failed",
                        error="Генерация остановлена пользователем",
                        finished_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    )
                    found = True
                except Exception:  # noqa: BLE001
                    pass

    if found:
        invalidate_generation_list_cache()
        _refresh_queue_positions()
        logger.info("create_job.cancelled on disk: {}", job_id)
        return True

    return False


async def _run_job(
    job: CreateJob,
    *,
    run: GenerateFn,
    params: dict[str, Any] | None,
    quote: dict[str, Any] | None,
) -> None:
    import shutil

    from app.services.generation_storage import generations_root

    # Ждём свободный слот провайдера — пока status=queued (UI: «ожидание»)
    async with _semaphore(job.provider):
        job.status = "processing"
        job.queue_position = None
        job.started_at = (
            datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        )
        t0 = asyncio.get_running_loop().time()
        _refresh_queue_positions()
        update_sidecar(job.path, status="processing", started_at=job.started_at)
        logger.info(
            "create_job.start id={} provider={} media={} model={} (slot acquired, cap={})",
            job.id,
            job.provider,
            job.media,
            job.model,
            max_parallel(job.provider),
        )
        try:
            from app.services.api_tracker_hook import check_api_allowed
            check_api_allowed(provider=job.provider, model=job.model)

            result = await run(job.path)
            final_path = Path(getattr(result, "file_path", job.path))
            if not final_path.is_file() or final_path.stat().st_size < 32:
                raise RuntimeError(
                    f"Create job: файл не сохранён на диск ({final_path})"
                )

            root = generations_root().resolve()
            try:
                final_path.resolve().relative_to(root)
                under_gens = True
            except ValueError:
                under_gens = False

            if not under_gens:
                dest = (
                    job.path
                    if job.path.suffix == final_path.suffix
                    else job.path.with_suffix(final_path.suffix)
                )
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(final_path, dest)
                final_path = dest
            elif final_path != job.path:
                old_side = job.path.with_suffix(".json")
                if old_side.is_file() and not final_path.with_suffix(".json").is_file():
                    try:
                        old_side.replace(final_path.with_suffix(".json"))
                    except OSError:
                        pass

            job.path = final_path
            job.history_id = f"gen-{final_path.name}"
            job.raw_url = getattr(result, "raw_url", None)
            job.preview_url = f"/api/files?path={job.path.resolve()}"
            job.status = "done"
            job.finished_at = (
                datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            )
            job.elapsed_sec = max(0, int(round(asyncio.get_running_loop().time() - t0)))
            write_sidecar(
                job.path,
                media=job.media,
                model=job.model,
                prompt=job.prompt,
                params=params,
                raw_url=job.raw_url,
                quote=quote,
                provider=job.provider,
                status="done",
                job_id=job.id,
                started_at=job.started_at,
                finished_at=job.finished_at,
                elapsed_sec=job.elapsed_sec,
            )
            if not job.path.is_file() or job.path.stat().st_size < 32:
                raise RuntimeError("Create job: sidecar ok, но media-файл пропал")
            from app.services.generation_storage import format_elapsed_min_sec

            logger.info(
                "create_job.done id={} path={} bytes={} elapsed={}",
                job.id,
                job.path,
                job.path.stat().st_size,
                format_elapsed_min_sec(job.elapsed_sec),
            )
            # Запись в API Tracker
            try:
                from app.services.api_tracker_hook import record_api_call
                cost_usd = None
                if quote and isinstance(quote, dict) and "usd" in quote:
                    try:
                        cost_usd = float(quote["usd"])
                    except (ValueError, TypeError):
                        pass
                record_api_call(
                    provider=job.provider,
                    model=job.model,
                    call_type=job.media,
                    media_count=1,
                    duration_sec=float(job.elapsed_sec or 0),
                    cost_usd=cost_usd,
                    status_code=200,
                    project_source="create_workspace",
                    metadata={"prompt": (job.prompt or "")[:500], "job_id": job.id},
                )
            except Exception as e:
                logger.debug("api_tracker record failed: {}", e)
        except asyncio.CancelledError:
            job.status = "failed"
            job.error = "Генерация отменена пользователем"
            job.finished_at = (
                datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            )
            job.elapsed_sec = max(0, int(round(asyncio.get_running_loop().time() - t0)))
            update_sidecar(
                job.path,
                status="failed",
                error=job.error,
                started_at=job.started_at,
                finished_at=job.finished_at,
                elapsed_sec=job.elapsed_sec,
            )
            logger.info("create_job.cancelled_handled id={}", job.id)
            raise
        except Exception as e:  # noqa: BLE001
            job.status = "failed"
            job.error = str(getattr(e, "reason", None) or e)[:500]
            job.finished_at = (
                datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            )
            job.elapsed_sec = max(0, int(round(asyncio.get_running_loop().time() - t0)))
            update_sidecar(
                job.path,
                status="failed",
                error=job.error,
                started_at=job.started_at,
                finished_at=job.finished_at,
                elapsed_sec=job.elapsed_sec,
            )
            logger.exception("create_job.failed id={}", job.id)
            # Запись ошибки в API Tracker
            try:
                from app.services.api_tracker_hook import record_api_call
                record_api_call(
                    provider=job.provider,
                    model=job.model,
                    call_type=job.media,
                    media_count=1,
                    duration_sec=float(job.elapsed_sec or 0),
                    status_code=500,
                    error_message=str(job.error or "")[:300],
                    project_source="create_workspace",
                    metadata={"prompt": (job.prompt or "")[:500], "job_id": job.id},
                )
            except Exception:
                pass
        finally:
            _refresh_queue_positions()

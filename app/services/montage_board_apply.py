"""Применить правки панели монтажа: trim + очередь regen.

Потоки как у генерации: meta.outsee_streams + общий acquire_image_slot
(лимит слотов Outsee тот же, что у Create/img/video).

Порядок обязательный:
  1) сначала ВСЕ картинки, потом ВСЕ видео;
  2) «первый кадр» (shot1) → «второй кадр» (shot2) внутри одного frame #;
  3) разные frame # — до N параллельно (как нода img).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.project_db import project_db_session_scope, sync_project_row_to_master_db
from app.models import Project
from app.services.img_streams import acquire_image_slot, get_img_streams
from app.services.montage_board_meta import (
    add_failed_highlight,
    add_highlight,
    clear_failed_highlight,
    slot_key_from_op,
    montage_meta,
    public_board_meta,
    set_montage_meta,
    touch_applied,
)
from app.services.montage_ai_change import (
    character_ids_from_prompt,
    load_img_pr_master,
    rewrite_prompt_via_gpt,
    write_ai_change_db_card,
)
from app.services.montage_board_regen import (
    _frame_by_number,
    execute_image_regen,
    execute_video_regen,
    finalize_image_regen,
    finalize_video_regen,
    prepare_image_regen,
    prepare_video_regen,
)


ProgressCb = Callable[[int, int, dict[str, Any]], Awaitable[None]]


# Совпадает с порогами outsee-валидации — не финализируем stub/placeholder.
_READY_IMAGE_BYTES = 200_000
_READY_VIDEO_BYTES = 80_000

_IMAGE_OP_TYPES = frozenset(
    {
        "image_regen",
        "image_regen_prompt",
        "image_regen_correction",
        "image_ai_change",
    }
)
_VIDEO_OP_TYPES = frozenset(
    {"video_regen", "video_regen_prompt", "video_ai_change"}
)


def _ready_local_asset(path: Path, *, min_bytes: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= min_bytes
    except OSError:
        return False


def _is_sqlite_locked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database is busy" in msg


def _is_sqlite_transient(exc: BaseException) -> bool:
    """Locked / poisoned session после параллельного flush — можно retry."""
    msg = str(exc).lower()
    return _is_sqlite_locked(exc) or (
        "rolled back due to a previous exception" in msg
        or "invalid transaction is rolled back" in msg
        or "this session's transaction has been rolled back" in msg
    )


async def _call_with_sqlite_retry(
    factory,
    *,
    project_id: int,
    frame_number: int,
    label: str,
    attempts: int = 7,
):
    """Повторить короткую DB-операцию при SQLite lock / rollback."""
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if _is_sqlite_transient(exc) and attempt < attempts:
                wait = min(2.0 * attempt, 10.0)
                logger.warning(
                    "montage {} #{} F{} sqlite transient ({}/{}), wait {:.1f}s: {}",
                    label,
                    project_id,
                    frame_number,
                    attempt,
                    attempts,
                    wait,
                    str(exc)[:160].replace("\n", " "),
                )
                await asyncio.sleep(wait)
                continue
            raise
    assert last is not None
    raise last


def _op_frame_shot(op: dict[str, Any]) -> tuple[int, int]:
    try:
        frame = int(op.get("frame_number") or 0)
    except (TypeError, ValueError):
        frame = 0
    try:
        shot = int(op.get("shot") or 1)
    except (TypeError, ValueError):
        shot = 1
    return frame, (2 if shot == 2 else 1)


def order_montage_pending_ops(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Картинки по (frame, shot) → видео по (frame, shot). Чужие типы — в конец."""
    images: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for op in ops:
        t = str(op.get("type") or "").strip()
        if t in _IMAGE_OP_TYPES:
            images.append(op)
        elif t in _VIDEO_OP_TYPES:
            videos.append(op)
        else:
            other.append(op)
    images.sort(key=_op_frame_shot)
    videos.sort(key=_op_frame_shot)
    return images + videos + other


def group_ops_by_frame(
    ops: list[dict[str, Any]],
) -> list[tuple[int, list[dict[str, Any]]]]:
    """Кадры по возрастанию; внутри кадра ops уже отсортированы shot1→shot2."""
    by_frame: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for op in ops:
        fr, _ = _op_frame_shot(op)
        by_frame[fr].append(op)
    out: list[tuple[int, list[dict[str, Any]]]] = []
    for fr in sorted(by_frame.keys()):
        group = sorted(by_frame[fr], key=_op_frame_shot)
        out.append((fr, group))
    return out


def _montage_apply_parallel(project: Project) -> int:
    """Сколько одновременных кадров.

    meta.outsee_streams / img_streams — если заданы;
    иначе CREATE_MAX_PARALLEL_OUTSEE (как Studio Create), не IMG_MAX_STREAMS=1.
    """
    from app.services.create_jobs import max_parallel
    from app.services.img_streams import META_KEY, META_KEY_ALIAS

    meta = project.meta if isinstance(project.meta, dict) else {}
    if META_KEY_ALIAS in meta or META_KEY in meta:
        n = get_img_streams(project)
    else:
        n = max_parallel("outsee")
    if n <= 0:
        return 1
    return max(1, min(4, n))


async def _finalize_image_with_retry(
    project_id: int,
    prep: Any,
    new_path: Path,
    board: dict[str, Any],
) -> dict[str, Any]:
    last: BaseException | None = None
    for attempt in range(1, 8):
        try:
            async with project_db_session_scope(project_id) as session:
                project = await session.get(Project, project_id)
                if project is None:
                    raise RuntimeError(f"проект #{project_id} не найден")
                return await finalize_image_regen(
                    session, project, prep, new_path, board=board
                )
        except Exception as exc:  # noqa: BLE001
            last = exc
            if _is_sqlite_locked(exc) and attempt < 7:
                wait = min(2.0 * attempt, 10.0)
                logger.warning(
                    "montage finalize image #{} F{} locked ({}/7), wait {:.1f}s",
                    project_id,
                    prep.frame_number,
                    attempt,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            raise
    assert last is not None
    raise last


async def _finalize_video_with_retry(
    project_id: int,
    prep: Any,
    new_path: Path,
    board: dict[str, Any],
) -> dict[str, Any]:
    last: BaseException | None = None
    for attempt in range(1, 8):
        try:
            async with project_db_session_scope(project_id) as session:
                project = await session.get(Project, project_id)
                if project is None:
                    raise RuntimeError(f"проект #{project_id} не найден")
                return await finalize_video_regen(
                    session, project, prep, new_path, board=board
                )
        except Exception as exc:  # noqa: BLE001
            last = exc
            if _is_sqlite_locked(exc) and attempt < 7:
                wait = min(2.0 * attempt, 10.0)
                logger.warning(
                    "montage finalize video #{} F{} locked ({}/7), wait {:.1f}s",
                    project_id,
                    prep.frame_number,
                    attempt,
                    wait,
                )
                await asyncio.sleep(wait)
                continue
            raise
    assert last is not None
    raise last


async def _run_op_with_short_sessions(
    project_id: int,
    op: dict[str, Any],
    board: dict[str, Any],
) -> dict[str, Any]:
    """Чтение БД → (GPT для ИИзменение вне сессии) → Outsee → запись."""
    op_type = str(op.get("type") or "").strip()
    frame_number = int(op["frame_number"])
    shot = int(op.get("shot") or 1)

    ai_kind: str | None = None
    ai_voiceover = ""
    ai_img_pr_path = None
    ai_img_pr_variant = ""
    ai_db_card_path: Path | None = None
    ai_project: Any = None
    prep: Any = None

    async with project_db_session_scope(project_id) as session:
        project = await session.get(Project, project_id)
        if project is None:
            raise RuntimeError(f"проект #{project_id} не найден")

        if op_type in ("image_ai_change", "video_ai_change"):
            fr = await _frame_by_number(session, project.id, frame_number)
            if fr is None:
                raise RuntimeError(f"кадр {frame_number} не найден")
            ai_kind = "image" if op_type == "image_ai_change" else "video"
            ai_voiceover = fr.voiceover_text or ""
            ai_img_pr_path, ai_img_pr_variant = load_img_pr_master(project)
            ai_db_card_path = write_ai_change_db_card(
                project,
                fr,
                Path(tempfile.mkdtemp(prefix="ai_change_db_")),
            )
            ai_project = SimpleNamespace(
                id=project.id,
                meta=getattr(project, "meta", None),
            )
        elif op_type in (
            "image_regen",
            "image_regen_prompt",
            "image_regen_correction",
        ):
            mode = "same_prompt"
            if op_type == "image_regen_prompt":
                mode = "edit_prompt"
            elif op_type == "image_regen_correction":
                mode = "correction"
            pinned = str(op.get("prompt") or "").strip()
            prep = await prepare_image_regen(
                session,
                project,
                frame_number,
                shot=shot,
                mode=mode,
                new_prompt=str(op.get("prompt") or ""),
                correction=str(op.get("correction") or op.get("prompt") or ""),
                board=board,
                pinned_prompt=pinned if op_type == "image_regen" and pinned else None,
            )
        elif op_type in ("video_regen", "video_regen_prompt"):
            mode = "edit_prompt" if op_type == "video_regen_prompt" else "same_prompt"
            prep = await prepare_video_regen(
                session,
                project,
                frame_number,
                shot=shot,
                mode=mode,
                new_prompt=str(op.get("prompt") or ""),
                board=board,
            )
        else:
            raise RuntimeError(f"неизвестная операция: {op_type}")

    if ai_kind is not None:
        new_prompt = await rewrite_prompt_via_gpt(
            voiceover_text=ai_voiceover,
            kind=ai_kind,  # type: ignore[arg-type]
            project_id=project_id,
            project=ai_project,
            img_pr_path=ai_img_pr_path,
            img_pr_variant=ai_img_pr_variant,
            db_card_path=ai_db_card_path,
        )

        async def _prepare_after_gpt():
            async with project_db_session_scope(project_id) as session:
                project = await session.get(Project, project_id)
                if project is None:
                    raise RuntimeError(f"проект #{project_id} не найден")
                if ai_kind == "image":
                    return await prepare_image_regen(
                        session,
                        project,
                        frame_number,
                        shot=shot,
                        mode="edit_prompt",
                        new_prompt=new_prompt,
                        correction="",
                        board=board,
                        ref_person_ids=character_ids_from_prompt(new_prompt),
                    )
                return await prepare_video_regen(
                    session,
                    project,
                    frame_number,
                    shot=shot,
                    mode="edit_prompt",
                    new_prompt=new_prompt,
                    board=board,
                )

        prep = await _call_with_sqlite_retry(
            _prepare_after_gpt,
            project_id=project_id,
            frame_number=frame_number,
            label="prepare ai_change",
        )

    assert prep is not None

    # Слот общий с Create/img/video — не долбим Outsee сверх лимита потоков.
    async with acquire_image_slot():
        if op_type.startswith("image"):
            try:
                new_path = await execute_image_regen(prep)
            except Exception as exc:  # noqa: BLE001
                if _ready_local_asset(prep.file_path, min_bytes=_READY_IMAGE_BYTES):
                    logger.warning(
                        "montage apply #{} image frame {} shot {}: "
                        "execute failed but file ready — finalize: {}",
                        project_id,
                        frame_number,
                        shot,
                        exc,
                    )
                    new_path = prep.file_path
                else:
                    raise
            return await _finalize_image_with_retry(project_id, prep, new_path, board)

        try:
            new_path = await execute_video_regen(prep)
        except Exception as exc:  # noqa: BLE001
            if _ready_local_asset(prep.file_path, min_bytes=_READY_VIDEO_BYTES):
                logger.warning(
                    "montage apply #{} video frame {} shot {}: "
                    "execute failed but file ready — finalize: {}",
                    project_id,
                    frame_number,
                    shot,
                    exc,
                )
                new_path = prep.file_path
            else:
                raise
        return await _finalize_video_with_retry(project_id, prep, new_path, board)


async def _run_ops_phase(
    *,
    project_id: int,
    phase_indices: list[int],
    all_ops: list[dict[str, Any]],
    board: dict[str, Any],
    parallel: int,
    op_status: list[str | None],
    results: list[dict[str, Any]],
    errors: list[str],
    on_progress: ProgressCb | None,
    phase_label: str,
) -> None:
    """Одна фаза: кадры параллельно до N; внутри кадра shot1 → shot2 строго."""
    if not phase_indices:
        return

    by_frame: dict[int, list[int]] = defaultdict(list)
    for idx in phase_indices:
        fr, _ = _op_frame_shot(all_ops[idx])
        by_frame[fr].append(idx)
    for fr in by_frame:
        by_frame[fr].sort(key=lambda i: _op_frame_shot(all_ops[i]))

    frames = sorted(by_frame.keys())
    logger.info(
        "montage apply #{} phase={} frames={} ops={} parallel={}",
        project_id,
        phase_label,
        len(frames),
        len(phase_indices),
        parallel,
    )

    meta_lock = asyncio.Lock()
    total = len(all_ops)

    def _pending_snapshot() -> list[dict[str, Any]]:
        return [all_ops[i] for i, st in enumerate(op_status) if st != "ok"]

    async def _finish_op(idx: int, ok: bool, result: dict[str, Any]) -> None:
        async with meta_lock:
            op_status[idx] = "ok" if ok else "fail"
            if ok:
                results.append(result)
                highlight = result.get("highlight")
                if highlight:
                    add_highlight(board, str(highlight))
                    clear_failed_highlight(board, str(highlight))
            else:
                errors.append(str(result.get("error") or "error"))
                results.append(result)
                fail_key = slot_key_from_op(all_ops[idx])
                if not fail_key and isinstance(result.get("op"), dict):
                    fail_key = slot_key_from_op(result.get("op"))
                if fail_key:
                    add_failed_highlight(board, fail_key)
            board["pending_ops"] = _pending_snapshot()
            async with project_db_session_scope(project_id) as session:
                project = await session.get(Project, project_id)
                if project is not None:
                    set_montage_meta(project, board)
                    await session.commit()
                    try:
                        await sync_project_row_to_master_db(project)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("sync_project_row_to_master_db failed: {}", e)
            if on_progress is not None:
                await on_progress(len(results), max(total, 1), result)

    async def _frame_worker(frame: int) -> None:
        # Shot1 → shot2 строго подряд внутри кадра.
        for idx in by_frame[frame]:
            op = all_ops[idx]
            try:
                result = await _run_op_with_short_sessions(project_id, op, board)
                await _finish_op(idx, True, result)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                logger.warning(
                    "montage apply #{} op {} failed: {}",
                    project_id,
                    op,
                    msg,
                )
                await _finish_op(idx, False, {"ok": False, "error": msg, "op": op})

    sem = asyncio.Semaphore(max(1, parallel))

    async def _guarded(frame: int) -> None:
        async with sem:
            await _frame_worker(frame)

    await asyncio.gather(*(_guarded(fr) for fr in frames))


async def apply_montage_board(
    session: AsyncSession,
    project: Project,
    *,
    video_trims: dict[str, dict[str, float]] | None = None,
    pending_ops: list[dict[str, Any]] | None = None,
    on_progress: ProgressCb | None = None,
) -> dict[str, Any]:
    board = montage_meta(project)
    if video_trims is not None:
        board["video_trims"] = video_trims
    ops = order_montage_pending_ops(
        list(pending_ops or board.get("pending_ops") or [])
    )
    # Не стираем прошлые зелёные слоты — иначе после следующего apply
    # «слетают» все ранее применённые правки в UI. Чистим только failed
    # у слотов этой очереди (их снова добавят при ошибке).
    for op in ops:
        key = slot_key_from_op(op)
        if key:
            clear_failed_highlight(board, key)

    results: list[dict[str, Any]] = []
    errors: list[str] = []

    project_id = int(project.id)
    parallel = _montage_apply_parallel(project)

    image_indices = [
        i for i, o in enumerate(ops) if str(o.get("type") or "") in _IMAGE_OP_TYPES
    ]
    video_indices = [
        i for i, o in enumerate(ops) if str(o.get("type") or "") in _VIDEO_OP_TYPES
    ]
    other_indices = [
        i
        for i, o in enumerate(ops)
        if str(o.get("type") or "") not in _IMAGE_OP_TYPES
        and str(o.get("type") or "") not in _VIDEO_OP_TYPES
    ]

    logger.info(
        "montage apply #{}: {} image + {} video + {} other, parallel={}",
        project_id,
        len(image_indices),
        len(video_indices),
        len(other_indices),
        parallel,
    )

    # Commit trim/highlights до долгих Outsee — не держим write-txn.
    board["pending_ops"] = list(ops)
    set_montage_meta(project, board)
    await session.flush()
    await session.commit()
    try:
        await sync_project_row_to_master_db(project)
    except Exception as e:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", e)

    op_status: list[str | None] = [None] * len(ops)

    await _run_ops_phase(
        project_id=project_id,
        phase_indices=image_indices,
        all_ops=ops,
        board=board,
        parallel=parallel,
        op_status=op_status,
        results=results,
        errors=errors,
        on_progress=on_progress,
        phase_label="images",
    )
    await _run_ops_phase(
        project_id=project_id,
        phase_indices=video_indices,
        all_ops=ops,
        board=board,
        parallel=parallel,
        op_status=op_status,
        results=results,
        errors=errors,
        on_progress=on_progress,
        phase_label="videos",
    )
    await _run_ops_phase(
        project_id=project_id,
        phase_indices=other_indices,
        all_ops=ops,
        board=board,
        parallel=1,
        op_status=op_status,
        results=results,
        errors=errors,
        on_progress=on_progress,
        phase_label="other",
    )

    remaining = [ops[i] for i, st in enumerate(op_status) if st != "ok"]
    board["pending_ops"] = remaining
    touch_applied(board)
    set_montage_meta(project, board)
    await session.flush()
    await session.commit()
    try:
        await sync_project_row_to_master_db(project)
    except Exception as e:  # noqa: BLE001
        logger.debug("sync_project_row_to_master_db failed: {}", e)

    return {
        "ok": not errors,
        "results": results,
        "errors": errors,
        "meta": public_board_meta(board),
    }

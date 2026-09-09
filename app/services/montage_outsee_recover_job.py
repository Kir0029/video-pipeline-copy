"""Фоновое «Забрать правки из Outsee» — кнопка не ждёт синхронно минуты."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.db import session_scope
from app.project_db import project_db_session_scope, sync_project_row_to_master_db
from app.models import Project
from app.services.event_bus import publish_project_event
from app.services.montage_board_job_state import resolve_job_status
from app.services.montage_board_meta import montage_meta, set_montage_meta
from app.services.montage_outsee_recover import (
    recover_montage_images_from_outsee,
)

_JOB_KEY = "recover_outsee_job"
_recover_tasks: dict[int, asyncio.Task[None]] = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_recover_job(project: Project) -> dict[str, Any]:
    board = montage_meta(project)
    job = board.get(_JOB_KEY)
    raw = dict(job) if isinstance(job, dict) else {"status": "idle"}
    return resolve_job_status(project.id, raw, live_tasks=_recover_tasks)


def _set_job(project: Project, patch: dict[str, Any]) -> dict[str, Any]:
    board = montage_meta(project)
    job = dict(board.get(_JOB_KEY) or {})
    job.update(patch)
    board[_JOB_KEY] = job
    set_montage_meta(project, board)
    return job


async def _publish(
    project_id: int,
    status: str,
    *,
    extra: dict | None = None,
) -> None:
    payload: dict[str, Any] = {"montage_outsee_recover": True, "status": status}
    if extra:
        payload.update(extra)
    await publish_project_event(project_id, event_type="project_updated", payload=payload)


def spawn_recover_job(project_id: int) -> asyncio.Task[None]:
    prev = _recover_tasks.get(project_id)
    if prev is not None and not prev.done():
        return prev

    async def _runner() -> None:
        result: dict[str, Any] = {}
        try:
            async with project_db_session_scope(project_id) as session:
                project = await session.get(Project, project_id)
                if project is None:
                    return
                _set_job(
                    project,
                    {
                        "status": "running",
                        "error": None,
                        "started_at": _utc_now(),
                        "finished_at": None,
                        "saved_count": 0,
                        "hits_scanned": 0,
                    },
                )
                try:
                    await sync_project_row_to_master_db(project)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("sync_project_row_to_master_db failed: {}", exc)
            await _publish(project_id, "running")

            async with project_db_session_scope(project_id) as session:
                project = await session.get(Project, project_id)
                if project is None:
                    return
                board = montage_meta(project)
                pending = list(board.get("pending_ops") or [])
                image_ops = [
                    op
                    for op in pending
                    if str(op.get("type") or "").startswith("image")
                ]
                # Кнопка «Забрать» = всегда ЗАМЕНИТЬ свежими из Outsee.
                # Раньше без pending было force_replace=False → «already» и 0 файлов.
                frame_filter: set[tuple[int, int]] | None = None
                if image_ops:
                    frame_filter = set()
                    for op in image_ops:
                        try:
                            frame_filter.add(
                                (
                                    int(op["frame_number"]),
                                    int(op.get("shot") or 1),
                                )
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
                    if not frame_filter:
                        frame_filter = None
                result = await recover_montage_images_from_outsee(
                    session,
                    project,
                    frame_filter=frame_filter,
                    click_scan=True,
                    force_replace=True,
                    limit=40,
                )
                if image_ops and result.get("saved"):
                    # Снять только успешно забранные image-ops из очереди.
                    saved_keys = {
                        (int(s["frame_number"]), int(s["shot"]))
                        for s in result.get("saved") or []
                    }
                    board = montage_meta(project)
                    new_pending = []
                    for op in list(board.get("pending_ops") or []):
                        t = str(op.get("type") or "")
                        if t.startswith("image"):
                            try:
                                key = (
                                    int(op["frame_number"]),
                                    int(op.get("shot") or 1),
                                )
                            except (KeyError, TypeError, ValueError):
                                new_pending.append(op)
                                continue
                            if key in saved_keys:
                                continue
                        new_pending.append(op)
                    board["pending_ops"] = new_pending
                    set_montage_meta(project, board)
                    result["remaining_ops"] = new_pending

                errors = list(result.get("errors") or [])
                ok = bool(result.get("ok")) and not errors
                # Частичный успех: есть saved — не error, даже если errors.
                if result.get("saved_count") or result.get("saved"):
                    status = "done"
                elif ok:
                    status = "done"
                else:
                    status = "error"
                err_text = "; ".join(errors) if errors else None
                if status == "done" and not (
                    result.get("saved_count") or result.get("saved")
                ):
                    err_text = err_text or (
                        f"В истории Outsee нет подходящих карточек "
                        f"(hits={result.get('hits_scanned') or 0})"
                    )
                _set_job(
                    project,
                    {
                        "status": status,
                        "error": err_text,
                        "finished_at": _utc_now(),
                        "saved_count": int(result.get("saved_count") or 0),
                        "hits_scanned": int(result.get("hits_scanned") or 0),
                        "saved": result.get("saved") or [],
                    },
                )
                try:
                    await sync_project_row_to_master_db(project)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("sync_project_row_to_master_db failed: {}", exc)
            await _publish(
                project_id,
                status,
                extra={
                    "ok": status == "done",
                    "saved_count": result.get("saved_count"),
                    "hits_scanned": result.get("hits_scanned"),
                    "errors": result.get("errors"),
                },
            )
        except asyncio.CancelledError:
            logger.info("recover_outsee_job #{} cancelled", project_id)
            try:
                async with project_db_session_scope(project_id) as session:
                    project = await session.get(Project, project_id)
                    if project is not None:
                        _set_job(
                            project,
                            {
                                "status": "cancelled",
                                "error": "остановлено пользователем",
                                "finished_at": _utc_now(),
                            },
                        )
                        try:
                            await sync_project_row_to_master_db(project)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("sync_project_row_to_master_db failed: {}", exc)
                await _publish(project_id, "cancelled")
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("recover_outsee_job #{} failed", project_id)
            try:
                async with project_db_session_scope(project_id) as session:
                    project = await session.get(Project, project_id)
                    if project is not None:
                        _set_job(
                            project,
                            {
                                "status": "error",
                                "error": str(exc),
                                "finished_at": _utc_now(),
                            },
                        )
                        try:
                            await sync_project_row_to_master_db(project)
                        except Exception as exc2:  # noqa: BLE001
                            logger.debug("sync_project_row_to_master_db failed: {}", exc2)
                await _publish(project_id, "error", extra={"error": str(exc)})
            except Exception:  # noqa: BLE001
                pass

    task = asyncio.create_task(_runner(), name=f"montage-recover-outsee-{project_id}")
    _recover_tasks[project_id] = task

    def _done(_t: asyncio.Task[None]) -> None:
        _recover_tasks.pop(project_id, None)

    task.add_done_callback(_done)
    return task


async def cancel_recover_job(project_id: int) -> bool:
    task = _recover_tasks.get(project_id)
    if task is not None and not task.done():
        task.cancel()
        return True
    return False

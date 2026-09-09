"""Фоновый remount для кнопки «Монтаж» — HTTP не блокируется."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from app.db import session_scope
from app.project_db import project_db_session_scope, sync_project_row_to_master_db
from app.models import Project, ProjectStatus
from app.services.event_bus import publish_project_event
from app.services.montage_board_job_state import resolve_job_status
from app.services.montage_board_meta import montage_meta, set_montage_meta
from app.services.remount_video import remount_video
from app.services.step_cancel import (
    is_stop_requested,
    register_advance_task,
    unregister_advance_task,
)

_JOB_KEY = "montage_job"
_montage_tasks: dict[int, asyncio.Task[None]] = {}


def is_montage_job_live(project_id: int) -> bool:
    """Фоновый remount «Монтаж» — worker не дублирует generate_audio (SQLite lock)."""
    task = _montage_tasks.get(project_id)
    return task is not None and not task.done()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_montage_job(project: Project) -> dict[str, Any]:
    board = montage_meta(project)
    job = board.get(_JOB_KEY)
    raw = dict(job) if isinstance(job, dict) else {"status": "idle"}
    return resolve_job_status(project.id, raw, live_tasks=_montage_tasks)


def _set_job(project: Project, patch: dict[str, Any]) -> dict[str, Any]:
    board = montage_meta(project)
    job = dict(board.get(_JOB_KEY) or {})
    job.update(patch)
    board[_JOB_KEY] = job
    set_montage_meta(project, board)
    return job


async def _publish_job(project_id: int, status: str) -> None:
    await publish_project_event(
        project_id,
        event_type="project_updated",
        payload={"montage_board_montage": True, "status": status},
    )


def spawn_montage_job(project_id: int) -> asyncio.Task[None]:
    prev = _montage_tasks.get(project_id)
    if prev is not None and not prev.done():
        return prev
    task = asyncio.create_task(run_montage_job(project_id), name=f"montage-{project_id}")
    _montage_tasks[project_id] = task
    register_advance_task(project_id, task)

    def _done(t: asyncio.Task[None]) -> None:
        _montage_tasks.pop(project_id, None)
        unregister_advance_task(project_id)
        if t.cancelled():
            logger.info("montage_job #{} cancelled", project_id)

    task.add_done_callback(_done)
    return task


async def _cleanup_montage_interrupt(project_id: int) -> None:
    """Сбросить assemble/audio running после отмены remount."""
    from app.services.project_state import compute_actual_status
    from app.services.run_sync import stop_active_running_node

    async with project_db_session_scope(project_id) as session:
        project = await session.get(Project, project_id)
        if project is None:
            return
        if project.status in (ProjectStatus.generating_audio, ProjectStatus.assembling):
            project.status = await compute_actual_status(session, project)
        await stop_active_running_node(session, project)
        try:
            await sync_project_row_to_master_db(project)
        except Exception as exc:  # noqa: BLE001
            logger.debug("sync_project_row_to_master_db failed: {}", exc)


async def cancel_montage_job(project_id: int) -> bool:
    """⏹ STOP: отменить фоновый remount и сбросить статус в meta."""
    task = _montage_tasks.get(project_id)
    if task is not None and not task.done():
        task.cancel()
    try:
        async with project_db_session_scope(project_id) as session:
            project = await session.get(Project, project_id)
            if project is None:
                return False
            board = montage_meta(project)
            job = dict(board.get(_JOB_KEY) or {})
            if job.get("status") != "running":
                return task is not None
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
        await _publish_job(project_id, "cancelled")
        await _cleanup_montage_interrupt(project_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("cancel_montage_job #{}: {}", project_id, exc)
        return False


async def run_montage_job(project_id: int) -> None:
    try:
        async with project_db_session_scope(project_id) as session:
            project = await session.get(Project, project_id)
            if project is None:
                return
            if is_stop_requested(project_id):
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
                await _publish_job(project_id, "cancelled")
                return
            _set_job(
                project,
                {"status": "running", "error": None, "started_at": _utc_now(), "finished_at": None},
            )
            try:
                await sync_project_row_to_master_db(project)
            except Exception as exc:  # noqa: BLE001
                logger.debug("sync_project_row_to_master_db failed: {}", exc)
        await _publish_job(project_id, "running")

        if is_stop_requested(project_id):
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
            await _publish_job(project_id, "cancelled")
            return

        async with project_db_session_scope(project_id) as session:
            project = await session.get(Project, project_id)
            if project is None:
                return
            if is_stop_requested(project_id):
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
                await _publish_job(project_id, "cancelled")
                return
            result = await remount_video(session, project, run_assemble=True)
            if is_stop_requested(project_id):
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
                await _publish_job(project_id, "cancelled")
                return
            if result.get("error") and not result.get("done"):
                _set_job(
                    project,
                    {
                        "status": "error",
                        "error": str(result.get("error")),
                        "finished_at": _utc_now(),
                        "result": {"done": False},
                    },
                )
                try:
                    await sync_project_row_to_master_db(project)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("sync_project_row_to_master_db failed: {}", exc)
                await _publish_job(project_id, "error")
            else:
                _set_job(
                    project,
                    {
                        "status": "done",
                        "error": None,
                        "finished_at": _utc_now(),
                        "result": {
                            "done": bool(result.get("done")),
                            "final_video": result.get("final_video"),
                        },
                    },
                )
                try:
                    await sync_project_row_to_master_db(project)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("sync_project_row_to_master_db failed: {}", exc)
                await _publish_job(project_id, "done")
    except asyncio.CancelledError:
        logger.info("montage_job #{} task cancelled", project_id)
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
            await _publish_job(project_id, "cancelled")
        except Exception:  # noqa: BLE001
            pass
        await _cleanup_montage_interrupt(project_id)
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("montage_job #{} failed", project_id)
        try:
            async with project_db_session_scope(project_id) as session:
                project = await session.get(Project, project_id)
                if project is not None:
                    _set_job(
                        project,
                        {"status": "error", "error": str(exc), "finished_at": _utc_now()},
                    )
                    try:
                        await sync_project_row_to_master_db(project)
                    except Exception as exc2:  # noqa: BLE001
                        logger.debug("sync_project_row_to_master_db failed: {}", exc2)
            await _publish_job(project_id, "error")
        except Exception:  # noqa: BLE001
            pass

"""Шаг 2: общий план → закадровый текст (DB SoT через apply-ops)."""

from __future__ import annotations

from aiogram import Bot
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HITLKind, Project, ProjectStatus
from app.services import xlsx_step_runners as xsr
from app.services.hitl import send_hitl_text
from app.storage import for_project as _sheet_for_project


async def run(session: AsyncSession, project: Project, bot: Bot) -> None:
    if project.status is not ProjectStatus.scripting:
        return
    from app.services.gen_queue_run import is_user_stopped

    if is_user_stopped(project):
        logger.info("[#{}] make_script: user_stop — не запускаем GPT", project.id)
        return

    from sqlalchemy import select

    from app.models import Frame

    existing_frames = (
        await session.execute(
            select(Frame).where(Frame.project_id == project.id).order_by(Frame.number)
        )
    ).scalars().all()
    existing_vo = " ".join(
        (fr.voiceover_text or "").strip()
        for fr in existing_frames
        if (fr.voiceover_text or "").strip()
    )
    ready = existing_vo.strip() or (project.script_text or "").strip()
    if len(ready) >= 200:
        logger.warning(
            "[#{}] make_script: закадр уже есть ({} симв) — GPT не вызываем",
            project.id,
            len(ready),
        )
        if not (project.script_text or "").strip():
            project.script_text = existing_vo or ready
        project.status = ProjectStatus.script_ready
        await session.flush()
        return

    logger.info("[#{}] make_script (db-first) starting", project.id)

    result, voiceover_text = await xsr.run_script_xlsx(project)

    if len(voiceover_text) < 200:
        raise RuntimeError("GPT вернул пустой/слишком короткий закадр")

    project.script_text = voiceover_text
    await session.flush()

    from app.services import db_apply

    ops = list(result.apply_ops or []) or [
        {"target": "project", "fields": {"закадровый_текст": voiceover_text}}
    ]
    await db_apply.apply_ops(session, project, ops, export_xlsx=False)

    project.status = ProjectStatus.script_ready
    await session.flush()

    try:
        _sheet_for_project(project).write_general(
            status=project.status.value,
            script_text=voiceover_text,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[#{}] project_sheet status write failed: {}", project.id, e)

    try:
        from app.services.node_xlsx_snapshot import snapshot_and_bind_node_xlsx

        await snapshot_and_bind_node_xlsx(session, project, node_type="script")
    except Exception as e:  # noqa: BLE001
        logger.warning("[#{}] script xlsx snapshot bind failed: {}", project.id, e)

    try:
        from app.services.storage_step_sync import sync_storage_after_step

        await sync_storage_after_step(
            session, project, "script", log_prefix="make_script"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[#{}] make_script: sync downstream storage failed: {}",
            project.id,
            e,
        )

    await send_hitl_text(
        bot,
        session,
        project,
        kind=HITLKind.approve_script,
        title=f"Закадровый текст #{project.id}",
        text=voiceover_text,
        payload={"step": "script"},
    )

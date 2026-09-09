"""Шаг 1: тема → общий план (DB SoT через apply-ops).

Excel (`project.xlsx`) — только экспорт после записи в базу.
"""

from __future__ import annotations

from aiogram import Bot
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import HITLKind, Project, ProjectStatus
from app.services import xlsx_step_runners as xsr
from app.services.hitl import send_hitl_text
from app.storage import for_project as _sheet_for_project


async def run(session: AsyncSession, project: Project, bot: Bot) -> None:
    if project.status is not ProjectStatus.planning:
        return
    logger.info(
        "[#{}] make_plan (db-first, {}) starting: '{}'",
        project.id,
        xsr.XLSX_STEP_RUNNERS_ID,
        project.topic,
    )

    result = await xsr.run_plan_xlsx(project)
    plan_text = (result.plan_text or "").strip()
    if not plan_text:
        raise RuntimeError("make_plan: пустой общий_план после GPT")

    project.general_plan = plan_text
    await session.flush()

    from app.services import db_apply

    await db_apply.apply_ops(
        session,
        project,
        [{"target": "project", "fields": {"общий_план": plan_text}}],
        export_xlsx=False,
    )

    try:
        _sheet_for_project(project).write_general(
            topic=project.topic,
            slug=project.slug,
            hero_mode=project.hero_mode,
            status=ProjectStatus.plan_ready.value,
            general_plan=plan_text,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[#{}] project_sheet plan write failed: {}", project.id, e)

    try:
        from app.services.node_xlsx_snapshot import snapshot_and_bind_node_xlsx

        await snapshot_and_bind_node_xlsx(session, project, node_type="plan")
    except Exception as e:  # noqa: BLE001
        logger.warning("[#{}] plan xlsx snapshot bind failed: {}", project.id, e)

    try:
        from app.services.storage_step_sync import sync_storage_after_step

        await sync_storage_after_step(
            session, project, "plan", log_prefix="make_plan"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[#{}] make_plan: sync downstream storage failed: {}", project.id, e)

    project.status = ProjectStatus.plan_ready
    await session.flush()
    await session.commit()
    from app.services.agent_harness import harness_gate_or_raise

    await harness_gate_or_raise(session, project, step="plan")
    await session.commit()

    req = await send_hitl_text(
        bot,
        session,
        project,
        kind=HITLKind.approve_plan,
        title=f"Сценарий ролика #{project.id}",
        text=plan_text,
        payload={"step": "plan"},
    )
    logger.info("[#{}] plan HITL={} отправлен", project.id, req.id)

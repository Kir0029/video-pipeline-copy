"""Запуск advance_project в отдельной asyncio-task — снимается через ⏹."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiogram import Bot
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.models import Project, ProjectStatus
from app.orchestrator.pipeline import advance_project
from app.project_db import project_db_session_scope
from app.services.run_sync import complete_active_node_for_step


@dataclass(frozen=True)
class AdvanceJobResult:
    project_id: int
    prev_status: str
    new_status: str | None  # None если статус не изменился


async def advance_project_job(project_id: int, bot: Bot) -> AdvanceJobResult:
    """Один такт advance_project в своей сессии изолированной проектной БД."""
    try:
        # 1. Проверяем состояние в master (state.db): статус, meta и текстовые поля
        master_status = None
        master_meta = None
        master_general_plan = None
        master_script_text = None
        master_hero_desc = None
        try:
            async with session_scope() as master_sess:
                m_proj = await master_sess.get(Project, project_id)
                if m_proj:
                    master_status = m_proj.status
                    if isinstance(m_proj.meta, dict):
                        master_meta = dict(m_proj.meta)
                    master_general_plan = m_proj.general_plan
                    master_script_text = m_proj.script_text
                    master_hero_desc = m_proj.hero_description
        except Exception as exc:  # noqa: BLE001
            logger.debug("advance_project_job: check master state error: {}", exc)

        async with project_db_session_scope(project_id) as session:
            project = await session.get(Project, project_id)
            if project is None:
                # Если ещё не в project.db, проверим master
                async with session_scope() as master_sess:
                    project = await master_sess.get(Project, project_id)
                    if project is None:
                        logger.warning("advance_project_job: проект #{} не найден", project_id)
                        return AdvanceJobResult(project_id, "", None)
                    from app.project_db import init_project_db

                    await init_project_db(project.data_dir, project=project)
                project = await session.get(Project, project_id)
                if project is None:
                    logger.warning("advance_project_job: повторное открытие #{} не удалось", project_id)
                    return AdvanceJobResult(project_id, "", None)

            # Синхронизируем статус и мета из master, если мастер был переведён в running (например, через веб-UI)
            if master_status is not None and project.status != master_status:
                logger.info(
                    "advance_project_job: синхронизация статуса #{} project.db {} -> master {}",
                    project_id,
                    project.status.value,
                    master_status.value,
                )
                project.status = master_status
                if master_meta:
                    project.meta = master_meta
                if master_general_plan:
                    project.general_plan = master_general_plan
                if master_script_text:
                    project.script_text = master_script_text
                if master_hero_desc:
                    project.hero_description = master_hero_desc
                await session.flush()

            prev = project.status.value
            prev_status = project.status
            await advance_project(session, project, bot)
            new = project.status.value
            if new != prev:
                # Завершаем ноду в изолированной базе проекта
                await complete_active_node_for_step(
                    session,
                    project,
                    prev_status=prev_status,
                    new_status=project.status,
                )
                # Синхронизируем статус, данные и NodeRun в master state.db
                try:
                    async with session_scope() as master_sess:
                        m_proj = await master_sess.get(Project, project_id)
                        if m_proj:
                            m_proj.status = project.status
                            if project.general_plan:
                                m_proj.general_plan = project.general_plan
                            if project.script_text:
                                m_proj.script_text = project.script_text
                            if project.hero_description:
                                m_proj.hero_description = project.hero_description
                            if isinstance(project.meta, dict):
                                m_proj.meta = dict(project.meta)
                        await complete_active_node_for_step(
                            master_sess,
                            m_proj or project,
                            prev_status=prev_status,
                            new_status=project.status,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("advance_project_job: sync status to master failed: {}", exc)

                logger.debug(
                    "advance_project_job: #{} {} -> {}", project_id, prev, new
                )
                return AdvanceJobResult(project_id, prev, new)
            return AdvanceJobResult(project_id, prev, None)
    except asyncio.CancelledError:
        logger.info("advance_project_job: #{} hard-cancelled (⏹)", project_id)
        try:
            async with project_db_session_scope(project_id) as session:
                project = await session.get(Project, project_id)
                if project is not None:
                    await session.refresh(project)
        except Exception:  # noqa: BLE001
            logger.warning("advance_project_job: refresh #{} after cancel failed", project_id)
        raise

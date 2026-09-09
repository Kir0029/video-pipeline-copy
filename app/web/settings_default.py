"""Дефолтный шаблон Workflow — сид на старте сервера."""

from __future__ import annotations

from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import session_scope
from app.models import Workflow
from app.orchestrator.default_graph import LAYOUT_VERSION, default_graph as _default_graph
from app.services.excel_gpt_node import (
    assign_slot_indices,
    is_legacy_enrich_label,
    migrate_enrich_nodes,
)


def default_workflow_needs_refresh(wf: Workflow) -> bool:
    """True если дефолтный граф устарел или в нём нет обязательных нод."""
    meta = dict(wf.meta or {})
    if meta.get("layout_version") != LAYOUT_VERSION:
        return True
    types = {str(n.get("type") or "") for n in (wf.nodes or [])}
    if "topic" not in types:
        return True
    # Миграция enrich_1..5 → excel_gpt (layout 5 → 6+).
    if any(t.startswith("enrich_") for t in types):
        return True
    if "excel_gpt" not in types and any("enrich" in t for t in types):
        return True
    for n in wf.nodes or []:
        typ = str(n.get("type") or "")
        if typ.startswith("enrich_") or typ == "excel_gpt":
            data = n.get("data") if isinstance(n.get("data"), dict) else {}
            if is_legacy_enrich_label(str(data.get("label") or "")):
                return True
    return False


async def apply_default_graph(session: AsyncSession, wf: Workflow) -> bool:
    """Подменить nodes/edges дефолтного workflow на фабричный граф.

    Возвращает True если были изменения.
    """
    if not default_workflow_needs_refresh(wf):
        return False

    from app.services.workflow_run_sync import sync_runs_from_workflow

    nodes, edges = _default_graph()
    wf.nodes = nodes
    wf.edges = edges
    meta = dict(wf.meta or {})
    meta["layout_version"] = LAYOUT_VERSION
    wf.meta = meta
    wf.version = (wf.version or 1) + 1
    wf.updated_at = datetime.utcnow()
    await sync_runs_from_workflow(session, wf)
    logger.info(
        "default workflow #{} refreshed to layout_version={} ({} nodes)",
        wf.id,
        LAYOUT_VERSION,
        len(nodes),
    )
    return True


async def migrate_workflow_enrich_nodes(session: AsyncSession, wf: Workflow) -> bool:
    """Мигрирует enrich_* → excel_gpt и перезаписывает устаревшие подписи."""
    nodes = list(wf.nodes or [])
    if not nodes:
        return False
    has_enrich = any(str(n.get("type") or "").startswith("enrich_") for n in nodes)
    has_legacy_labels = any(
        is_legacy_enrich_label(
            str((n.get("data") or {}).get("label") or "")
        )
        for n in nodes
        if str(n.get("type") or "") == "excel_gpt"
    )
    if not has_enrich and not has_legacy_labels:
        return False

    from app.services.workflow_run_sync import sync_runs_from_workflow

    wf.nodes = assign_slot_indices(migrate_enrich_nodes(nodes))
    wf.version = (wf.version or 1) + 1
    wf.updated_at = datetime.utcnow()
    await sync_runs_from_workflow(session, wf)
    logger.info(
        "workflow #{} migrated enrich/excel_gpt labels ({} nodes)",
        wf.id,
        len(wf.nodes or []),
    )
    return True


async def seed_default_workflow(session: AsyncSession | None = None) -> None:
    """Создаёт или обновляет системный default Workflow."""
    nodes, edges = _default_graph()

    async def _run_seed(s: AsyncSession) -> None:
        existing = (
            await s.execute(
                select(Workflow).where(Workflow.is_default == True)  # noqa: E712
            )
        ).scalar_one_or_none()

        if existing is None:
            wf = Workflow(
                name="Стандартный shorts-pipeline",
                description=(
                    "Полный 60–75 сек ролик: тема → план → сценарий → разбивка → "
                    "герои/предметы → enrich → image_prompts → images → "
                    "анимация → видео → аудио → сборка → публикация."
                ),
                nodes=nodes,
                edges=edges,
                is_default=True,
                version=1,
                meta={"layout_version": LAYOUT_VERSION},
            )
            s.add(wf)
            await s.flush()
        else:
            await apply_default_graph(s, existing)

        all_workflows = (await s.execute(select(Workflow))).scalars().all()
        for wf in all_workflows:
            if wf.is_default:
                continue
            await migrate_workflow_enrich_nodes(s, wf)

    if session is not None:
        await _run_seed(session)
    else:
        async with session_scope() as s:
            await _run_seed(s)

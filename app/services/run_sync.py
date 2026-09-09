"""Синхронизатор NodeRun ↔ WorkflowRun для веб-UI.

NodeRun.status — единственный источник правды (см. node_status_machine.py).
run_sync НЕ повышает статусы из Project.status; только:
  - skipped для отключённых нод;
  - агрегация WorkflowRun.status;
  - reconcile зависших running/queued без живой задачи.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import session_scope
from app.models import (
    NodeRun,
    NodeRunStatus,
    Project,
    ProjectStatus,
    Workflow,
    WorkflowRun,
    WorkflowRunStatus,
)
from app.services.event_bus import publish_node_event
from app.services.node_status_machine import (
    complete_node,
    fail_node,
    heal_failed_node_done,
    mark_node_skipped,
    queue_node_for_start,
    reset_node_to_pending,
    start_node_running,
)

_STALE_NODE_RUN_ERROR = "прервано: рабочий процесс не активен"
_STALE_GRACE_SEC = 30.0


async def _get_default_workflow_id(
    session: AsyncSession | None = None,
) -> int | None:
    """Id default Workflow. Optional session — для тестов/вызовов с уже открытой сессией.

    Самовосстанавливающийся поиск:
    1. Ищет Workflow с is_default == True.
    2. Если не найден — берет любой существующий Workflow и делает его дефолтным.
    3. Если воркфлоу нет вообще — авто-засевает дефолтный шаблон через seed_default_workflow().
    """

    async def _lookup(s: AsyncSession) -> int | None:
        try:
            wf = (
                await s.execute(
                    select(Workflow)
                    .where(Workflow.is_default == True)  # noqa: E712
                    .order_by(Workflow.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if wf is not None:
                return wf.id

            # Фолбэк 1: есть ли любой воркфлоу в БД
            any_wf = (
                await s.execute(
                    select(Workflow).order_by(Workflow.id.asc()).limit(1)
                )
            ).scalar_one_or_none()
            if any_wf is not None:
                any_wf.is_default = True
                await s.flush()
                return any_wf.id
        except Exception:  # noqa: BLE001 — пустая/битая БД в тестах
            logger.debug("default workflow lookup failed", exc_info=True)
            return None
        return None

    if session is not None:
        res = await _lookup(session)
        if res is not None:
            return res

    async with session_scope() as s:
        res = await _lookup(s)
    if res is not None:
        return res

    # Фолбэк 2: база пуста, вызываем авто-засев
    try:
        from app.web.settings_default import seed_default_workflow

        await seed_default_workflow(session)
    except Exception:  # noqa: BLE001
        logger.warning("auto seed_default_workflow failed", exc_info=True)

    if session is not None:
        res = await _lookup(session)
        if res is not None:
            return res

    async with session_scope() as s:
        return await _lookup(s)


async def ensure_run_for_project(
    project_id: int,
    workflow_id: int,
    session: AsyncSession | None = None,
) -> int:
    """Гарантирует, что у проекта есть WorkflowRun. Возвращает его id.

    ``session`` — опционально: использовать уже открытую сессию (тесты / API
    с dependency override), иначе ``session_scope`` глобальной БД.
    """

    async def _ensure(s: AsyncSession) -> int:
        existing = (
            await s.execute(
                select(WorkflowRun).where(WorkflowRun.project_id == project_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing.id
        wf = await s.get(Workflow, workflow_id)
        if wf is None:
            raise ValueError(f"workflow {workflow_id} not found")
        project = await s.get(Project, project_id)
        nodes = list(wf.nodes or [])
        edges = list(wf.edges or [])
        if project is not None:
            from app.services.canvas_graph import canvas_graph_from_meta

            cg = canvas_graph_from_meta(
                project.meta if isinstance(project.meta, dict) else {}
            )
            if cg:
                nodes = list(cg["nodes"])
                edges = list(cg["edges"])
        run = WorkflowRun(
            workflow_id=wf.id,
            project_id=project_id,
            status=WorkflowRunStatus.new,
            nodes_snapshot=nodes,
            edges_snapshot=edges,
        )
        s.add(run)
        await s.flush()
        for node in run.nodes_snapshot:
            nr = NodeRun(
                workflow_run_id=run.id,
                node_key=node["id"],
                node_type=effective_node_type(node),
            )
            s.add(nr)
        await s.flush()
        return run.id

    if session is not None:
        return await _ensure(session)
    async with session_scope() as s:
        return await _ensure(s)


from app.orchestrator.node_registry import (
    LINEAR_NODE_TYPES,
    NODE_TYPE_TO_READY,
    READY_TO_NODE_TYPE,
    RUNNING_TO_NODE_TYPE,
    STEP_CODE_TO_NODE_TYPE,
)
from app.services.disabled_nodes import disabled_node_types
from app.services.excel_gpt_node import (
    EXCEL_GPT_NODE_TYPE,
    active_excel_gpt_node_key,
    clear_slot_completion_meta,
    completed_node_keys,
    effective_node_type,
    resolve_excel_gpt_node_key_for_slot,
    slot_for_excel_gpt_node_key,
    slot_from_ready_status,
    slot_from_running_status,
)


def _canvas_node_type_for_running(status: ProjectStatus) -> str | None:
    """Тип ноды на канвасе для running ProjectStatus (excel_gpt, не enrich_N)."""
    if slot_from_running_status(status) is not None:
        return EXCEL_GPT_NODE_TYPE
    return RUNNING_TO_NODE_TYPE.get(status)


def _canvas_node_type_for_ready(status: ProjectStatus) -> str | None:
    if slot_from_ready_status(status) is not None:
        return EXCEL_GPT_NODE_TYPE
    return READY_TO_NODE_TYPE.get(status)


def _sd_agent_name_from_node_key(node_key: str) -> str | None:
    """Имя агента из ключа ``n_excel_gpt_sd_cd_skeleton`` / ``…_sd_asm``."""
    from app.services.scene_design.agents import ALL_AGENTS, ASSEMBLER

    key = (node_key or "").strip().lower()
    if not key:
        return None
    if key.endswith("_sd_asm") or key.endswith("sd_asm"):
        return ASSEMBLER
    for name in ALL_AGENTS:
        if key.endswith("_" + name) or key.endswith(name):
            return name
    return None


def _effective_type_from_nr(nr: NodeRun) -> str:
    """Оркестраторный тип без snapshot: excel_gpt + ключ sd_* → sd_agent/sd_assemble.

    На канвасе веер лежит как «Работа с GPT» (node_type=excel_gpt в NodeRun
    после старых sync / ручного create). Ключ ``n_excel_gpt_sd_*`` — канон
    node_groups; check-ноды ``*_sd_check_*`` не трогаем.
    """
    if nr.node_type in ("sd_agent", "sd_assemble", "scene_design"):
        return nr.node_type
    key = nr.node_key or ""
    if nr.node_type == EXCEL_GPT_NODE_TYPE and "_sd_" in key:
        if key.endswith("_sd_asm") or key.endswith("sd_asm"):
            return "sd_assemble"
        if "sd_check" not in key:
            return "sd_agent"
    return nr.node_type


def _nr_effective_type(run: WorkflowRun, nr: NodeRun) -> str:
    """Тип ноды для оркестратора: snapshot marker побеждает raw NodeRun.node_type."""
    for n in run.nodes_snapshot or []:
        if str(n.get("id") or "") == (nr.node_key or ""):
            return effective_node_type(n)
    return _effective_type_from_nr(nr)


async def _workflow_run_with_nodes(
    session: AsyncSession, project_id: int
) -> WorkflowRun | None:
    return (
        await session.execute(
            select(WorkflowRun)
            .where(WorkflowRun.project_id == project_id)
            .options(selectinload(WorkflowRun.node_runs))
        )
    ).scalar_one_or_none()


def _aggregate_workflow_run_status(
    run: WorkflowRun, project: Project | None = None
) -> None:
    """Обновить WorkflowRun.status по NodeRun-ам.

    Если Project уже в terminal success (assembled/published), не держим
    Run в «ошибка» из‑за stale failed/pending на боковых excel_gpt-нодах.
    """
    now = datetime.utcnow()
    any_running = False
    any_pending = False
    any_failed = False
    for nr in run.node_runs:
        if nr.status in (NodeRunStatus.running, NodeRunStatus.queued):
            any_running = True
        elif nr.status == NodeRunStatus.pending:
            any_pending = True
        elif nr.status == NodeRunStatus.failed:
            any_failed = True

    terminal_ok = project is not None and project.status in (
        ProjectStatus.assembled,
        ProjectStatus.publishing,
        ProjectStatus.published,
    )
    if terminal_ok:
        if any_running:
            run.status = WorkflowRunStatus.running
            if run.started_at is None:
                run.started_at = now
        else:
            run.status = WorkflowRunStatus.done
            if run.finished_at is None:
                run.finished_at = now
        return

    if any_failed:
        run.status = WorkflowRunStatus.failed
    elif any_running:
        run.status = WorkflowRunStatus.running
        if run.started_at is None:
            run.started_at = now
    elif any_pending:
        run.status = WorkflowRunStatus.waiting_hitl
    else:
        run.status = WorkflowRunStatus.done
        if run.finished_at is None:
            run.finished_at = now


async def sync_run_for_project(
    project_id: int, session: AsyncSession | None = None
) -> None:
    """Синхронизировать skipped/disabled и агрегировать WorkflowRun (без повышения статусов).

    ``session`` — опционально (тесты / API override); иначе глобальный session_scope.
    """

    async def _sync(s: AsyncSession) -> None:
        project = await s.get(Project, project_id)
        if project is None:
            return
        run = (
            await s.execute(
                select(WorkflowRun)
                .where(WorkflowRun.project_id == project_id)
                .options(selectinload(WorkflowRun.node_runs))
            )
        ).scalar_one_or_none()
        if run is None:
            return

        # Repair: NodeRun.node_type=excel_gpt при маркере sd_* → sd_agent/sd_assemble.
        # Иначе complete/reconcile не находят веер и UI горит «ошибка».
        for nr in run.node_runs:
            want = _nr_effective_type(run, nr)
            if want != nr.node_type:
                logger.info(
                    "[#{}] repair NodeRun type {}/{} → {}",
                    project_id,
                    nr.node_type,
                    nr.node_key,
                    want,
                )
                nr.node_type = want

        disabled = disabled_node_types(project)
        for nr in run.node_runs:
            if nr.node_type in disabled:
                if nr.status == NodeRunStatus.pending:
                    old = nr.status
                    if mark_node_skipped(nr, project_id=project_id):
                        await publish_node_event(
                            run.id,
                            event_type="node_status_changed",
                            node_key=nr.node_key,
                            payload={
                                "node_type": nr.node_type,
                                "from": old.value,
                                "to": nr.status.value,
                                "project_id": project_id,
                            },
                        )
            elif nr.status == NodeRunStatus.skipped and nr.node_type not in disabled:
                old = nr.status
                if reset_node_to_pending(nr, project_id=project_id, initiator="api_reset"):
                    await publish_node_event(
                        run.id,
                        event_type="node_status_changed",
                        node_key=nr.node_key,
                        payload={
                            "node_type": nr.node_type,
                            "from": old.value,
                            "to": nr.status.value,
                            "project_id": project_id,
                        },
                    )

        # Heal: excel_gpt в completed_keys, но NodeRun ещё running/queued —
        # (auto-chain раньше помечал next done и оставлял prev running).
        # pending + completed_keys = stale meta → снимаем «готово», не прыгаем в done.
        active_key = active_excel_gpt_node_key(project)
        done_keys = completed_node_keys(project)
        for nr in run.node_runs:
            if nr.node_type != EXCEL_GPT_NODE_TYPE:
                continue
            if nr.node_key not in done_keys:
                continue
            if active_key and nr.node_key == active_key:
                continue
            if nr.status == NodeRunStatus.done:
                continue
            slot = slot_for_excel_gpt_node_key(project, nr.node_key)
            if nr.status == NodeRunStatus.pending:
                if slot is not None:
                    await clear_slot_completion_meta(
                        s, project, slot, node_key=nr.node_key
                    )
                    logger.info(
                        "[#{}] heal: stale completed_key {} (slot {}) — "
                        "meta сброшен, NodeRun pending (не прыгаем в done)",
                        project_id,
                        nr.node_key,
                        slot,
                    )
                continue
            if nr.status in (
                NodeRunStatus.running,
                NodeRunStatus.queued,
                NodeRunStatus.waiting_hitl,
            ):
                old = nr.status.value
                if complete_node(nr, project_id=project_id, initiator="worker"):
                    await publish_node_event(
                        run.id,
                        event_type="node_status_changed",
                        node_key=nr.node_key,
                        payload={
                            "node_type": nr.node_type,
                            "from": old,
                            "to": nr.status.value,
                            "project_id": project_id,
                        },
                    )
                    logger.info(
                        "[#{}] heal NodeRun {} → done (excel_gpt_completed_keys, was {})",
                        project_id,
                        nr.node_key,
                        old,
                    )

        # Heal: failed NodeRun, но Project уже доказывает успех шага
        # (после WA / гонки reconcile → UI «Run: ошибка» при assembled).
        for nr in run.node_runs:
            if nr.status != NodeRunStatus.failed:
                continue
            if not _node_already_succeeded_for_project(project, nr):
                continue
            old = nr.status.value
            if heal_failed_node_done(nr, project_id=project_id):
                await publish_node_event(
                    run.id,
                    event_type="node_status_changed",
                    node_key=nr.node_key,
                    payload={
                        "node_type": nr.node_type,
                        "from": old,
                        "to": nr.status.value,
                        "project_id": project_id,
                    },
                )
                logger.info(
                    "[#{}] heal NodeRun {}/{} failed → done (project already past step)",
                    project_id,
                    nr.node_type,
                    nr.node_key,
                )

        # UI truth: project в scene_assembling, а sd_asm ещё pending/failed —
        # поднять в running (assemble идёт, prepare раньше не нашёл excel_gpt-typed).
        if project.status is ProjectStatus.scene_assembling:
            for nr in run.node_runs:
                if _nr_effective_type(run, nr) != "sd_assemble":
                    continue
                if nr.status == NodeRunStatus.done:
                    continue
                if nr.status == NodeRunStatus.failed:
                    reset_node_to_pending(
                        nr, project_id=project_id, initiator="auto_unstick"
                    )
                if nr.status == NodeRunStatus.pending:
                    queue_node_for_start(nr, project_id=project_id, initiator="api")
                    start_node_running(nr, project_id=project_id, initiator="api")
                    logger.info(
                        "[#{}] NodeRun {} → running (scene_assembling UI sync)",
                        project_id,
                        nr.node_key,
                    )
                elif nr.status == NodeRunStatus.queued:
                    start_node_running(nr, project_id=project_id, initiator="api")

        _aggregate_workflow_run_status(run, project)

    if session is not None:
        await _sync(session)
        return
    async with session_scope() as s:
        await _sync(s)


async def sync_all_active_projects() -> None:
    """Прогон по всем проектам, у которых статус не `new` и не финальный."""
    default_wf = await _get_default_workflow_id()
    if default_wf is None:
        return
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Project.id, Project.status).where(
                    Project.status.notin_([ProjectStatus.new, ProjectStatus.paused])
                )
            )
        ).all()
    for pid, _ in rows:
        try:
            await ensure_run_for_project(pid, default_wf)
            await sync_run_for_project(pid)
        except Exception:  # noqa: BLE001
            logger.exception("run_sync failed for project {}", pid)


async def background_sync_loop(*, interval_sec: float = 2.5) -> None:
    """Фоновая задача: агрегация WorkflowRun (без повышения NodeRun из project.status)."""
    while True:
        try:
            await sync_all_active_projects()
        except Exception:  # noqa: BLE001
            logger.exception("background_sync_loop iteration failed")
        await asyncio.sleep(interval_sec)


async def resolve_node_run_for_step(
    session: AsyncSession,
    project: Project,
    step_code: str,
    *,
    node_key: str | None = None,
    enrich_slot: int | None = None,
) -> NodeRun | None:
    """Найти NodeRun для шага (по node_key или node_type).

    Для excel_gpt при нескольких нодах нужен node_key / active key / enrich_slot.
    """
    run = await _workflow_run_with_nodes(session, project.id)
    if run is None:
        return None
    key = (node_key or "").strip() or None
    if step_code == "excel_gpt" and not key:
        key = active_excel_gpt_node_key(project)
    if step_code == "excel_gpt" and not key and enrich_slot is not None:
        key = resolve_excel_gpt_node_key_for_slot(project, enrich_slot)
    # Per-agent шаги веера scene_design: нода по маркеру data.sd_agent.
    from app.orchestrator.node_registry import SD_AGENT_STEP_CODES
    from app.services.excel_gpt_node import effective_node_type, sd_agent_marker

    sd_agent_name = SD_AGENT_STEP_CODES.get(step_code)
    if sd_agent_name and not key:
        for n in run.nodes_snapshot or []:
            if effective_node_type(n) != "sd_agent":
                continue
            if sd_agent_marker(n) == sd_agent_name:
                key = str(n.get("id") or "") or None
                break
    if key:
        for nr in run.node_runs:
            if nr.node_key == key:
                return nr
    node_type = STEP_CODE_TO_NODE_TYPE.get(step_code)
    if node_type is None:
        return None
    matches = [
        nr for nr in run.node_runs if _nr_effective_type(run, nr) == node_type
    ]
    if not matches and node_type in ("sd_agent", "sd_assemble"):
        # Legacy-канвас: одна нода scene_design вместо веера sd_*.
        matches = [
            nr
            for nr in run.node_runs
            if _nr_effective_type(run, nr) == "scene_design"
        ]
    if len(matches) == 1:
        return matches[0]
    if key:
        for nr in matches:
            if nr.node_key == key:
                return nr
    # Несколько excel_gpt — без явного node_key не угадываем первую.
    if step_code == "excel_gpt" and len(matches) > 1:
        return None
    return matches[0] if matches else None


async def prepare_node_for_step_start(
    session: AsyncSession,
    project: Project,
    step_code: str,
    *,
    node_key: str | None = None,
    enrich_slot: int | None = None,
    strict: bool = False,
    explicit_ui_start: bool = False,
) -> bool:
    """Подготовить ноду к запуску шага: queued → running."""
    from app.services.canvas_graph import sync_run_snapshot_from_canvas_graph
    from app.services.step_cancel import is_generation_active

    default_wf = await _get_default_workflow_id(session)
    if default_wf is None:
        if strict:
            raise ValueError("workflow по умолчанию не найден — сохраните workflow")
        logger.debug(
            "[#{}] prepare_node_for_step_start: нет default workflow, FSM пропущен",
            project.id,
        )
        return False
    # ensure_run пишет в свою session_scope — только если default workflow известен.
    try:
        await ensure_run_for_project(project.id, default_wf)
    except Exception:  # noqa: BLE001
        if strict:
            raise
        logger.debug(
            "[#{}] prepare_node_for_step_start: ensure_run_for_project failed",
            project.id,
            exc_info=True,
        )
        return False
    # Canvas мог вырасти после create_run — дописать недостающие NodeRun.
    try:
        await sync_run_snapshot_from_canvas_graph(session, project)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[#{}] prepare_node_for_step_start: canvas sync failed",
            project.id,
            exc_info=True,
        )
    resolved_key = (node_key or "").strip() or None
    if step_code == "excel_gpt" and not resolved_key and enrich_slot is not None:
        resolved_key = resolve_excel_gpt_node_key_for_slot(project, enrich_slot)
    # advance_project готовит NodeRun без node_key — подтянуть only_agent с ▶ ноды.
    if step_code == "scene_d" and not resolved_key:
        try:
            from app.services.scene_design import runner as sd_runner

            only = sd_runner.get_only_agent(project)
            if only:
                resolved_key = sd_runner.resolve_sd_node_key(project, only)
                if resolved_key:
                    logger.info(
                        "[#{}] prepare_node: scene_d only_agent={} → {}",
                        project.id,
                        only,
                        resolved_key,
                    )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[#{}] prepare_node: only_agent resolve failed",
                project.id,
                exc_info=True,
            )
    nr = await resolve_node_run_for_step(
        session,
        project,
        step_code,
        node_key=resolved_key,
        enrich_slot=enrich_slot,
    )
    if nr is None:
        if strict:
            raise ValueError(
                f"нода для шага «{step_code}» не найдена в WorkflowRun — создайте Run на канвасе"
            )
        logger.debug(
            "[#{}] prepare_node_for_step_start: NodeRun для {} не найден"
            + (f" (slot={enrich_slot})" if enrich_slot is not None else ""),
            project.id,
            step_code,
        )
        return False
    if nr.status == NodeRunStatus.skipped:
        if explicit_ui_start:
            # Ручной старт: включаем ноду обратно и продолжаем.
            meta = dict(project.meta or {})
            disabled = [str(x) for x in (meta.get("disabled_nodes") or [])]
            if nr.node_key in disabled:
                meta["disabled_nodes"] = [k for k in disabled if k != nr.node_key]
                project.meta = meta
            reset_node_to_pending(nr, project_id=project.id, initiator="ui_restart")
            logger.info(
                "[#{}] prepare_node: skipped → pending (ui re-enable {})",
                project.id,
                nr.node_key,
            )
        else:
            msg = f"нода «{nr.node_type}» отключена в графе"
            if strict:
                raise ValueError(msg)
            logger.debug("[#{}] {}", project.id, msg)
            return False

    if nr.status in (NodeRunStatus.done, NodeRunStatus.waiting_hitl):
        if explicit_ui_start:
            reset_node_to_pending(nr, project_id=project.id, initiator="ui_restart")
        else:
            msg = (
                f"нода «{nr.node_type}» уже в статусе «{nr.status.value}» — "
                "явный перезапуск только из UI"
            )
            if strict:
                raise ValueError(msg)
            logger.debug("[#{}] {}", project.id, msg)
            return False

    if nr.status in (NodeRunStatus.running, NodeRunStatus.queued):
        if is_generation_active(project.id) and not explicit_ui_start:
            msg = (
                f"нода «{nr.node_type}» уже в работе ({nr.status.value}) — "
                "дождитесь или «Сбросить шаг»"
            )
            if strict:
                raise ValueError(msg)
            return True
        reset_node_to_pending(nr, project_id=project.id, initiator="auto_unstick")
        logger.info(
            "[#{}] prepare_node_for_step_start: auto_unstick {}/{}",
            project.id,
            nr.node_type,
            nr.node_key,
        )

    if not queue_node_for_start(nr, project_id=project.id, initiator="api"):
        pass
    if not start_node_running(nr, project_id=project.id, initiator="api"):
        if strict:
            raise ValueError(
                f"нода «{nr.node_type}» не перешла в «выполняется» "
                f"(текущий статус: {nr.status.value})"
            )
        return False

    # Точечный ▶ (meta.scene_design.only_agent): НЕ поднимать весь веер.
    # advance_project зовёт prepare повторно без node_key — иначе UI снова
    # зажигает все sd_agent как running, хотя GPT идёт только у одной ноды.
    only_agent: str | None = None
    if step_code == "scene_d":
        try:
            from app.services.scene_design import runner as sd_runner

            only_agent = sd_runner.get_only_agent(project)
        except Exception:  # noqa: BLE001
            only_agent = None

    if only_agent and step_code == "scene_d":
        run_scope = await _workflow_run_with_nodes(session, project.id)
        if run_scope is not None:
            for other in run_scope.node_runs:
                if (
                    _nr_effective_type(run_scope, other) != "sd_agent"
                    or other.node_key == nr.node_key
                ):
                    continue
                if other.status in (
                    NodeRunStatus.running,
                    NodeRunStatus.queued,
                    NodeRunStatus.waiting_hitl,
                ):
                    reset_node_to_pending(
                        other,
                        project_id=project.id,
                        initiator="only_agent_scope",
                    )
        await session.flush()
    # Веер scene_design: полный scene_d (без only_agent) гоняет все sd_agent.
    elif (
        step_code == "scene_d"
        and not resolved_key
        and _effective_type_from_nr(nr) == "sd_agent"
    ):
        run_fan = await _workflow_run_with_nodes(session, project.id)
        if run_fan is not None:
            for other in run_fan.node_runs:
                if (
                    _nr_effective_type(run_fan, other) != "sd_agent"
                    or other.node_key == nr.node_key
                ):
                    continue
                if other.status == NodeRunStatus.skipped:
                    continue
                if other.status in (NodeRunStatus.done, NodeRunStatus.waiting_hitl):
                    if not explicit_ui_start:
                        continue
                    reset_node_to_pending(
                        other, project_id=project.id, initiator="ui_restart"
                    )
                if other.status in (NodeRunStatus.running, NodeRunStatus.queued):
                    reset_node_to_pending(
                        other, project_id=project.id, initiator="auto_unstick"
                    )
                queue_node_for_start(other, project_id=project.id, initiator="api")
                start_node_running(other, project_id=project.id, initiator="api")
    await session.flush()
    run = await _workflow_run_with_nodes(session, project.id)
    if run is not None:
        await publish_node_event(
            run.id,
            event_type="node_status_changed",
            node_key=nr.node_key,
            payload={
                "node_type": nr.node_type,
                "from": "pending",
                "to": nr.status.value,
                "project_id": project.id,
            },
        )
    return True


async def complete_excel_gpt_node_by_key(
    session: AsyncSession,
    project: Project,
    node_key: str | None,
    *,
    enrich_slot: int | None = None,
) -> bool:
    """Пометить excel_gpt NodeRun done ДО auto-chain на следующий слот.

    Вызывать из enrich_xlsx после успешного скачивания/sync xlsx, пока
    active_key ещё не переключён на next — иначе UI оставляет prev «в работе».
    """
    from app.services.canvas_graph import sync_run_snapshot_from_canvas_graph
    from app.services.excel_gpt_node import resolve_excel_gpt_node_key_for_slot

    key = (node_key or "").strip() or None
    if not key and enrich_slot is not None:
        key = resolve_excel_gpt_node_key_for_slot(project, enrich_slot)
    if not key:
        return False
    try:
        await sync_run_snapshot_from_canvas_graph(session, project)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[#{}] complete_excel_gpt_node: canvas sync failed",
            project.id,
            exc_info=True,
        )
    run = await _workflow_run_with_nodes(session, project.id)
    if run is None:
        return False
    for nr in run.node_runs:
        if nr.node_type != EXCEL_GPT_NODE_TYPE or nr.node_key != key:
            continue
        if nr.status == NodeRunStatus.done:
            return True
        if nr.status == NodeRunStatus.pending:
            queue_node_for_start(nr, project_id=project.id, initiator="worker")
            start_node_running(nr, project_id=project.id, initiator="worker")
        if nr.status in (
            NodeRunStatus.running,
            NodeRunStatus.queued,
            NodeRunStatus.waiting_hitl,
            NodeRunStatus.pending,
        ):
            old = nr.status.value
            if complete_node(nr, project_id=project.id, initiator="worker"):
                await session.flush()
                await publish_node_event(
                    run.id,
                    event_type="node_status_changed",
                    node_key=nr.node_key,
                    payload={
                        "node_type": nr.node_type,
                        "from": old,
                        "to": nr.status.value,
                        "project_id": project.id,
                    },
                )
                logger.info(
                    "[#{}] excel_gpt NodeRun {} → done (slot complete before chain)",
                    project.id,
                    key,
                )
                return True
        return nr.status == NodeRunStatus.done
    return False


async def complete_active_node_for_step(
    session: AsyncSession,
    project: Project,
    *,
    prev_status: ProjectStatus,
    new_status: ProjectStatus,
) -> None:
    """running → done для ноды после успешного шага воркера.

    Для excel_gpt: active_excel_gpt_node_key часто уже pop'нут в enrich_xlsx
    до этого вызова — резолвим ноду по слоту из prev_status (enriching_N).
    Если prepare не сработал (pending), поднимаем pending→running→done
    (шаг на стороне Project уже успешен).
    """
    from app.services.canvas_graph import sync_run_snapshot_from_canvas_graph

    # Точечный ▶ scene_d (only_agent) откатывает Project в frames_ready —
    # READY_TO_NODE_TYPE[frames_ready]=split. Нельзя брать ready-маппинг:
    # иначе скелет остаётся running, only_agent не чистится, reconcile → failed.
    if prev_status in (
        ProjectStatus.scene_designing,
        ProjectStatus.scene_assembling,
    ):
        node_type = _canvas_node_type_for_running(prev_status)
        if node_type is None:
            node_type = _canvas_node_type_for_ready(new_status)
    else:
        node_type = _canvas_node_type_for_ready(new_status)
        if node_type is None:
            node_type = _canvas_node_type_for_running(prev_status)
    if node_type is None:
        return
    try:
        await sync_run_snapshot_from_canvas_graph(session, project)
    except Exception:  # noqa: BLE001
        logger.debug(
            "[#{}] complete_active_node: canvas sync failed",
            project.id,
            exc_info=True,
        )
    run = await _workflow_run_with_nodes(session, project.id)
    if run is None:
        return

    # Legacy-канвас: нод sd_agent/sd_assemble нет — закрываем scene_design.
    if (
        node_type in ("sd_agent", "sd_assemble")
        and not any(_nr_effective_type(run, nr) == node_type for nr in run.node_runs)
        and any(
            _nr_effective_type(run, nr) == "scene_design" for nr in run.node_runs
        )
    ):
        node_type = "scene_design"

    # Веер scene_design: полный scene_d закрывает все sd_agent разом.
    # Точечный ▶ (meta.scene_design.only_agent) — только одну ноду.
    only_agent: str | None = None
    if node_type == "sd_agent":
        try:
            from app.services.scene_design import runner as sd_runner

            only_agent = sd_runner.get_only_agent(project)
        except Exception:  # noqa: BLE001
            only_agent = None
    complete_all = node_type == "sd_agent" and not only_agent

    # excel_gpt auto-chain УЖЕ ставит active_excel_gpt_node_key на СЛЕДУЮЩУЮ
    # ноду до вызова complete (enriching_N → enriching_N+1). Брать active_key
    # первым = пометить next done и оставить prev running — UI врёт, xlsx
    # следующего слота «применён» без работы. Всегда слот из prev_status.
    finished_key: str | None = None
    if only_agent and node_type == "sd_agent":
        try:
            from app.services.scene_design import runner as sd_runner

            finished_key = sd_runner.resolve_sd_node_key(project, only_agent)
        except Exception:  # noqa: BLE001
            finished_key = None
    if node_type == EXCEL_GPT_NODE_TYPE:
        slot = slot_from_running_status(prev_status)
        if slot is None:
            slot = slot_from_ready_status(new_status)
        if slot is not None:
            finished_key = resolve_excel_gpt_node_key_for_slot(project, slot)
        if not finished_key:
            finished_key = active_excel_gpt_node_key(project)
            if finished_key:
                logger.warning(
                    "[#{}] complete_active_node: excel_gpt fallback active_key={} "
                    "(prev={} → {}) — слот prev не резолвится",
                    project.id,
                    finished_key,
                    prev_status.value,
                    new_status.value,
                )

    excel_matches = [
        nr for nr in run.node_runs if nr.node_type == EXCEL_GPT_NODE_TYPE
    ]
    # Несколько excel_gpt без ключа — не трогаем «первую попавшуюся».
    if (
        node_type == EXCEL_GPT_NODE_TYPE
        and not finished_key
        and len(excel_matches) > 1
    ):
        logger.warning(
            "[#{}] complete_active_node: excel_gpt multi-node без ключа "
            "(prev={} → {}), NodeRun не помечен done",
            project.id,
            prev_status.value,
            new_status.value,
        )
        return

    def _clear_sd_only_agent() -> None:
        if not only_agent:
            return
        try:
            from app.services.scene_design import runner as sd_runner

            sd_runner.clear_only_agent(project)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[#{}] clear only_agent failed", project.id, exc_info=True
            )

    # Точечный ▶: чужие sd_agent, ошибочно поднятые в running, вернуть в pending.
    if only_agent and finished_key and node_type == "sd_agent":
        for other in run.node_runs:
            if _nr_effective_type(run, other) != "sd_agent":
                continue
            if other.node_key == finished_key:
                continue
            if other.status in (NodeRunStatus.running, NodeRunStatus.queued):
                reset_node_to_pending(
                    other, project_id=project.id, initiator="only_agent_scope"
                )
        await session.flush()

    for nr in run.node_runs:
        if _nr_effective_type(run, nr) != node_type:
            continue
        if finished_key and nr.node_key != finished_key:
            continue
        if nr.status in (
            NodeRunStatus.running,
            NodeRunStatus.queued,
            NodeRunStatus.waiting_hitl,
        ):
            if complete_node(nr, project_id=project.id, initiator="worker"):
                await session.flush()
                await publish_node_event(
                    run.id,
                    event_type="node_status_changed",
                    node_key=nr.node_key,
                    payload={
                        "node_type": nr.node_type,
                        "from": "running",
                        "to": nr.status.value,
                        "project_id": project.id,
                    },
                )
                logger.info(
                    "[#{}] NodeRun {} → done (finished slot, prev={} → {})",
                    project.id,
                    nr.node_key,
                    prev_status.value,
                    new_status.value,
                )
            if not complete_all:
                _clear_sd_only_agent()
                return
            continue
        # Recovery: шаг успешен, но prepare не нашёл ноду (осталась pending /
        # failed после ложного background_reconcile на excel_gpt-typed sd_*).
        if nr.status not in (NodeRunStatus.pending, NodeRunStatus.failed):
            continue
        if node_type == EXCEL_GPT_NODE_TYPE and not finished_key:
            continue
        if node_type not in (EXCEL_GPT_NODE_TYPE, "sd_agent", "sd_assemble"):
            continue
        if nr.status == NodeRunStatus.failed:
            if heal_failed_node_done(nr, project_id=project.id):
                await session.flush()
                await publish_node_event(
                    run.id,
                    event_type="node_status_changed",
                    node_key=nr.node_key,
                    payload={
                        "node_type": nr.node_type,
                        "from": "failed",
                        "to": nr.status.value,
                        "project_id": project.id,
                    },
                )
                logger.info(
                    "[#{}] NodeRun {} → done (heal failed after success, prev={})",
                    project.id,
                    nr.node_key,
                    prev_status.value,
                )
            if not complete_all:
                _clear_sd_only_agent()
                return
            continue
        queue_node_for_start(nr, project_id=project.id, initiator="worker")
        start_node_running(nr, project_id=project.id, initiator="worker")
        if complete_node(nr, project_id=project.id, initiator="worker"):
            await session.flush()
            await publish_node_event(
                run.id,
                event_type="node_status_changed",
                node_key=nr.node_key,
                payload={
                    "node_type": nr.node_type,
                    "from": "pending",
                    "to": nr.status.value,
                    "project_id": project.id,
                },
            )
            logger.info(
                "[#{}] NodeRun {} → done (recovery pending→done, prev={})",
                project.id,
                nr.node_key,
                prev_status.value,
            )
        if not complete_all:
            _clear_sd_only_agent()
            return
    _clear_sd_only_agent()


async def mark_running_node_failed(
    session: AsyncSession,
    project: Project,
    error: str | Exception,
    *,
    initiator: str = "worker",
    error_code: str | None = None,
) -> None:
    """running → failed для активной ноды проекта.

    `error` — строка или Exception (тогда через describe_error).
    """
    code = error_code
    if isinstance(error, Exception):
        from app.services.error_catalog import describe_error

        code, msg = describe_error(error)
        error_text = msg
    else:
        error_text = str(error)
    run = await _workflow_run_with_nodes(session, project.id)
    if run is None:
        return
    node_type = _canvas_node_type_for_running(project.status)
    if not node_type:
        return
    # Legacy-канвас: нод sd_agent/sd_assemble нет — фейлим scene_design.
    if (
        node_type in ("sd_agent", "sd_assemble")
        and not any(_nr_effective_type(run, nr) == node_type for nr in run.node_runs)
        and any(
            _nr_effective_type(run, nr) == "scene_design" for nr in run.node_runs
        )
    ):
        node_type = "scene_design"
    # Веер scene_design: фейлим все running-ноды агентов, не только первую.
    # Точечный ▶ — только целевую ноду.
    only_agent_fail: str | None = None
    if node_type == "sd_agent":
        try:
            from app.services.scene_design import runner as sd_runner

            only_agent_fail = sd_runner.get_only_agent(project)
        except Exception:  # noqa: BLE001
            only_agent_fail = None
    fail_all = node_type == "sd_agent" and not only_agent_fail
    active_key = (
        active_excel_gpt_node_key(project) if node_type == EXCEL_GPT_NODE_TYPE else None
    )
    if only_agent_fail and node_type == "sd_agent":
        try:
            from app.services.scene_design import runner as sd_runner

            active_key = sd_runner.resolve_sd_node_key(project, only_agent_fail)
        except Exception:  # noqa: BLE001
            pass
    if node_type == EXCEL_GPT_NODE_TYPE and not active_key:
        slot = slot_from_running_status(project.status)
        if slot is not None:
            active_key = resolve_excel_gpt_node_key_for_slot(project, slot)
    for nr in run.node_runs:
        if _nr_effective_type(run, nr) != node_type:
            continue
        if active_key and nr.node_key != active_key:
            continue
        if nr.status in (
            NodeRunStatus.running,
            NodeRunStatus.queued,
            NodeRunStatus.waiting_hitl,
        ):
            fail_node(nr, error_text, project_id=project.id, initiator=initiator)
            await session.flush()
            payload: dict = {
                "node_type": nr.node_type,
                "from": "running",
                "to": nr.status.value,
                "project_id": project.id,
                "error": error_text[:500],
            }
            if code:
                payload["error_code"] = code
            await publish_node_event(
                run.id,
                event_type="node_status_changed",
                node_key=nr.node_key,
                payload=payload,
            )
            if not fail_all:
                return


async def update_active_node_progress_text(
    session: AsyncSession,
    project: Project,
    progress_text: str | None,
) -> None:
    """Обновить progress_text активной ноды (видно в UI при running)."""
    run = await _workflow_run_with_nodes(session, project.id)
    if run is None:
        return
    node_type = _canvas_node_type_for_running(project.status)
    active_key = (
        active_excel_gpt_node_key(project) if node_type == EXCEL_GPT_NODE_TYPE else None
    )
    if node_type == EXCEL_GPT_NODE_TYPE and not active_key:
        slot = slot_from_running_status(project.status)
        if slot is not None:
            active_key = resolve_excel_gpt_node_key_for_slot(project, slot)
    for nr in run.node_runs:
        if node_type and _nr_effective_type(run, nr) != node_type:
            continue
        if active_key and nr.node_key != active_key:
            continue
        if nr.status in (NodeRunStatus.running, NodeRunStatus.queued):
            nr.progress_text = (progress_text or None)
            if nr.progress_text:
                nr.progress_text = nr.progress_text[:200]
            await session.flush()
            return


async def reset_nodes_from_step(
    session: AsyncSession,
    project_id: int,
    step_code: str,
) -> None:
    """Сбросить ноды начиная с step_code → pending (явный reset)."""
    run = await _workflow_run_with_nodes(session, project_id)
    if run is None:
        return
    # Per-agent сброс веера scene_design: только нода этого агента
    # (по маркеру data.sd_agent из snapshot'а) + sd_assemble и downstream.
    # Соседние агенты не трогаем — их чекпоинты валидны.
    from app.orchestrator.node_registry import SD_AGENT_STEP_CODES
    from app.services.excel_gpt_node import effective_node_type, sd_agent_marker

    sd_agent_name = SD_AGENT_STEP_CODES.get(step_code)
    if sd_agent_name is not None:
        target_key: str | None = None
        for n in run.nodes_snapshot or []:
            if effective_node_type(n) != "sd_agent":
                continue
            if sd_agent_marker(n) == sd_agent_name:
                target_key = str(n.get("id") or "") or None
                break
        try:
            asm_idx = NODE_TYPE_ORDER.index("sd_assemble")
        except ValueError:
            asm_idx = len(NODE_TYPE_ORDER)
        for nr in run.node_runs:
            if nr.node_key == target_key:
                reset_node_to_pending(nr, project_id=project_id, initiator="api_reset")
                continue
            if nr.node_type in NODE_TYPE_ORDER:
                idx = NODE_TYPE_ORDER.index(nr.node_type)
                if idx >= asm_idx and nr.node_type != "sd_agent":
                    reset_node_to_pending(
                        nr, project_id=project_id, initiator="api_reset"
                    )
        await session.flush()
        return
    node_type = STEP_CODE_TO_NODE_TYPE.get(step_code)
    if step_code == "excel_gpt":
        node_type = EXCEL_GPT_NODE_TYPE
    if node_type is None:
        return
    try:
        start_idx = NODE_TYPE_ORDER.index(node_type)
    except ValueError:
        start_idx = 0
    try:
        enrich_idx = NODE_TYPE_ORDER.index("enrich_1")
    except ValueError:
        enrich_idx = start_idx
    # excel_gpt на канвасе вместо enrich_* — сбрасываем вместе с ранними шагами.
    reset_excel = step_code == "excel_gpt" or start_idx <= enrich_idx
    for nr in run.node_runs:
        if nr.node_type in NODE_TYPE_ORDER:
            idx = NODE_TYPE_ORDER.index(nr.node_type)
            if idx >= start_idx:
                reset_node_to_pending(nr, project_id=project_id, initiator="api_reset")
        elif nr.node_type == EXCEL_GPT_NODE_TYPE and reset_excel:
            reset_node_to_pending(nr, project_id=project_id, initiator="api_reset")
    await session.flush()


async def stop_active_running_node(
    session: AsyncSession,
    project: Project,
) -> None:
    """При ⏹ STOP: running/queued → pending (явное действие пользователя)."""
    run = await _workflow_run_with_nodes(session, project.id)
    if run is None:
        return

    async def _reset_and_notify(nr: NodeRun) -> None:
        old = nr.status
        if not reset_node_to_pending(nr, project_id=project.id, initiator="api_stop"):
            return
        await publish_node_event(
            run.id,
            event_type="node_status_changed",
            node_key=nr.node_key,
            payload={
                "node_type": nr.node_type,
                "from": old.value,
                "to": nr.status.value,
                "project_id": project.id,
            },
        )

    node_type = _canvas_node_type_for_running(project.status)
    if not node_type:
        for nr in run.node_runs:
            if nr.status in (NodeRunStatus.running, NodeRunStatus.queued):
                await _reset_and_notify(nr)
        await session.flush()
        return
    active_key = (
        active_excel_gpt_node_key(project) if node_type == EXCEL_GPT_NODE_TYPE else None
    )
    for nr in run.node_runs:
        if nr.node_type != node_type:
            continue
        if active_key and nr.node_key != active_key:
            continue
        if nr.status in (
            NodeRunStatus.running,
            NodeRunStatus.queued,
        ):
            await _reset_and_notify(nr)
            await session.flush()
            return


# Линейный порядок типов нод (legacy — reset_nodes_from_step, тесты).
NODE_TYPE_ORDER: list[str] = [
    "topic",
    "plan",
    "script",
    "split",
    "scene_design",
    "sd_agent",
    "sd_assemble",
    "hero",
    "items",
    "enrich_1",
    "enrich_2",
    "enrich_3",
    "enrich_4",
    "enrich_5",
    "image_prompts",
    "images",
    "animation_prompts",
    "videos",
    "audio",
    "music",
    "assemble",
    "publish",
]


def _node_already_succeeded_for_project(project: Project, nr: NodeRun) -> bool:
    """True если Project.status уже говорит, что шаг ноды успешно завершён.

    Защита от гонки: complete_node ещё не закоммитен / не виден другой сессии,
    а background_reconcile уже видит running без live-task и готов вызвать fail_node.
    """
    if project.status in (
        ProjectStatus.assembled,
        ProjectStatus.publishing,
        ProjectStatus.published,
    ):
        return True
    # Overflow excel_gpt: слот общий (enrich_1_ready), успех — только свой ключ.
    # Иначе reconcile красит сценариста/промты/QC в «процесс не активен»
    # после успешной записи, пока worker-сессия ещё не commit.
    key = (nr.node_key or "").strip()
    if key and key in completed_node_keys(project):
        return True
    if _effective_type_from_nr(nr) == EXCEL_GPT_NODE_TYPE:
        from app.services.excel_gpt_node import (
            slot_for_excel_gpt_node_key,
            slot_from_ready_status,
            slot_from_running_status,
        )

        node_slot = slot_for_excel_gpt_node_key(project, key) if key else None
        ready_slot = slot_from_ready_status(project.status)
        running_slot = slot_from_running_status(project.status)
        if node_slot is not None and node_slot >= 1:
            if ready_slot is not None and node_slot <= ready_slot:
                return True
            if running_slot is not None and node_slot < running_slot:
                return True
    eff = _effective_type_from_nr(nr)
    # Веер scene_design: агенты done при scene_agents_ready / assembling / дальше.
    if eff == "sd_agent" and project.status in (
        ProjectStatus.scene_agents_ready,
        ProjectStatus.scene_assembling,
        ProjectStatus.scene_design_ready,
    ):
        return True
    # only_agent ▶ → frames_ready: агент в meta уже done, NodeRun мог застрять.
    if eff == "sd_agent" and project.status == ProjectStatus.frames_ready:
        agent = _sd_agent_name_from_node_key(nr.node_key or "")
        sd = (project.meta or {}).get("scene_design")
        agents = sd.get("agents") if isinstance(sd, dict) else None
        if (
            agent
            and isinstance(agents, dict)
            and isinstance(agents.get(agent), dict)
            and str(agents[agent].get("status") or "") == "done"
        ):
            return True
    # split: «проект дальше / frames_ready» НЕ значит разбивка готова.
    # Иначе после ▶ split поверх живого enrich reconcile красит n_split
    # в done без GPT (split_completed=false).
    if eff == "split":
        meta = project.meta if isinstance(project.meta, dict) else {}
        return bool(meta.get("split_completed"))
    if READY_TO_NODE_TYPE.get(project.status) == eff:
        return True
    if eff not in LINEAR_NODE_TYPES:
        return False
    nr_i = LINEAR_NODE_TYPES.index(eff)
    cur_type = READY_TO_NODE_TYPE.get(project.status) or RUNNING_TO_NODE_TYPE.get(
        project.status
    )
    if cur_type in LINEAR_NODE_TYPES and LINEAR_NODE_TYPES.index(cur_type) > nr_i:
        return True
    # ready-статус этой ноды уже пройден (project на следующем ready/running)
    ready = NODE_TYPE_TO_READY.get(eff)
    if ready is not None and project.status == ready:
        return True
    return False


async def _reconcile_stale_node_runs(
    *,
    initiator: str,
    require_no_live_task: bool = False,
    grace_sec: float = _STALE_GRACE_SEC,
) -> int:
    """NodeRun running/queued без живого воркера → failed (или heal → done)."""
    from app.services.step_cancel import is_generation_active

    fixed = 0
    healed = 0
    now = datetime.utcnow()
    grace = timedelta(seconds=grace_sec)
    async with session_scope() as session:
        runs = (
            await session.execute(
                select(WorkflowRun).options(selectinload(WorkflowRun.node_runs))
            )
        ).scalars().all()
        for run in runs:
            if run.project_id is None:
                continue
            project = await session.get(Project, run.project_id)
            if project is None:
                continue
            live = is_generation_active(run.project_id)
            for nr in run.node_runs:
                # Ложный failed после успеха: sd_* веер + линейные media-ноды
                # (img/anim_pr/video…), когда Project уже доказывает ready/дальше.
                _heal_types = (
                    "sd_agent",
                    "sd_assemble",
                    "excel_gpt",
                    "image_prompts",
                    "images",
                    "animation_prompts",
                    "videos",
                    "audio",
                    "music",
                    "assemble",
                )
                if (
                    nr.status == NodeRunStatus.failed
                    and _effective_type_from_nr(nr) in _heal_types
                    and _node_already_succeeded_for_project(project, nr)
                ):
                    if heal_failed_node_done(nr, project_id=run.project_id):
                        healed += 1
                        if (
                            project.status == ProjectStatus.frames_ready
                            and _effective_type_from_nr(nr) == "sd_agent"
                        ):
                            try:
                                from app.services.scene_design import (
                                    runner as sd_runner,
                                )

                                sd_runner.clear_only_agent(project)
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "[#{}] heal clear only_agent failed",
                                    run.project_id,
                                    exc_info=True,
                                )
                        logger.info(
                            "[#{}] NodeRun {}/{}: failed → done (heal after success, {})",
                            run.project_id,
                            nr.node_type,
                            nr.node_key,
                            initiator,
                        )
                    continue
                if nr.status not in (NodeRunStatus.running, NodeRunStatus.queued):
                    continue
                if require_no_live_task:
                    if live:
                        continue
                    if nr.started_at is not None and now - nr.started_at < grace:
                        continue
                old = nr.status
                # Гонка с complete_active_node_for_step: шаг уже ready в Project.
                if _node_already_succeeded_for_project(project, nr):
                    # complete_node принимает только initiator=worker
                    if complete_node(nr, project_id=run.project_id, initiator="worker"):
                        healed += 1
                        # only_agent ▶ → frames_ready: complete_active мог
                        # промахнуться в split и не снять маркер.
                        if (
                            project.status == ProjectStatus.frames_ready
                            and _effective_type_from_nr(nr) == "sd_agent"
                        ):
                            try:
                                from app.services.scene_design import (
                                    runner as sd_runner,
                                )

                                sd_runner.clear_only_agent(project)
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "[#{}] heal clear only_agent failed",
                                    run.project_id,
                                    exc_info=True,
                                )
                        logger.info(
                            "[#{}] NodeRun {}/{}: {} → done (heal after success, {})",
                            run.project_id,
                            nr.node_type,
                            nr.node_key,
                            old.value,
                            initiator,
                        )
                    continue
                # Рестарт mid-генерации / mid scene_design: не красим ноду в
                # failed — иначе UI «постоянно ошибки», хотя воркер сейчас подхватит.
                running_type = _canvas_node_type_for_running(project.status)
                status_val = getattr(project.status, "value", str(project.status))
                eff = _effective_type_from_nr(nr)
                # Точечный ▶ скелета: чужие sd_agent в running — сбросить, не keep.
                if (
                    status_val == "scene_designing"
                    and eff == "sd_agent"
                ):
                    try:
                        from app.services.scene_design import runner as sd_runner

                        only = sd_runner.get_only_agent(project)
                        only_key = (
                            sd_runner.resolve_sd_node_key(project, only)
                            if only
                            else None
                        )
                    except Exception:  # noqa: BLE001
                        only = None
                        only_key = None
                    if only_key and nr.node_key != only_key:
                        if reset_node_to_pending(
                            nr,
                            project_id=run.project_id,
                            initiator="only_agent_reconcile",
                        ):
                            fixed += 1
                            logger.info(
                                "[#{}] NodeRun {}/{}: {} → pending "
                                "(only_agent={}, {})",
                                run.project_id,
                                nr.node_type,
                                nr.node_key,
                                old.value,
                                only,
                                initiator,
                            )
                        continue
                if (
                    running_type is not None
                    and running_type == eff
                    and (
                        str(status_val).startswith("generating_")
                        or str(status_val).startswith("enriching_")
                        or status_val
                        in ("scene_designing", "scene_assembling")
                    )
                ):
                    logger.info(
                        "[#{}] NodeRun {}/{}: keep {} (project still {}, {})",
                        run.project_id,
                        nr.node_type,
                        nr.node_key,
                        old.value,
                        status_val,
                        initiator,
                    )
                    continue
                if fail_node(
                    nr,
                    _STALE_NODE_RUN_ERROR,
                    project_id=run.project_id,
                    initiator=initiator,
                ):
                    fixed += 1
                    logger.warning(
                        "[#{}] NodeRun {}/{}: {} → failed ({})",
                        run.project_id,
                        nr.node_type,
                        nr.node_key,
                        old.value,
                        initiator,
                    )
        if fixed or healed:
            await session.commit()
    if fixed:
        logger.info("reconcile stale NodeRun: {} → failed ({})", fixed, initiator)
    if healed:
        logger.info("reconcile stale NodeRun: {} → done heal ({})", healed, initiator)
    return fixed + healed


async def reconcile_stale_node_runs_on_startup() -> int:
    """NodeRun running/queued без живого воркера после перезапуска → failed."""
    return await _reconcile_stale_node_runs(initiator="startup_reconcile")


async def background_node_run_reconcile_loop(*, interval_sec: float = 60.0) -> None:
    """Фон: running/queued без живой задачи дольше N сек → failed."""
    while True:
        try:
            await _reconcile_stale_node_runs(
                initiator="background_reconcile",
                require_no_live_task=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("background_node_run_reconcile_loop failed")
        await asyncio.sleep(interval_sec)

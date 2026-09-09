"""Универсальная нода «Работа с GPT» (excel_gpt)."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any, Literal

from app.models import Project, ProjectStatus

InputSource = Literal[
    "project_xlsx",
    "upload",
    "voiceover",
    "image",
    "hero_refs",
    "scene_images",
]
WorkMode = Literal["assist", "review", "transform"]

EXCEL_GPT_STEP_CODE = "excel_gpt"
EXCEL_GPT_NODE_TYPE = "excel_gpt"
MAX_EXCEL_GPT_SLOTS = 5

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv"})
_UPLOAD_SUFFIXES = frozenset(
    {
        ".xlsx",
        ".xls",
        ".txt",
        ".md",
        ".json",
        ".csv",
        ".pdf",
        *_IMAGE_SUFFIXES,
        *_VIDEO_SUFFIXES,
    }
)
_VALID_SOURCES = frozenset(
    {"project_xlsx", "upload", "voiceover", "image", "hero_refs", "scene_images"}
)
_VALID_WORK_MODES = frozenset({"assist", "review", "transform"})

_SLOT_MAP: dict[int, tuple[ProjectStatus, ProjectStatus, str]] = {
    1: (ProjectStatus.enriching_1, ProjectStatus.enrich_1_ready, "enrich_1"),
    2: (ProjectStatus.enriching_2, ProjectStatus.enrich_2_ready, "enrich_2"),
    3: (ProjectStatus.enriching_3, ProjectStatus.enrich_3_ready, "enrich_3"),
    4: (ProjectStatus.enriching_4, ProjectStatus.enrich_4_ready, "enrich_4"),
    5: (ProjectStatus.enriching_5, ProjectStatus.enrich_5_ready, "enrich_5"),
}


def is_excel_gpt_node_type(node_type: str) -> bool:
    return node_type == EXCEL_GPT_NODE_TYPE or (
        node_type.startswith("enrich_") and node_type != "enrich"
    )


# Маркер scene-агента на ноде «Работа с GPT»: data.sd_agent =
# skeleton/characters/world/style/camera/action (агенты веера) или "assemble".
# Такие ноды — обычные excel_gpt в UI (пульт, промты 05_excel_gpt), но
# оркестратор гоняет их шагами scene_d/scene_asm, а не enrich-слотами.
SCENE_AGENT_DATA_KEY = "sd_agent"
SCENE_AGENT_ASSEMBLER = "assemble"


def sd_agent_marker(node: dict[str, Any]) -> str | None:
    """Имя scene-агента с ноды или None для обычной «Работы с GPT»."""
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    raw = data.get(SCENE_AGENT_DATA_KEY) or data.get("agent")  # agent — legacy
    v = str(raw or "").strip()
    return v or None


def effective_node_type(node: dict[str, Any]) -> str:
    """Тип ноды для оркестратора: marked excel_gpt → sd_agent/sd_assemble.

    UI видит тип excel_gpt (настоящая «Работа с GPT»), а планировщик и
    run_sync работают с виртуальными типами веера scene_design.
    """
    typ = str(node.get("type") or "")
    if typ in ("sd_agent", "sd_assemble"):
        return typ  # legacy-канвасы с уникальными типами
    if typ == EXCEL_GPT_NODE_TYPE:
        marker = sd_agent_marker(node)
        if marker:
            return (
                "sd_assemble" if marker == SCENE_AGENT_ASSEMBLER else "sd_agent"
            )
    return typ


def is_legacy_enrich_label(label: str | None) -> bool:
    """Старые подписи enrich_1..5 / «Доп работа с Excel» — перезаписываем при миграции."""
    if not label or not str(label).strip():
        return False
    low = str(label).strip().lower()
    markers = (
        "дополнение",
        "доп работа",
        "доп. работа",
        "доп. excel",
        "enrich",
        "excel #",
    )
    return any(m in low for m in markers)


def default_excel_gpt_label(_slot: int) -> str:
    return "ИИ-редактор сцен"


def legacy_enrich_slot_from_type(node_type: str) -> int | None:
    if not node_type.startswith("enrich_"):
        return None
    tail = node_type.removeprefix("enrich_")
    try:
        idx = int(tail)
    except ValueError:
        return None
    return idx if 1 <= idx <= MAX_EXCEL_GPT_SLOTS else None


def slot_index_from_node(node: dict[str, Any]) -> int:
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    if sd_agent_marker(node):
        # scene-агенты не занимают enrich-слоты (шаги scene_d/scene_asm).
        return 0
    if data.get("slotOverflow"):
        # >5 excel_gpt: не участвует в resolve(slot), только запуск по node_key.
        return 0
    raw = data.get("slotIndex")
    if isinstance(raw, int) and 1 <= raw <= MAX_EXCEL_GPT_SLOTS:
        return raw
    typ = str(node.get("type") or "")
    legacy = legacy_enrich_slot_from_type(typ)
    if legacy is not None:
        return legacy
    return 1


def assign_slot_indices(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Нумерует excel_gpt ноды слева направо (1..5). Лишние — без slotIndex.

    Ноды с маркером sd_agent (scene-агенты) слоты не занимают. Ноды с
    явным data.slotOverflow (проверки scene-веера и т.п.) закреплены вне
    слотов: нумерацию пропускают, флаг сохраняют.
    """
    def _pinned_overflow(n: dict[str, Any]) -> bool:
        data = n.get("data")
        return isinstance(data, dict) and data.get("slotOverflow") is True

    excel_nodes = sorted(
        [
            n
            for n in nodes
            if is_excel_gpt_node_type(str(n.get("type") or ""))
            and not sd_agent_marker(n)
            and not _pinned_overflow(n)
        ],
        key=lambda n: float((n.get("position") or {}).get("x", 0)),
    )
    out: list[dict[str, Any]] = []
    for i, n in enumerate(excel_nodes[:MAX_EXCEL_GPT_SLOTS], start=1):
        data = dict(n.get("data") or {})
        data["slotIndex"] = i
        data.pop("slotOverflow", None)
        label = str(data.get("label") or "").strip()
        if not label or is_legacy_enrich_label(label):
            data["label"] = default_excel_gpt_label(i)
        out.append({**n, "data": data, "type": EXCEL_GPT_NODE_TYPE})
    for n in excel_nodes[MAX_EXCEL_GPT_SLOTS:]:
        data = dict(n.get("data") or {})
        data.pop("slotIndex", None)
        data["slotOverflow"] = True
        label = str(data.get("label") or "").strip()
        if not label or is_legacy_enrich_label(label):
            data["label"] = default_excel_gpt_label(MAX_EXCEL_GPT_SLOTS)
        out.append({**n, "data": data, "type": EXCEL_GPT_NODE_TYPE})
    # Закреплённые overflow-ноды: флаг сохранить, slotIndex не давать.
    for n in nodes:
        if not (
            is_excel_gpt_node_type(str(n.get("type") or ""))
            and not sd_agent_marker(n)
            and _pinned_overflow(n)
        ):
            continue
        data = dict(n.get("data") or {})
        data.pop("slotIndex", None)
        out.append({**n, "data": data, "type": EXCEL_GPT_NODE_TYPE})
    keyed = {n["id"]: n for n in out}
    result: list[dict[str, Any]] = []
    for n in nodes:
        nid = n.get("id")
        if nid in keyed:
            result.append(keyed[nid])
        else:
            result.append(n)
    return result


def migrate_enrich_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    migrated: list[dict[str, Any]] = []
    for n in nodes:
        typ = str(n.get("type") or "")
        if is_excel_gpt_node_type(typ) and typ != EXCEL_GPT_NODE_TYPE:
            data = dict(n.get("data") or {})
            slot = legacy_enrich_slot_from_type(typ)
            if slot is not None:
                data.setdefault("slotIndex", slot)
            label = str(data.get("label") or "").strip()
            if not label or is_legacy_enrich_label(label):
                data["label"] = default_excel_gpt_label(slot or 1)
            migrated.append({**n, "type": EXCEL_GPT_NODE_TYPE, "data": data})
        else:
            migrated.append(n)
    return assign_slot_indices(migrated)


def _slot_status_pair(slot: int) -> tuple[ProjectStatus, ProjectStatus, str]:
    """Пара running/ready для слота 1..5.

    Слот 0 (slotOverflow / sd_agent без enrich) и любой мусор — занимаем
    enriching_1: фактическая нода задаётся ``active_excel_gpt_node_key``.
    Иначе ▶ overflow-ноды даёт KeyError → HTTP 500.
    """
    if slot in _SLOT_MAP:
        return _SLOT_MAP[slot]
    return _SLOT_MAP[1]


def running_status_for_slot(slot: int) -> ProjectStatus:
    return _slot_status_pair(slot)[0]


def ready_status_for_slot(slot: int) -> ProjectStatus:
    return _slot_status_pair(slot)[1]


def slot_from_running_status(status: ProjectStatus) -> int | None:
    for idx, (running, _ready, _code) in _SLOT_MAP.items():
        if status is running:
            return idx
    return None


def slot_from_ready_status(status: ProjectStatus) -> int | None:
    for idx, (_running, ready, _code) in _SLOT_MAP.items():
        if status is ready:
            return idx
    return None


def next_excel_gpt_slot_after_ready(project: Project, ready_status: ProjectStatus) -> int | None:
    """Следующий slot excel_gpt на канвасе после enrich_N_ready (игнор «готово»).

    Stale enrich_completed_slots / excel_gpt_completed_keys не должны давать
    прыжок enrich_2_ready → generating_hero, пока на канвасе есть слот 3+.
    """
    finished = slot_from_ready_status(ready_status)
    if finished is None:
        return None
    nodes = excel_gpt_nodes_from_project(project)
    if not nodes:
        return None
    later = sorted(
        {
            slot_index_from_node(n)
            for n in nodes
            if slot_index_from_node(n) > finished
        }
    )
    return later[0] if later else None


def first_work_successor_along_edges(
    project: Project, from_node_key: str
) -> tuple[str, str] | None:
    """Первый work-узел по исходящим стрелкам (passthrough/storage пропускаем).

    Returns (node_key, node_type) или None.
    """
    from collections import deque

    from app.orchestrator.graph.planner import (
        is_passthrough_node_type,
        is_work_node_type,
    )
    from app.services.canvas_graph import canvas_graph_from_meta

    key0 = (from_node_key or "").strip()
    if not key0:
        return None
    meta = project.meta if isinstance(project.meta, dict) else {}
    cg = canvas_graph_from_meta(meta) or {}
    by_id: dict[str, dict[str, Any]] = {}
    for n in cg.get("nodes") or []:
        if isinstance(n, dict) and n.get("id"):
            by_id[str(n["id"])] = n
    outs: dict[str, list[str]] = {}
    for e in cg.get("edges") or []:
        if not isinstance(e, dict):
            continue
        s = str(e.get("source") or "").strip()
        t = str(e.get("target") or "").strip()
        if s and t:
            outs.setdefault(s, []).append(t)
    visited: set[str] = set()
    queue: deque[str] = deque(outs.get(key0, []))
    while queue:
        key = queue.popleft()
        if key in visited:
            continue
        visited.add(key)
        node = by_id.get(key) or {}
        typ = effective_node_type(node)
        if is_passthrough_node_type(typ):
            queue.extend(outs.get(key, []))
            continue
        if is_work_node_type(typ):
            return key, typ
        queue.extend(outs.get(key, []))
    return None


def first_work_successor_from_excel_slot(
    project: Project,
    from_slot: int,
    *,
    from_key: str | None = None,
) -> tuple[str, str, int | None] | None:
    """(node_key, type, slot_if_excel_gpt) — следующий work после excel_gpt слота.

    from_key — явный id завершённой ноды (стрелки). Иначе resolve(slot) при
    коллизии slotIndex может стартовать не с той ноды.
    """
    key0 = (from_key or "").strip() or resolve_excel_gpt_node_key_for_slot(
        project, from_slot
    )
    if not key0:
        return None
    succ = first_work_successor_along_edges(project, key0)
    if succ is None:
        return None
    key, typ = succ
    slot: int | None = None
    if is_excel_gpt_node_type(typ):
        slot = slot_for_excel_gpt_node_key(project, key)
        if slot is None:
            node = next(
                (
                    n
                    for n in excel_gpt_nodes_from_project(project)
                    if str(n.get("id") or "") == key
                ),
                None,
            )
            if node is not None:
                slot = slot_index_from_node(node)
    return key, typ, slot


def next_excel_gpt_running_after_ready(
    project: Project, ready_status: ProjectStatus
) -> ProjectStatus | None:
    """Running-статус следующего excel_gpt после enrich_N_ready, если слот есть."""
    nxt = next_excel_gpt_slot_after_ready(project, ready_status)
    if nxt is None:
        return None
    return running_status_for_slot(nxt)


def prepare_enrich_chain_for_auto_advance(
    project: Project,
    ready_status: ProjectStatus,
    *,
    finished_key: str | None = None,
) -> ProjectStatus | None:
    """Перед auto_advance с enrich_N_ready: цепочка ТОЛЬКО по стрелкам.

    Если следующий work по рёбрам — excel_gpt M — вернуть enriching_M.
    Если стрелка ведёт в script/hero/другое — None (caller идёт по graph BFS).

    Активную ноду ставим по id со стрелки, НЕ через resolve(slot) — иначе при
    двух excel_gpt с одним slotIndex (лимит 5 слотов / 6+ нод) прыжок мимо
    проверки на «персонажи».
    """
    finished = slot_from_ready_status(ready_status)
    if finished is None:
        return None
    from_key = (finished_key or "").strip() or None
    if not from_key:
        # Последний completed key на этом слоте. overflow-группа (slot 0)
        # тоже ходит через enrich_N_ready — слот в meta не 1..5, поэтому
        # берём последний не-sd completed, иначе resolve(N) хватает world.
        meta0 = project.meta if isinstance(project.meta, dict) else {}
        for raw in reversed(list(meta0.get("excel_gpt_completed_keys") or [])):
            k = str(raw or "").strip()
            if not k:
                continue
            slot_k = slot_for_excel_gpt_node_key(project, k)
            if slot_k == finished:
                from_key = k
                break
        if not from_key:
            for raw in reversed(list(meta0.get("excel_gpt_completed_keys") or [])):
                k = str(raw or "").strip()
                if not k:
                    continue
                node = canvas_node_by_key(project, k)
                if node is None or sd_agent_marker(node):
                    continue
                slot_k = slot_for_excel_gpt_node_key(project, k)
                if slot_k in (0, None):
                    from_key = k
                    break
        if not from_key:
            from_key = resolve_excel_gpt_node_key_for_slot(project, finished)
    succ = first_work_successor_from_excel_slot(
        project, finished, from_key=from_key
    )
    if succ is None:
        return None
    next_key, typ, nxt_slot = succ
    if not is_excel_gpt_node_type(typ):
        return None
    if not next_key or next_key == (from_key or ""):
        return None
    if nxt_slot is None:
        nxt_slot = slot_for_excel_gpt_node_key(project, next_key)
    if nxt_slot is None:
        return None
    meta = dict(project.meta or {})
    if nxt_slot < 1:
        # overflow → overflow: слот общий (enriching_1), различаем по id.
        keys = [str(k) for k in (meta.get("excel_gpt_completed_keys") or [])]
        if next_key in keys:
            meta["excel_gpt_completed_keys"] = [k for k in keys if k != next_key]
        meta["active_excel_gpt_node_key"] = next_key
        project.meta = meta
        return running_status_for_slot(nxt_slot)
    ensure_enrich_auto_chain_to(project, finished, from_key=from_key)
    clear_excel_gpt_tail_completion(project, nxt_slot)
    meta = dict(project.meta or {})
    meta["active_excel_gpt_node_key"] = next_key
    project.meta = meta
    return running_status_for_slot(nxt_slot)


def active_excel_gpt_node_key(project: Project) -> str | None:
    meta = project.meta if isinstance(project.meta, dict) else {}
    key = str(meta.get("active_excel_gpt_node_key") or "").strip()
    return key or None


def canvas_node_by_key(project: Project, node_key: str) -> dict[str, Any] | None:
    """Нода канваса по id (включая scene-агентов type=excel_gpt)."""
    key = (node_key or "").strip()
    if not key:
        return None
    from app.services.canvas_graph import canvas_graph_from_meta

    meta = project.meta if isinstance(project.meta, dict) else {}
    cg = canvas_graph_from_meta(meta) or {}
    for n in cg.get("nodes") or []:
        if isinstance(n, dict) and str(n.get("id") or "").strip() == key:
            return n
    return None


def excel_gpt_nodes_from_project(project: Project) -> list[dict[str, Any]]:
    """Ноды excel_gpt из canvas_graph meta (без scene-агентов sd_agent)."""
    from app.services.canvas_graph import canvas_graph_from_meta

    meta = project.meta if isinstance(project.meta, dict) else {}
    cg = canvas_graph_from_meta(meta)
    nodes = list((cg or {}).get("nodes") or [])
    return [
        n
        for n in nodes
        if is_excel_gpt_node_type(str(n.get("type") or ""))
        and not sd_agent_marker(n)
    ]


def _resolve_colliding_slot_via_graph(
    project: Project,
    match_ids: set[str],
) -> str | None:
    """При коллизии slotIndex — следующая excel_gpt по стрелкам канваса."""
    from app.orchestrator.graph.planner import WorkflowGraph
    from app.orchestrator.node_registry import READY_TO_NODE_TYPE, RUNNING_TO_NODE_TYPE
    from app.services.canvas_graph import canvas_graph_from_meta

    meta = project.meta if isinstance(project.meta, dict) else {}
    cg = canvas_graph_from_meta(meta) or {}
    g_nodes = list(cg.get("nodes") or [])
    g_edges = list(cg.get("edges") or [])
    if not g_nodes or not g_edges:
        return None
    graph = WorkflowGraph(g_nodes, g_edges)

    status = project.status
    ready: ProjectStatus | None = None
    if status in READY_TO_NODE_TYPE:
        ready = status
    elif status in RUNNING_TO_NODE_TYPE:
        # enriching_5 → ищем от hero_ready / предыдущего ready по статусу
        # через ready-пары слота и общие *_ready.
        slot = slot_from_running_status(status)
        if slot is not None:
            # Уже в running этого слота — следующий по графу от предыдущего
            # ready не восстановить однозначно; вернём active, если он в match.
            active = str(meta.get("active_excel_gpt_node_key") or "").strip()
            if active in match_ids:
                return active
        # Частый кейс: после hero ещё не pin'нули — status hero_ready уже выше.
        return None
    if ready is None:
        return None
    key = graph.next_work_node_key_after_ready(project, ready)
    if key and key in match_ids:
        return key
    return None


def resolve_excel_gpt_node_key_for_slot(
    project: Project,
    slot: int,
    *,
    nodes: list[dict[str, Any]] | None = None,
) -> str | None:
    """Найти node_key excel_gpt для слота 1..5 (по графу при коллизии)."""
    from loguru import logger

    if not (1 <= int(slot) <= MAX_EXCEL_GPT_SLOTS):
        return None
    pool = nodes if nodes is not None else excel_gpt_nodes_from_project(project)
    pool = [n for n in pool if not sd_agent_marker(n)]
    if not pool:
        return None
    matches = [
        n
        for n in pool
        if slot_index_from_node(n) == slot
        and not (
            isinstance(n.get("data"), dict) and n["data"].get("slotOverflow")
        )
    ]
    if matches:
        matches.sort(
            key=lambda n: float((n.get("position") or {}).get("x", 0)),
        )
        match_ids = {
            str(n.get("id") or "").strip()
            for n in matches
            if str(n.get("id") or "").strip()
        }
        # Коллизия slotIndex: только граф / стрелки. Incomplete-leftmost
        # врёт, если ранние ноды уже пройдены, но ключ не записан.
        if len(matches) > 1:
            graph_key = _resolve_colliding_slot_via_graph(project, match_ids)
            if graph_key:
                logger.info(
                    "excel_gpt: slotIndex={} collides on {} — graph→{}",
                    slot,
                    len(matches),
                    graph_key,
                )
                return graph_key
            done_keys = completed_node_keys(project)
            incomplete = [
                n
                for n in matches
                if str(n.get("id") or "").strip() not in done_keys
            ]
            pick = incomplete[0] if incomplete else matches[0]
            logger.warning(
                "excel_gpt: slotIndex={} collides on {} nodes — fallback {} "
                "(incomplete={})",
                slot,
                len(matches),
                pick.get("id"),
                len(incomplete),
            )
            nid = str(pick.get("id") or "").strip()
            return nid or None
        nid = str(matches[0].get("id") or "").strip()
        return nid or None
    ordered = sorted(
        [
            n
            for n in pool
            if not (
                isinstance(n.get("data"), dict) and n["data"].get("slotOverflow")
            )
        ],
        key=lambda n: float((n.get("position") or {}).get("x", 0)),
    )
    if slot - 1 < len(ordered):
        nid = str(ordered[slot - 1].get("id") or "").strip()
        return nid or None
    return None


def active_node_key(project: Project) -> str | None:
    meta = project.meta if isinstance(project.meta, dict) else {}
    key = meta.get("active_excel_gpt_node_key")
    return str(key) if key else None


def node_config(project: Project, node_key: str) -> dict[str, Any]:
    meta = project.meta if isinstance(project.meta, dict) else {}
    configs = meta.get("excel_gpt_nodes")
    if not isinstance(configs, dict):
        return {}
    cfg = configs.get(node_key)
    return dict(cfg) if isinstance(cfg, dict) else {}


def input_source(project: Project, node_key: str | None) -> InputSource:
    if not node_key:
        return "project_xlsx"
    cfg = node_config(project, node_key)
    raw = str(cfg.get("inputSource") or "project_xlsx")
    if raw in _VALID_SOURCES:
        return raw  # type: ignore[return-value]
    return "project_xlsx"


def work_mode(project: Project, node_key: str | None) -> WorkMode:
    """Роль ноды: участвует / проверяет результат / преобразует ввод."""
    if not node_key:
        return "assist"
    cfg = node_config(project, node_key)
    raw = str(cfg.get("workMode") or "assist").strip().lower()
    if raw in _VALID_WORK_MODES:
        return raw  # type: ignore[return-value]
    return "assist"


def expects_xlsx_result(project: Project, node_key: str | None) -> bool:
    """True — round-trip ждёт обновлённый .xlsx; False — достаточно ответа GPT."""
    if node_key:
        cfg = node_config(project, node_key)
        # DB SoT: ответ = apply-ops JSON, не файл Excel.
        if str(cfg.get("outputMode") or "").strip() == "project_file":
            return False
    src = input_source(project, node_key)
    if src in ("image", "hero_refs", "scene_images", "voiceover"):
        return False
    mode = work_mode(project, node_key)
    if mode in ("review", "transform") and src == "upload":
        cfg = node_config(project, node_key) if node_key else {}
        name = str(cfg.get("uploadedFileName") or "").lower()
        if Path(name).suffix.lower() in _IMAGE_SUFFIXES:
            return False
    return True


def is_allowed_upload_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in _UPLOAD_SUFFIXES


def upload_dir(project: Project, node_key: str) -> Path:
    return project.data_dir / "excel_gpt_uploads" / node_key


def upload_file_path(project: Project, node_key: str, filename: str) -> Path:
    safe = Path(filename).name
    return upload_dir(project, node_key) / safe


def _collect_images_under(root: Path, *, limit: int = 12) -> list[Path]:
    if not root.is_dir():
        return []
    found: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        if p.stat().st_size < 256:
            continue
        found.append(p)
        if len(found) >= limit:
            break
    return found


def completed_node_keys(project: Project) -> set[str]:
    meta = project.meta if isinstance(project.meta, dict) else {}
    raw = meta.get("excel_gpt_completed_keys") or []
    return {str(k) for k in raw if k}


def max_excel_gpt_slot(project: Project) -> int:
    """Максимальный slotIndex среди excel_gpt на канвасе (1..5)."""
    nodes = excel_gpt_nodes_from_project(project)
    if not nodes:
        return 1
    return max(slot_index_from_node(n) for n in nodes)


def next_incomplete_excel_gpt_slot(project: Project, after_slot: int) -> int | None:
    """Следующий слот excel_gpt после after_slot (для цепочки / gen_queue bypass).

    При активном enrich_auto_chain_to возвращает следующий слот даже если
    он уже в completed — старт должен перегенерировать.
    """
    nodes = excel_gpt_nodes_from_project(project)
    if not nodes:
        return None
    meta = project.meta if isinstance(project.meta, dict) else {}
    chain_to = meta.get("enrich_auto_chain_to")
    done_slots = set()
    for raw in meta.get("enrich_completed_slots") or []:
        try:
            done_slots.add(int(raw))
        except (TypeError, ValueError):
            continue
    done_keys = completed_node_keys(project)
    candidates: list[int] = []
    for n in nodes:
        slot = slot_index_from_node(n)
        if slot <= after_slot:
            continue
        nid = str(n.get("id") or "")
        marked_done = slot in done_slots or (nid and nid in done_keys)
        if marked_done and not (
            isinstance(chain_to, int) and slot <= chain_to
        ):
            continue
        candidates.append(slot)
    if candidates:
        return min(candidates)
    if isinstance(chain_to, int) and after_slot < chain_to:
        nxt = after_slot + 1
        if any(slot_index_from_node(n) == nxt for n in nodes):
            return nxt
    return None


def ensure_enrich_auto_chain_to(
    project: Project,
    from_slot: int,
    *,
    from_key: str | None = None,
) -> int | None:
    """Выставить enrich_auto_chain_to по цепочке excel_gpt ТОЛЬКО вдоль стрелок.

    GPT1→script→GPT2: после слота 1 цепочка НЕ ставится (следующий work = script).
    GPT1→GPT2→GPT3: chain_to=3.
    """
    end = from_slot
    cur = from_slot
    cur_key = (from_key or "").strip() or None
    seen: set[str] = set()
    if cur_key:
        seen.add(cur_key)
    guard = 0
    while guard < MAX_EXCEL_GPT_SLOTS + 2:
        guard += 1
        succ = first_work_successor_from_excel_slot(
            project, cur, from_key=cur_key
        )
        if succ is None:
            break
        key, typ, slot = succ
        if not is_excel_gpt_node_type(typ) or slot is None:
            break
        if key in seen:
            break
        seen.add(key)
        end = max(end, slot)
        cur = slot
        cur_key = key
    if end <= from_slot:
        return None
    meta = dict(project.meta or {})
    cur_meta = meta.get("enrich_auto_chain_to")
    if isinstance(cur_meta, int) and cur_meta >= end:
        return cur_meta
    meta["enrich_auto_chain_to"] = end
    project.meta = meta
    return end


def slot_for_excel_gpt_node_key(project: Project, node_key: str) -> int | None:
    """slotIndex (1..5) для node_key на канвасе."""
    key = (node_key or "").strip()
    if not key:
        return None
    for n in excel_gpt_nodes_from_project(project):
        if str(n.get("id") or "").strip() == key:
            return slot_index_from_node(n)
    return None


def clear_excel_gpt_tail_completion(
    project: Project,
    from_slot: int,
) -> dict[str, Any]:
    """Снять enrich_completed / excel_gpt_completed_keys для слотов >= from_slot.

    Старт/цепочка должны перегенерировать даже «готовые» ноды.
    """
    if from_slot < 1:
        from_slot = 1
    nodes = excel_gpt_nodes_from_project(project)
    slots_to_clear: set[int] = set()
    keys_to_clear: set[str] = set()
    for n in nodes:
        slot = slot_index_from_node(n)
        if slot < from_slot:
            continue
        slots_to_clear.add(slot)
        nid = str(n.get("id") or "").strip()
        if nid:
            keys_to_clear.add(nid)

    meta = dict(project.meta or {})
    completed = [
        int(x) for x in (meta.get("enrich_completed_slots") or []) if str(x).isdigit()
    ]
    slots_cleared = [s for s in completed if s in slots_to_clear]
    if slots_cleared:
        meta["enrich_completed_slots"] = sorted(
            s for s in completed if s not in slots_to_clear
        )

    keys = [str(k) for k in (meta.get("excel_gpt_completed_keys") or [])]
    keys_cleared = [k for k in keys if k in keys_to_clear]
    if keys_cleared:
        meta["excel_gpt_completed_keys"] = [k for k in keys if k not in keys_to_clear]

    if slots_cleared or keys_cleared:
        project.meta = meta
    return {
        "from_slot": from_slot,
        "slots_cleared": slots_cleared,
        "keys_cleared": keys_cleared,
    }


def excel_gpt_force_rerun_slots(project: Project) -> set[int]:
    """Слоты, которые нельзя пропускать как done (активная цепочка)."""
    meta = project.meta if isinstance(project.meta, dict) else {}
    chain_to = meta.get("enrich_auto_chain_to")
    if not isinstance(chain_to, int):
        return set()
    return set(range(1, min(5, chain_to) + 1))


def display_attachment_name(project: Project, node_key: str | None) -> str:
    src = input_source(project, node_key)
    if src == "voiceover":
        return "voiceover.txt"
    if src == "hero_refs":
        return "characters/* (рефы персонажей)"
    if src == "scene_images":
        return "scenes/* (картинки кадров)"
    if src in ("upload", "image") and node_key:
        cfg = node_config(project, node_key)
        name = str(cfg.get("uploadedFileName") or "").strip()
        if name:
            return name
        return "image.png" if src == "image" else "upload.xlsx"
    return "project.xlsx"


def attachment_paths(project: Project, node_key: str | None = None) -> list[Path]:
    """Файлы данных для отправки в GPT (без промта)."""
    key = node_key or active_node_key(project)
    src = input_source(project, key)
    paths: list[Path] = []
    if src == "voiceover":
        voice = project.data_dir / "voiceover.txt"
        if voice.is_file():
            paths.append(voice)
        return paths
    if src == "hero_refs":
        return _collect_images_under(project.data_dir / "characters", limit=8)
    if src == "scene_images":
        scenes = _collect_images_under(project.data_dir / "scenes", limit=8)
        if scenes:
            return scenes
        return _collect_images_under(project.data_dir / "images", limit=8)
    if src in ("upload", "image") and key:
        cfg = node_config(project, key)
        fname = str(cfg.get("uploadedFileName") or "").strip()
        if fname:
            p = upload_file_path(project, key, fname)
            if p.is_file():
                paths.append(p)
                return paths
        # Fallback: любой файл в upload-папке ноды.
        udir = upload_dir(project, key)
        if udir.is_dir():
            for p in sorted(udir.iterdir()):
                if p.is_file() and p.suffix.lower() in _UPLOAD_SUFFIXES:
                    paths.append(p)
                    break
        return paths
    xlsx = project.data_dir / "project.xlsx"
    if xlsx.is_file():
        paths.append(xlsx)
    return paths


def save_gpt_reply_text(
    project: Project, node_key: str | None, reply: str
) -> Path | None:
    """Сохранить текстовый ответ GPT (режим review/transform / image)."""
    from datetime import datetime

    key = (node_key or active_node_key(project) or "default").strip() or "default"
    out_dir = upload_dir(project, key)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "gpt_reply.txt"
    path.write_text((reply or "").strip() + "\n", encoding="utf-8")
    meta = dict(project.meta or {})
    configs = dict(meta.get("excel_gpt_nodes") or {})
    cur = dict(configs.get(key) or {})
    cur["lastReplyPath"] = str(path.relative_to(project.data_dir)).replace("\\", "/")
    cur["lastReplyAt"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    configs[key] = cur
    meta["excel_gpt_nodes"] = configs
    project.meta = meta
    return path


async def clear_slot_completion_meta(
    session: Any,
    project: Project,
    slot: int,
    *,
    node_key: str | None = None,
) -> dict[str, Any]:
    """Сбросить enrich_completed_slots и excel_gpt_completed_keys для слота."""
    from app.orchestrator.graph.planner import load_graph_for_project

    meta = dict(project.meta or {})
    completed = [int(x) for x in (meta.get("enrich_completed_slots") or []) if str(x).isdigit()]
    slots_cleared: list[int] = []
    if slot in completed:
        completed.remove(slot)
        slots_cleared.append(slot)
        meta["enrich_completed_slots"] = sorted(completed)

    keys = [str(k) for k in (meta.get("excel_gpt_completed_keys") or [])]
    keys_cleared: list[str] = []
    targets: set[str] = set()
    if node_key:
        targets.add(str(node_key))
    graph = await load_graph_for_project(session, project)
    # overflow / slot 0: вся fw-группа и sd_agent имеют slotIndex=0.
    # ▶ одной ноды не должен сносить completed_keys соседей.
    if slot >= 1:
        for nid, n in graph._by_id.items():
            if str(n.get("type") or "") != EXCEL_GPT_NODE_TYPE:
                continue
            if sd_agent_marker(n):
                continue
            if slot_index_from_node(n) == slot:
                targets.add(str(nid))
    for k in list(keys):
        if k in targets:
            keys.remove(k)
            keys_cleared.append(k)
    meta["excel_gpt_completed_keys"] = keys
    if node_key and str(meta.get("active_excel_gpt_node_key") or "") == str(node_key):
        meta.pop("active_excel_gpt_node_key", None)
    project.meta = meta
    return {
        "slot": slot,
        "slots_cleared": slots_cleared,
        "keys_cleared": keys_cleared,
    }


def remap_node_keys_in_meta(project: Project, mapping: dict[str, str]) -> list[str]:
    """Перенести excel_gpt_nodes / storage / snapshots / edges при смене node_key (paste)."""
    import shutil

    meta = dict(project.meta or {})
    configs = dict(meta.get("excel_gpt_nodes") or {})
    remapped: list[str] = []
    for old_key, new_key in mapping.items():
        old_s, new_s = str(old_key), str(new_key)
        if not old_s or not new_s or old_s == new_s:
            continue
        cfg = configs.pop(old_s, None)
        if cfg:
            configs[new_s] = dict(cfg)
            remapped.append(new_s)
        old_dir = upload_dir(project, old_s)
        if old_dir.is_dir():
            new_dir = upload_dir(project, new_s)
            new_dir.mkdir(parents=True, exist_ok=True)
            for f in old_dir.iterdir():
                if f.is_file():
                    shutil.copy2(f, new_dir / f.name)
    active = meta.get("active_excel_gpt_node_key")
    if active and str(active) in mapping:
        meta["active_excel_gpt_node_key"] = mapping[str(active)]
    done_keys = [str(k) for k in (meta.get("excel_gpt_completed_keys") or [])]
    meta["excel_gpt_completed_keys"] = [mapping.get(k, k) for k in done_keys]
    meta["excel_gpt_nodes"] = configs

    # storage_nodes / gpt_operator_results / xlsx snapshots — иначе связи «отваливаются»
    for bucket in ("storage_nodes", "gpt_operator_results", "xlsx_snapshots_by_node"):
        raw = meta.get(bucket)
        if not isinstance(raw, dict):
            continue
        moved = dict(raw)
        for old_key, new_key in mapping.items():
            old_s, new_s = str(old_key), str(new_key)
            if not old_s or not new_s or old_s == new_s:
                continue
            if old_s in moved and new_s not in moved:
                moved[new_s] = moved.pop(old_s)
        meta[bucket] = moved

    # canvas edges: source/target id
    cg = meta.get("canvas_graph")
    if isinstance(cg, dict):
        edges = cg.get("edges")
        if isinstance(edges, list):
            new_edges = []
            for e in edges:
                if not isinstance(e, dict):
                    new_edges.append(e)
                    continue
                ee = dict(e)
                src = str(ee.get("source") or "")
                tgt = str(ee.get("target") or "")
                if src in mapping:
                    ee["source"] = mapping[src]
                if tgt in mapping:
                    ee["target"] = mapping[tgt]
                new_edges.append(ee)
            cg = dict(cg)
            cg["edges"] = new_edges
            meta["canvas_graph"] = cg

    project.meta = meta
    return remapped

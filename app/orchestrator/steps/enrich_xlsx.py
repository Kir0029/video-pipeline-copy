"""Шаг «Доп работа с EXCEL» — generic xlsx round-trip с ChatGPT.

Параметризован по slot_idx (1..5). Каждый слот — отдельный
мастер-промт (в `prompts/05<a..e>_enrich_<i>/`) и отдельный gpt_text
override (через `Project.gpt_text_overrides["enrich_<i>"]`).

Поток:
  1. Берём текущий `data/videos/<slug>/project.xlsx`.
  2. Открываем НОВЫЙ чат ChatGPT (без истории прошлых шагов).
  3. Аплоадим xlsx как вложение + шлём промт «мастер + сопровождающий
     текст».
  4. Ждём ответ. Скачиваем приложенный к ответу обновлённый xlsx и
     сохраняем поверх исходного `project.xlsx`.
  5. Если ChatGPT не приложил файл — повторяем (новый чат) до 3 раз.
  6. Данные в БД пишет apply-ops; Excel → DB только явный Import.
     `recompute_status()` поднимет статус. И принудительно ставим
     статус `enrich_<i>_ready`.
"""

from __future__ import annotations

from pathlib import Path

from aiogram import Bot
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Frame, Project, ProjectStatus
from app.services import chatgpt_xlsx as cx
from app.services import gpt_text_builder as gtb
from app.services import xlsx_gpt_flow as xgf
from app.services.excel_gpt_node import (
    EXCEL_GPT_STEP_CODE,
    active_node_key,
    attachment_paths,
    display_attachment_name,
    expects_xlsx_result,
    save_gpt_reply_text,
    work_mode,
)
from app.services.prompt_library import read_resolved_project_prompt
from app.services.xlsx_versioning import validate_xlsx
from app.storage import for_project as _sheet_for_project

_APPLY_OPS_HINT = (
    "# ЗАПИСЬ В ПРОЕКТ (предпочтительный формат)\n"
    "Верни ТОЛЬКО JSON apply-ops (без TSV, без `# Лист:`, без `@row=`):\n"
    '{"ops":[{"frame_uuid":"<uuid кадра>","fields":{...}}]}\n'
    "Поля по-человечески: закадр, промт_картинки, промт_видео, смысл, "
    "длительность, промт_картинки_2, промт_видео_2, персонажи; общий план: "
    '{"target":"project","fields":{"общий_план":"…"}}.\n'
    "Реестр персонажей (опционально в том же JSON): "
    '{"characters":[{"id":"c01","имя":"…","внешность":"…","одежда":"…",'
    '"характер":"…","правила":""}],'
    '"ops":[{"frame_uuid":"…","fields":{"персонажи":"c01"}}]}.\n'
    "Неизвестный uuid/поле — весь запрос отклонён. "
    "Адресация кадров (номер = uuid):\n"
)

_SCENE_GRAMMAR_APPLY_HINT = (
    "# SCENE GRAMMAR — ЗАПИСЬ (v1.6, постановка кадра)\n"
    "JSON {characters, scenes, ops, report}. "
    "Каждый shot: подробный фон + конкретное освещение + описание_кадра=камера. "
    "То же место → тот же фон/свет, меняется только камера. "
    "смысл_сцены=видимый факт (не «зритель не знает»). "
    "ops=[]. Запрет: клон акцента; однословный фон; 1 колонка=1 сцена.\n"
)

_CHARACTER_REGISTRY_APPLY_HINT = (
    "# CHARACTER REGISTRY — ЗАПИСЬ (DB SoT)\n"
    "Верни ТОЛЬКО JSON {characters, ops, report}. "
    "Вход = db_frames.json (закадр + текущие Entity). "
    "Пустой characters[] во входе = создай реестр с нуля по закадру. "
    "Запрет: error «реестр не найден»; Excel; TSV; # Лист:; проза вне JSON. "
    "ops: только поле персонажи по frame_uuid из входа.\n"
)

_SCENE_GRAMMAR_PROMPT_MARKERS = (
    "scene_grammar_unified_agent",
    "scene grammar unified",
)

_CHARACTER_REGISTRY_PROMPT_MARKERS = (
    "character_registry_database_agent",
    "агент по созданию персонажей",
    "агент заполнения реестра персонажей",
    "реестр персонаж",
)


def _is_scene_grammar_prompt(variant: str | None, master: str | None) -> bool:
    blob = f"{variant or ''}\n{(master or '')[:400]}".casefold()
    return any(m in blob for m in _SCENE_GRAMMAR_PROMPT_MARKERS)


def _is_character_registry_prompt(variant: str | None, master: str | None) -> bool:
    blob = f"{variant or ''}\n{(master or '')[:800]}".casefold()
    return any(m in blob for m in _CHARACTER_REGISTRY_PROMPT_MARKERS)


_SCRIPT_WRITER_PROMPT_MARKERS = (
    "script_writer",
    "сценарист закадра",
)
_MAIN_ACTION_PROMPT_MARKERS = (
    "main_action_from_bits",
    "главное действие · по битам",
    "главное действие кадра по битам",
)
_SCENES_TO_FRAMES_MARKERS = (
    "scenes_to_frames",
    "сцены → кадры",
    "сцены -> кадры",
)
_FRAME_PROMPTS_MARKERS = (
    "frame_prompts",
    "промты кадров",
)
_QC_PROMPTS_MARKERS = (
    "prompts_qc",
    "qc промпт",
)
_SCENE_ANALYTICS_MARKERS = (
    "54_59",
    "scene_analytics_54_59",
    "scene_analytics",
)


def _drop_replace_frames_ops(ops: list) -> tuple[list, int]:
    """Разбивка — только шаг split / camera_expand, не excel_gpt."""
    kept = [
        op
        for op in ops
        if not (isinstance(op, dict) and op.get("target") == "replace_frames")
    ]
    return kept, len(ops) - len(kept)


def _filter_check_frame_ops(
    ops: list,
    known_uuids: list[str],
    number_to_uuid: dict[int, str] | None = None,
) -> tuple[list, int]:
    """checkMode: неизвестные uuid не валят шаг — дропаем, гейт остаётся."""
    from app.services.db_apply import (
        remap_frame_number_uuids,
        repair_near_miss_frame_uuids,
    )

    frame_ops = [op for op in ops if isinstance(op, dict)]
    if number_to_uuid:
        remap_frame_number_uuids(frame_ops, number_to_uuid)
    known = [u for u in known_uuids if u]
    if known:
        repair_near_miss_frame_uuids(frame_ops, known)
    known_set = set(known)
    kept: list = []
    dropped = 0
    for op in frame_ops:
        uid = str(op.get("frame_uuid") or "").strip()
        if uid in known_set:
            kept.append(op)
        else:
            dropped += 1
    return kept, dropped


def _prompt_blob(variant: str | None, master: str | None) -> str:
    return f"{variant or ''}\n{(master or '')[:800]}".casefold()


def _is_script_writer_prompt(variant: str | None, master: str | None) -> bool:
    """Нода сценария на канвасе: размечает биты в готовом закадре (текст не пишет)."""
    return any(m in _prompt_blob(variant, master) for m in _SCRIPT_WRITER_PROMPT_MARKERS)


def _is_script_writer_node(variant: str | None, master: str | None, node_key: str | None) -> bool:
    nk = str(node_key or "")
    return _is_script_writer_prompt(variant, master) or nk.endswith("_fw_script")


def _is_main_action_node(variant: str | None, master: str | None, node_key: str | None) -> bool:
    nk = str(node_key or "")
    blob = _prompt_blob(variant, master)
    return any(m in blob for m in _MAIN_ACTION_PROMPT_MARKERS) or nk.endswith("_fw_action")


def _is_scenes_to_frames_node(
    variant: str | None, master: str | None, node_key: str | None
) -> bool:
    nk = str(node_key or "")
    # Промт картинок цитирует «сцены → кадры» в шапке — это не нода покрытия.
    if nk.endswith(("_fw_frames", "_fw_qc", "_fw_script", "_fw_action")):
        return False
    if nk.endswith("_fw_shots"):
        return True
    if _is_frame_prompts_prompt(variant, master) or _is_qc_prompts_prompt(
        variant, master
    ):
        return False
    blob = _prompt_blob(variant, master)
    return any(m in blob for m in _SCENES_TO_FRAMES_MARKERS)


def _is_vo_cell_markup_node(
    variant: str | None, master: str | None, node_key: str | None
) -> bool:
    """Биты / главное действие / сцены→кадры работают по VO-ячейке, не по шотам."""
    return (
        _is_script_writer_node(variant, master, node_key)
        or _is_main_action_node(variant, master, node_key)
        or _is_scenes_to_frames_node(variant, master, node_key)
    )


_SCRIPT_FRAMES_QC_SUFFIXES = (
    "_fw_script",
    "_fw_action",
    "_fw_shots",
    "_fw_frames",
    "_fw_qc",
)


def _is_script_frames_qc_group_node(
    variant: str | None, master: str | None, node_key: str | None
) -> bool:
    """Группа script_frames_qc: биты → действие → кадры → промты → QC."""
    nk = str(node_key or "")
    if any(nk.endswith(s) for s in _SCRIPT_FRAMES_QC_SUFFIXES):
        return True
    return (
        _is_vo_cell_markup_node(variant, master, node_key)
        or _is_frame_prompts_prompt(variant, master)
        or _is_qc_prompts_prompt(variant, master)
    )


def _script_frames_qc_footer_kind(
    variant: str | None, master: str | None, node_key: str | None
) -> str:
    nk = str(node_key or "")
    if nk.endswith("_fw_script"):
        return "bits"
    if nk.endswith("_fw_action"):
        return "action_chain"
    if nk.endswith("_fw_shots"):
        return "shots_coverage"
    if nk.endswith(("_fw_frames", "_fw_qc")):
        return "prompts"
    if _is_script_writer_node(variant, master, node_key):
        return "bits"
    if _is_main_action_node(variant, master, node_key):
        return "action_chain"
    if _is_scenes_to_frames_node(variant, master, node_key):
        return "shots_coverage"
    return "prompts"


def _project_topic_bits(project) -> list[str]:
    """Тема/план для ноды сценария. Project.title, не .name (его нет)."""
    bits: list[str] = []
    title = str(getattr(project, "title", None) or "").strip()
    if title:
        bits.append(f"Название проекта: {title}")
    meta = getattr(project, "meta", None)
    plan_text = str(
        getattr(project, "general_plan", None)
        or (meta if isinstance(meta, dict) else {}).get("general_plan")
        or ""
    ).strip()
    if plan_text:
        bits.append(f"Общий план:\n{plan_text}")
    return bits


def _is_frame_prompts_prompt(variant: str | None, master: str | None) -> bool:
    # QC-промт ссылается на frame_prompts_continuity — это не fill-агент.
    if _is_qc_prompts_prompt(variant, master):
        return False
    return any(m in _prompt_blob(variant, master) for m in _FRAME_PROMPTS_MARKERS)


def _is_qc_prompts_prompt(variant: str | None, master: str | None) -> bool:
    return any(m in _prompt_blob(variant, master) for m in _QC_PROMPTS_MARKERS)


def _is_scene_analytics_prompt(variant: str | None, master: str | None) -> bool:
    return any(m in _prompt_blob(variant, master) for m in _SCENE_ANALYTICS_MARKERS)


def _frame_action_text(fr) -> str:
    attrs = getattr(fr, "attrs", None) or {}
    if not isinstance(attrs, dict):
        return ""
    return str(
        attrs.get("shot01_action")
        or attrs.get("main_action")
        or attrs.get("действие")
        or ""
    ).strip()


def _frame_prompts_and_action_ready(fr) -> bool:
    from app.services.vo_shot_expand import is_shot_child

    if is_shot_child(fr):
        return True
    img = (getattr(fr, "image_prompt", None) or "").strip()
    anim = (getattr(fr, "animation_prompt", None) or "").strip()
    return bool(img and anim and _frame_action_text(fr))


def _frame_camera_menu_ready(fr) -> bool:
    from app.services.vo_shot_expand import is_shot_child

    if is_shot_child(fr):
        return True
    attrs = getattr(fr, "attrs", None) or {}
    if isinstance(fr, dict):
        attrs = fr.get("attrs") if isinstance(fr.get("attrs"), dict) else {}
        cs = fr.get("camera_subdivide") if isinstance(fr.get("camera_subdivide"), dict) else {}
    else:
        cs = attrs.get("camera_subdivide") if isinstance(attrs, dict) else {}
    if not isinstance(cs, dict):
        cs = {}
    if isinstance(attrs, dict) and isinstance(attrs.get("camera_subdivide"), dict):
        cs = {**attrs["camera_subdivide"], **cs}
    size = str(cs.get("крупность") or cs.get("size") or "").strip()
    move = str(cs.get("движение") or cs.get("move") or "").strip()
    nab = str(cs.get("набор") or cs.get("set") or "").strip()
    return bool(size and move and nab)


def _all_frame_prompts_ready(frames) -> bool:
    """True если у каждого кадра image+anim+действие (QC можно не ждать GPT)."""
    rows = list(frames or [])
    if len(rows) < 2:
        return False
    return all(_frame_prompts_and_action_ready(fr) for fr in rows)


def _excel_gpt_ui_force_full(project) -> bool:
    """Явный ▶ в Studio: эту ноду (и auto-chain хвост) нельзя skip-filled."""
    meta = getattr(project, "meta", None)
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("excel_gpt_ui_force_full"))


def _clear_excel_gpt_ui_force_full(project) -> bool:
    meta = dict(getattr(project, "meta", None) or {})
    changed = False
    for key in ("excel_gpt_ui_force_full", "excel_gpt_force_full_rerun"):
        if key in meta:
            meta.pop(key, None)
            changed = True
    if changed:
        project.meta = meta
    return changed


def _select_fw_frames_for_gpt(frames_for_map, *, force_full: bool):
    """Какие кадры слать в GPT на fw_frames. force_full — все uuid, без skip."""
    from app.services.vo_shot_expand import is_shot_child

    uuid_frames = [
        fr
        for fr in (frames_for_map or [])
        if getattr(fr, "uuid", None) and not is_shot_child(fr)
    ]
    if force_full:
        return list(uuid_frames), False
    prompt_pending = [
        fr for fr in uuid_frames if not _frame_prompts_and_action_ready(fr)
    ]
    camera_pending = [
        fr for fr in uuid_frames if not _frame_camera_menu_ready(fr)
    ]
    if prompt_pending:
        return prompt_pending, False
    if camera_pending:
        return camera_pending, True
    return [], False


def _should_skip_qc_gpt(frames_for_map, *, force_full: bool) -> bool:
    return (not force_full) and _all_frame_prompts_ready(frames_for_map)

# Маппинг slot_idx (1..5) → (running_status, ready_status, step_code).
_SLOT_MAP: dict[int, tuple[ProjectStatus, ProjectStatus, str]] = {
    1: (ProjectStatus.enriching_1, ProjectStatus.enrich_1_ready, "enrich_1"),
    2: (ProjectStatus.enriching_2, ProjectStatus.enrich_2_ready, "enrich_2"),
    3: (ProjectStatus.enriching_3, ProjectStatus.enrich_3_ready, "enrich_3"),
    4: (ProjectStatus.enriching_4, ProjectStatus.enrich_4_ready, "enrich_4"),
    5: (ProjectStatus.enriching_5, ProjectStatus.enrich_5_ready, "enrich_5"),
}

def _check_mode_input_paths(
    data_paths: list[Path], db_check_path: Path
) -> list[Path]:
    """checkMode DB SoT: только db_check.json + картинки, без leftover JSON/txt."""
    keep_img = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    out: list[Path] = [db_check_path]
    seen = {db_check_path.resolve()}
    for p in data_paths:
        try:
            rp = p.resolve()
        except OSError:
            rp = p
        if rp in seen:
            continue
        if p.suffix.lower() in keep_img:
            out.append(p)
            seen.add(rp)
    return out


def _persist_excel_gpt_reply_for_ui(
    project: Project,
    node_key: str | None,
    *,
    data_paths: list[Path],
    api_res: object,
) -> None:
    """Сохранить ответ GPT в meta/диск до raise — иначе UI без результата."""
    if not node_key:
        return
    try:
        from app.services.gpt_operator import save_operator_result

        reply = str(getattr(api_res, "reply_text", "") or "")
        outs = list(getattr(api_res, "output_paths", None) or [])
        save_operator_result(
            project,
            node_key,
            input_paths=list(data_paths or []),
            output_paths=outs,
            reply_text=reply,
            gate_status=getattr(api_res, "gate_status", None),
            analysis=(
                api_res.analysis.to_dict()  # type: ignore[attr-defined]
                if getattr(api_res, "analysis", None) is not None
                else None
            ),
        )
        if reply.strip():
            save_gpt_reply_text(project, node_key, reply)
    except Exception:  # noqa: BLE001
        logger.exception(
            "[#{}] enrich_xlsx: не удалось сохранить reply для UI (node={})",
            project.id,
            node_key,
        )


def _apply_enrich_ready_status(
    project: Project,
    *,
    running_status: ProjectStatus,
    ready_status: ProjectStatus,
) -> bool:
    """Ставит enrich_N_ready, не откатывая media/более поздний шаг.

    API-путь раньше всегда писал ready_status и затирал generating_videos,
    если video/run стартовал параллельно с долгим checkMode excel_gpt.
    """
    from app.telegram.menu import status_order as _ord

    cur = project.status
    if cur is running_status or _ord(cur) < _ord(ready_status):
        project.status = ready_status
        return True
    logger.warning(
        "[#{}] enrich_xlsx: skip status → {} (project already at {})",
        project.id,
        ready_status.value,
        cur.value if cur is not None else None,
    )
    return False


async def _harness_before_enrich_ready(
    session: AsyncSession, project: Project, *, check_mode: bool
) -> None:
    """NODE_SYSTEM: excel_gpt gate before enrich_N_ready / node done. Skip checkMode."""
    if check_mode:
        return
    from app.services.agent_harness import harness_gate_or_raise

    await harness_gate_or_raise(session, project, step="excel_gpt")


async def _after_excel_gpt_done(
    session: AsyncSession,
    project: Project,
    *,
    node_key: str | None,
    slot_idx: int,
    ready_status: ProjectStatus,
) -> None:
    """После готовности excel_gpt: sync storage; цепочка — только при auto_mode."""
    if node_key:
        try:
            from app.services.storage_node import sync_downstream_storage_from_node

            synced = sync_downstream_storage_from_node(project, node_key)
            for info in synced:
                skipped = info.get("skipped") or []
                logger.info(
                    "[#{}] enrich_xlsx: storage {} ← {} copied={} skipped={} errors={}",
                    project.id,
                    info.get("storageNode"),
                    node_key,
                    len(info.get("copied") or []),
                    len(skipped),
                    info.get("errors") or [],
                )
                if not (info.get("copied") or []) and skipped:
                    logger.warning(
                        "[#{}] enrich_xlsx: storage {} пусто после sync — причины: {}",
                        project.id,
                        info.get("storageNode"),
                        "; ".join(str(x) for x in skipped[:12]),
                    )
            if synced:
                from sqlalchemy.orm.attributes import flag_modified

                flag_modified(project, "meta")
                await session.flush()
        except Exception:  # noqa: BLE001
            logger.exception(
                "[#{}] enrich_xlsx: sync downstream storage failed node={}",
                project.id,
                node_key,
            )

    # checkMode после hero/scenes/videos: fail → db_patch + точечный переген → снова check.
    # Работает и без auto_mode (воркер подхватит generating_*).
    if node_key:
        try:
            from app.services.vision_check_loop import (
                maybe_start_vision_check_loop_after_check,
            )

            started = await maybe_start_vision_check_loop_after_check(
                session, project, node_key
            )
            if started:
                return
        except Exception:  # noqa: BLE001
            logger.exception(
                "[#{}] enrich_xlsx: vision_check_loop after {} failed",
                project.id,
                node_key,
            )

    # Без автопродвижения — остаёмся на enrich_N_ready, следующую ноду
    # пользователь запускает вручную ▶. Воркер сам вызовет maybe_auto_advance
    # только если auto_mode=True.
    if not getattr(project, "auto_mode", False):
        if _clear_excel_gpt_ui_force_full(project):
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(project, "meta")
        logger.info(
            "[#{}] enrich_xlsx: slot {} → {} — auto_mode выкл, без auto-chain/advance",
            project.id,
            slot_idx,
            ready_status.value,
        )
        return

    await _maybe_auto_chain_excel_gpt(
        session,
        project,
        slot_idx,
        ready_status,
        finished_key=node_key,
    )


async def _maybe_auto_chain_excel_gpt(
    session: AsyncSession,
    project: Project,
    slot_idx: int,
    ready_status: ProjectStatus,
    *,
    finished_key: str | None = None,
) -> None:
    """После enrich_N_ready: прыжок на следующий excel_gpt ТОЛЬКО по стрелкам.

    Если следующий work — script/hero/другое, не трогаем status (auto_advance
    пойдёт по графу). Orphan-слоты на канвасе без входящей стрелки не запускаем.

    Активную ноду берём по id со стрелки, не через resolve(next_slot) — иначе
    при коллизии slotIndex (6+ excel_gpt / лимит 5) стартует чужая нода
    (проверка пропускается, «персонажи» стартуют раньше).
    """
    from app.services.excel_gpt_node import (
        clear_excel_gpt_tail_completion,
        ensure_enrich_auto_chain_to,
        first_work_successor_from_excel_slot,
        is_excel_gpt_node_type,
    )

    finished_key = (finished_key or "").strip() or None
    succ = first_work_successor_from_excel_slot(
        project, slot_idx, from_key=finished_key
    )
    if succ is None:
        meta = dict(project.meta or {})
        meta.pop("enrich_auto_chain_to", None)
        meta.pop("excel_gpt_ui_force_full", None)
        meta.pop("excel_gpt_force_full_rerun", None)
        project.meta = meta
        await session.flush()
        return
    next_key, typ, next_slot = succ
    if not is_excel_gpt_node_type(typ):
        # Стрелка ведёт не в excel_gpt — цепочку не форсим.
        meta = dict(project.meta or {})
        meta.pop("enrich_auto_chain_to", None)
        meta.pop("excel_gpt_ui_force_full", None)
        meta.pop("excel_gpt_force_full_rerun", None)
        project.meta = meta
        await session.flush()
        logger.info(
            "[#{}] enrich_xlsx: после slot {} / {} следующий work={} — без auto-chain",
            project.id,
            slot_idx,
            finished_key or "?",
            typ or "?",
        )
        return
    if not next_key or next_key == (finished_key or ""):
        return

    from app.services.excel_gpt_node import running_status_for_slot
    from app.services.run_sync import prepare_node_for_step_start

    overflow_next = next_slot is None or next_slot < 1
    if overflow_next:
        # fw-группа: все ноды slotIndex=0, бегут как enriching_1.
        # Не прыгать на slot+1 — startup guard откатывает enriching_2 → ▶.
        next_running = running_status_for_slot(0)
        enrich_slot = 1
        chain_to = None
    else:
        if next_slot not in _SLOT_MAP:
            return
        ensure_enrich_auto_chain_to(project, slot_idx, from_key=finished_key)
        clear_excel_gpt_tail_completion(project, next_slot)
        next_running = _SLOT_MAP[next_slot][0]
        enrich_slot = next_slot
        meta_chain = dict(project.meta or {})
        chain_to = meta_chain.get("enrich_auto_chain_to")

    meta = dict(project.meta or {})
    if overflow_next:
        keys = [str(k) for k in (meta.get("excel_gpt_completed_keys") or [])]
        if next_key in keys:
            meta["excel_gpt_completed_keys"] = [k for k in keys if k != next_key]
    meta["active_excel_gpt_node_key"] = next_key
    project.meta = meta
    try:
        await prepare_node_for_step_start(
            session,
            project,
            EXCEL_GPT_STEP_CODE,
            node_key=next_key,
            enrich_slot=enrich_slot,
            strict=False,
            explicit_ui_start=True,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[#{}] enrich_xlsx auto-chain: prepare NodeRun {} failed",
            project.id,
            next_key,
            exc_info=True,
        )
    project.status = next_running
    await session.flush()
    logger.info(
        "[#{}] enrich_xlsx auto-chain: {} → {} (next slot #{}, chain_to={}, node={})",
        project.id,
        ready_status.value,
        next_running.value,
        next_slot,
        chain_to,
        next_key,
    )


def _resolve_slot_idx(status: ProjectStatus) -> int | None:
    """Из running-статуса (enriching_N) достаём slot_idx."""
    for idx, (running, _ready, _code) in _SLOT_MAP.items():
        if status is running:
            return idx
    return None


def _get_accompanying_text(project: Project, step_code: str) -> str:
    text = gtb.get_effective_text(project, EXCEL_GPT_STEP_CODE)
    if text.strip():
        return text
    return gtb.get_effective_text(project, step_code)


async def run(session: AsyncSession, project: Project, bot: Bot) -> None:
    slot_idx = _resolve_slot_idx(project.status)
    if slot_idx is None:
        logger.warning(
            "[#{}] enrich_xlsx.run: статус {} не соответствует ни одному слоту",
            project.id,
            project.status.value,
        )
        return
    running_status, ready_status, legacy_step_code = _SLOT_MAP[slot_idx]
    from app.services.excel_gpt_node import resolve_excel_gpt_node_key_for_slot

    node_key = active_node_key(project)
    if not node_key:
        node_key = resolve_excel_gpt_node_key_for_slot(project, slot_idx)
    if not node_key:
        from app.orchestrator.graph.planner import load_graph_for_project
        from app.services.excel_gpt_node import EXCEL_GPT_NODE_TYPE, slot_index_from_node

        graph = await load_graph_for_project(session, project)
        for nid, n in graph._by_id.items():
            if str(n.get("type") or "") == EXCEL_GPT_NODE_TYPE and slot_index_from_node(n) == slot_idx:
                node_key = nid
                break
    if node_key:
        meta = dict(project.meta or {})
        if meta.get("active_excel_gpt_node_key") != node_key:
            meta["active_excel_gpt_node_key"] = node_key
            project.meta = meta
            await session.flush()
    prompt_step_code = EXCEL_GPT_STEP_CODE
    # Слот «main» в Node Studio → meta.prompt_slot_variants[node_key]["main"].
    # Без node_key все excel_gpt брали один prompt_overrides["excel_gpt"].
    prompt_slot_id = "main"
    logger.info(
        "[#{}] enrich_xlsx slot={} node={} prompt={} starting",
        project.id,
        slot_idx,
        node_key,
        prompt_step_code,
    )

    await session.refresh(project)

    sheet = _sheet_for_project(project)
    xlsx_path: Path = sheet.ensure_initialized(
        project_id=project.id, slug=project.slug
    )

    from app.services.gpt_operator import (
        operator_config,
        resolve_operator,
        save_operator_result,
    )

    op_cfg = operator_config(project, node_key) if node_key else {}
    resolved = resolve_operator(project, node_key) if node_key else None
    explicit = str(op_cfg.get("transport") or "").strip().lower()
    if explicit == "browser":
        raise RuntimeError(
            "enrich_xlsx: transport=browser отключён. "
            "Используй transport=api (GPT HTTP). Убери browser в настройках ноды."
        )
    transport = "api"

    try:
        variant, src_path, master, prompt_source = read_resolved_project_prompt(
            project,
            prompt_step_code,
            node_key=node_key,
            slot_id=prompt_slot_id if node_key else None,
        )
    except FileNotFoundError:
        variant = "default"
        src_path = None
        prompt_source = "default"
        master = (
            f"# {prompt_step_code}\n\n"
            "Мастер-промт для доп. работы с Excel ещё не настроен. "
            "Открой `prompts/05_excel_gpt/default.md` и опиши там, "
            "что именно ChatGPT должен изменить в приложенном файле."
        )
        logger.warning(
            "[#{}] enrich_xlsx slot={}: файл промта не найден, fallback текст",
            project.id,
            slot_idx,
        )
    else:
        logger.info(
            "[#{}] enrich_xlsx slot={} node={!r}: активный промт variant={!r} "
            "source={} path={} ({} симв) overrides={!r}",
            project.id,
            slot_idx,
            node_key,
            variant,
            prompt_source,
            src_path,
            len(master or ""),
            (getattr(project, "prompt_overrides", None) or {}).get(prompt_step_code),
        )

    accompanying = _get_accompanying_text(project, legacy_step_code)

    # Проверочные роли / тумблер «Проверка».
    branching_role = False
    check_mode = False
    check_fix = True
    check_prompt_source = "upstream"
    source_prompt_keys: list[str] = []
    db_sot_check = False
    if node_key and resolved is not None:
        role_name = str(resolved.get("role") or "")
        check_mode = bool(resolved.get("checkMode") or op_cfg.get("checkMode"))
        check_fix = bool(resolved.get("checkFix", op_cfg.get("checkFix", True)))
        check_prompt_source = str(
            resolved.get("checkPromptSource")
            or op_cfg.get("checkPromptSource")
            or "upstream"
        ).strip().lower()
        if check_prompt_source not in ("upstream", "agent"):
            check_prompt_source = "upstream"
        branching_role = role_name in ("review", "gate", "compare") or check_mode
        if check_mode:
            from app.services.gpt_operator import (
                assemble_check_agent_prompt,
                assemble_check_master_prompt,
                collect_source_prompts,
                project_format_hint_for_check,
                sanitize_check_reviewer_notes,
            )

            reviewer_notes = sanitize_check_reviewer_notes(accompanying or "")
            from app.services.gpt_operator import resolve_check_report_format

            report_fmt, _ = resolve_check_report_format(project, node_key)
            from app.services.gpt_operator import append_vision_hint_for_upstream

            # Нет картинок на входе → проверка по DB, не по Excel/TSV.
            _img_ext = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
            _has_vision = any(
                Path(str(f.get("name") or f.get("path") or "")).suffix.lower()
                in _img_ext
                for f in (resolved.get("files") or [])
                if f.get("ok")
            )
            db_sot_check = not _has_vision

            if check_prompt_source == "agent":
                master, agent_step = assemble_check_agent_prompt(
                    project,
                    node_key,
                    check_fix=check_fix,
                    reviewer_notes=reviewer_notes,
                    report_format=report_fmt,
                    db_sot=db_sot_check,
                )
                master = append_vision_hint_for_upstream(project, node_key, master)
                source_prompt_keys = [f"agent:{agent_step}"] if agent_step else ["agent"]
                accompanying = ""
                hint = project_format_hint_for_check(project, node_key)
                if hint:
                    accompanying = hint
                logger.info(
                    "[#{}] enrich_xlsx node={!r}: checkMode agent={} "
                    "db_sot={} chars={}",
                    project.id,
                    node_key,
                    agent_step,
                    db_sot_check,
                    len(master),
                )
            else:
                sources = collect_source_prompts(project, node_key)
                ok_sources = [s for s in sources if s.get("ok")]
                if not ok_sources:
                    raise RuntimeError("нет исходного промта для проверки")
                source_prompt_keys = [str(s.get("nodeKey") or "") for s in ok_sources]
                # Короткий текст — только доп. указания; старый vp.check.v1 JSON выкидываем.
                master = assemble_check_master_prompt(
                    ok_sources,
                    check_fix=check_fix,
                    reviewer_notes=reviewer_notes,
                    report_format=report_fmt,
                    db_sot=db_sot_check,
                )
                master = append_vision_hint_for_upstream(project, node_key, master)
                accompanying = ""
                hint = project_format_hint_for_check(project, node_key)
                if hint:
                    accompanying = hint
                logger.info(
                    "[#{}] enrich_xlsx node={!r}: checkMode sources={} "
                    "db_sot={} chars={}",
                    project.id,
                    node_key,
                    source_prompt_keys,
                    db_sot_check,
                    len(master),
                )
        elif branching_role:
            from app.services.check_analysis import append_response_footer
            from app.services.gpt_operator import (
                default_check_prompt_for_node,
                project_format_hint_for_check,
            )

            # Нет кастомного промта у ноды (source=default) → берём
            # универсальный агент-проверки по типу вышестоящей рабочей ноды.
            if str(prompt_source or "") == "default" or not (master or "").strip():
                agent = default_check_prompt_for_node(project, node_key)
                if agent:
                    master = agent
                    logger.info(
                        "[#{}] enrich_xlsx node={!r}: универсальный агент-проверки "
                        "по вышестоящей ноде ({} симв)",
                        project.id,
                        node_key,
                        len(master),
                    )

            # Целевой формат проекта в контекст — не статично, из полей проекта.
            hint = project_format_hint_for_check(project, node_key)
            if hint and hint not in (accompanying or ""):
                accompanying = (
                    f"{accompanying}\n\n{hint}".strip() if accompanying else hint
                )

            master = append_response_footer(master or "")

    # ── API-транспорт (единственный): без браузера/CDP ─────────────────
    if node_key and resolved is not None:
        from sqlalchemy.orm.attributes import flag_modified

        from app.services.gpt_operator_client import run_operator_api
        from app.services.step_cancel import raise_if_cancelled

        if not resolved.get("canRun"):
            errs = "; ".join(resolved.get("errors") or ["входы не согласованы"])
            raise RuntimeError(f"gpt-operator resolve: {errs}")

        data_paths = [
            Path(str(f["path"]))
            for f in (resolved.get("files") or [])
            if f.get("ok") and f.get("path")
        ]
        # project_file / scene_grammar / character_registry = DB SoT.
        output_mode_early = (
            "text" if check_mode else str(resolved.get("outputMode") or "text")
        )
        scene_grammar_early = (not check_mode) and _is_scene_grammar_prompt(
            variant, master
        )
        character_registry_early = (not check_mode) and _is_character_registry_prompt(
            variant, master
        )
        if (
            not data_paths
            and output_mode_early != "project_file"
            and not scene_grammar_early
            and not character_registry_early
            and not (check_mode and db_sot_check)
        ):
            raise RuntimeError("gpt-operator: нет существующих файлов на входе")

        check_known_uuids: list[str] = []
        check_number_to_uuid: dict[int, str] = {}
        if check_mode and node_key and db_sot_check:
            import json as _json

            from sqlalchemy import select as _select

            from app.models import Entity
            from app.services import db_v2

            await db_v2.backfill_project_v2(session, project)
            frames_chk = list(
                (
                    await session.execute(
                        _select(Frame)
                        .where(Frame.project_id == project.id)
                        .order_by(Frame.number)
                    )
                ).scalars().all()
            )
            ents = list(
                (
                    await session.execute(
                        _select(Entity).where(Entity.project_id == project.id)
                    )
                ).scalars().all()
            )
            meta = project.meta if isinstance(project.meta, dict) else {}
            from app.services.db_frames_context import build_excel_gpt_check_context

            db_check = build_excel_gpt_check_context(
                project_id=project.id,
                slug=project.slug,
                frames=frames_chk,
                characters=[
                    {
                        "id": e.code or e.name,
                        "имя": e.name,
                        "type": e.type,
                        "attrs": e.attrs or {},
                    }
                    for e in ents
                ],
                scene_registry=meta.get("scene_registry") or [],
            )
            ctx_dir = project.data_dir / "excel_gpt_uploads" / str(node_key)
            ctx_dir.mkdir(parents=True, exist_ok=True)
            db_check_path = ctx_dir / "db_check.json"
            db_check_path.write_text(
                _json.dumps(db_check, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            # Только DB. Excel/TSV и leftover батчи/gpt_reply со стрелок — нет.
            data_paths = _check_mode_input_paths(data_paths, db_check_path)
            accompanying = (
                f"{accompanying}\n\n"
                "# DB SoT CHECK\n"
                "Вложение db_check.json — кадры (uuid+attrs), scene_registry, "
                "characters. Excel не существует для этой проверки. "
                "Не пиши findings про TSV / project.xlsx / # Лист:."
            ).strip()
            logger.info(
                "[#{}] enrich_xlsx node={!r}: checkMode DB SoT — "
                "db_check.json frames={} scenes={} files={}",
                project.id,
                node_key,
                len(db_check["frames"]),
                len(db_check["scene_registry"] or []),
                [p.name for p in data_paths],
            )
            check_known_uuids = [
                str(fr.uuid or "").strip()
                for fr in frames_chk
                if str(fr.uuid or "").strip()
            ]
            for fr in frames_chk:
                n = getattr(fr, "number", None)
                uid = str(fr.uuid or "").strip()
                if n is None or not uid:
                    continue
                try:
                    ni = int(n)
                except (TypeError, ValueError):
                    continue
                check_number_to_uuid.setdefault(ni, uid)

        if check_mode and node_key:
            from app.services.vision_check_db import build_vision_db_snapshot
            from app.services.vision_check_loop import (
                filter_image_paths_for_recheck,
                get_vision_kind,
            )

            data_paths = filter_image_paths_for_recheck(project, data_paths)
            try:
                from app.services.gpt_operator import upstream_node_type_for_check

                up = upstream_node_type_for_check(project, node_key)
                kind_hint = get_vision_kind(project)
                if not kind_hint:
                    if up == "hero":
                        kind_hint = "hero"
                    elif up in ("images", "hitl_images", "image_prompts"):
                        kind_hint = "scenes"
                    elif up in ("videos", "hitl_videos", "animation_prompts"):
                        kind_hint = "videos"
                # videos/*.mp4 → сетка 3×2 (6 stills) для GPT vision
                if kind_hint == "videos" or up in (
                    "videos",
                    "hitl_videos",
                ):
                    from app.services.video_sheet import (
                        is_video_path,
                        materialize_video_sheets_for_check,
                    )

                    if any(is_video_path(p) for p in data_paths):
                        sheets_dir = project.data_dir / "tmp_video_sheets"
                        data_paths = await materialize_video_sheets_for_check(
                            data_paths, sheets_dir
                        )
                        kind_hint = "videos"
                        logger.info(
                            "[#{}] enrich_xlsx: video→sheet 3×2 → {} файлов",
                            project.id,
                            len(data_paths),
                        )
                db_snap = await build_vision_db_snapshot(
                    session,
                    project,
                    image_paths=data_paths,
                    kind=kind_hint,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[#{}] enrich_xlsx: vision DB snapshot failed",
                    project.id,
                )
                db_snap = ""
            if db_snap:
                accompanying = (
                    f"{accompanying}\n\n{db_snap}".strip()
                    if accompanying
                    else db_snap
                )

        role = str(resolved.get("role") or "assist")
        output_mode = "text" if check_mode else str(resolved.get("outputMode") or "text")
        scene_grammar = (not check_mode) and _is_scene_grammar_prompt(variant, master)
        character_registry = (not check_mode) and _is_character_registry_prompt(
            variant, master
        )
        script_writer = (not check_mode) and _is_script_writer_node(
            variant, master, node_key
        )
        main_action_node = (not check_mode) and _is_main_action_node(
            variant, master, node_key
        )
        scenes_to_frames = (not check_mode) and _is_scenes_to_frames_node(
            variant, master, node_key
        )
        if script_writer and output_mode != "project_file":
            logger.info(
                "[#{}] enrich_xlsx node={!r}: script_writer → force "
                "outputMode=project_file (было {!r})",
                project.id,
                node_key,
                output_mode,
            )
            output_mode = "project_file"
        if scene_grammar and output_mode != "project_file":
            logger.info(
                "[#{}] enrich_xlsx node={!r}: scene_grammar → force "
                "outputMode=project_file (было {!r})",
                project.id,
                node_key,
                output_mode,
            )
            output_mode = "project_file"
        if character_registry and output_mode != "project_file":
            logger.info(
                "[#{}] enrich_xlsx node={!r}: character_registry → force "
                "outputMode=project_file (было {!r})",
                project.id,
                node_key,
                output_mode,
            )
            output_mode = "project_file"
        logger.info(
            "[#{}] enrich_xlsx API transport slot={} role={} checkMode={} "
            "output={} files={}",
            project.id,
            slot_idx,
            role,
            check_mode,
            output_mode,
            [p.name for p in data_paths],
        )
        raise_if_cancelled(project.id)
        expected_frame_uuids: list[str] = []
        db_ctx: dict | None = None
        ctx_path = None
        fw_camera_menu_only = False
        force_full = True
        if _excel_gpt_ui_force_full(project):
            logger.info(
                "[#{}] enrich_xlsx node={!r}: UI ▶ — полный GPT, без skip filled",
                project.id,
                node_key,
            )
        # DB SoT: для project_file просим apply-ops JSON вместо TSV (+ uuid-мап).
        if output_mode == "project_file" and not check_mode:
            import json as _json

            from sqlalchemy import select as _select

            from app.models import Entity
            from app.services import db_apply, db_v2
            from app.services.db_frames_context import build_excel_gpt_db_context
            from app.services.excel_characters import entity_cards_for_gpt

            await db_v2.backfill_project_v2(session, project)
            frames_for_map = list(
                (
                    await session.execute(
                        _select(Frame)
                        .where(Frame.project_id == project.id)
                        .order_by(Frame.sort_key, Frame.number)
                    )
                ).scalars().all()
            )
            if str(node_key or "").endswith("_fw_frames"):
                from app.services.vo_shot_expand import expand_vo_cells_into_shots

                frames_for_map, exp = await expand_vo_cells_into_shots(
                    session, project, frames_for_map
                )
                await session.flush()
                logger.info(
                    "[#{}] fw_frames vo_shot_expand {}",
                    project.id,
                    exp,
                )
            ents = list(
                (
                    await session.execute(
                        _select(Entity).where(Entity.project_id == project.id)
                    )
                ).scalars().all()
            )
            # Компактный снимок DB (SoT). Excel в GPT не отдаём вообще.
            # scene_grammar: attrs не тащим — пишем с нуля.
            # character_registry: voiceover + текущие персонажи кадра + Entity.
            # excel_gpt else: slim whitelist + короткий VO, без сырого attrs.
            if scene_grammar or character_registry:
                frame_rows: list[dict] = []
                for fr in frames_for_map:
                    if not fr.uuid:
                        continue
                    row: dict = {
                        "number": fr.number,
                        "uuid": fr.uuid,
                        "voiceover_text": fr.voiceover_text or "",
                        "meaning": fr.meaning or "",
                    }
                    if character_registry:
                        attrs = fr.attrs or {}
                        row["персонажи"] = str(
                            attrs.get("characters")
                            or attrs.get("персонажи")
                            or attrs.get("persons")
                            or ""
                        )
                    frame_rows.append(row)
                db_ctx: dict = {
                    "source": "db_v2",
                    "project_id": project.id,
                    "slug": project.slug,
                    "frames": frame_rows,
                    "characters": entity_cards_for_gpt(ents),
                }
            else:
                gpt_frames = list(frames_for_map)
                vo_markup = _is_vo_cell_markup_node(variant, master, node_key)
                if vo_markup:
                    from app.services.db_frames_context import (
                        collapse_script_writer_frames,
                    )

                    collapsed = collapse_script_writer_frames(frames_for_map)
                    parent_ids = {
                        str(r.get("uuid") or "").strip()
                        for r in collapsed
                        if str(r.get("uuid") or "").strip()
                    }
                    if parent_ids:
                        gpt_frames = [
                            fr
                            for fr in frames_for_map
                            if str(getattr(fr, "uuid", "") or "") in parent_ids
                        ]
                if str(node_key or "").endswith("_fw_frames"):
                    gpt_frames, fw_camera_menu_only = _select_fw_frames_for_gpt(
                        frames_for_map, force_full=force_full
                    )
                    logger.info(
                        "[#{}] fw_frames GPT payload {}/{} "
                        "({}{})",
                        project.id,
                        len(gpt_frames),
                        len(frames_for_map),
                        (
                            "force_full rewrite image+anim+action, strip old prompts from payload"
                            if force_full
                            else "skip filled image+anim+action"
                        ),
                        "; camera_menu only" if fw_camera_menu_only else "",
                    )
                elif (
                    _is_qc_prompts_prompt(variant, master)
                    or str(node_key or "").endswith("_fw_qc")
                ):
                    from app.services.vo_shot_expand import is_shot_child

                    gpt_frames = [
                        fr for fr in gpt_frames if not is_shot_child(fr)
                    ]
                db_ctx = build_excel_gpt_db_context(
                    project_id=project.id,
                    slug=project.slug,
                    frames=gpt_frames,
                    characters=entity_cards_for_gpt(ents),
                    strip_prompts=bool(force_full),
                    full_vo=vo_markup,
                )
                if vo_markup:
                    overlay = {
                        str(r.get("uuid") or "").strip(): r
                        for r in collapse_script_writer_frames(frames_for_map)
                        if str(r.get("uuid") or "").strip()
                    }
                    for row in db_ctx.get("frames") or []:
                        src = overlay.get(str(row.get("uuid") or "").strip())
                        if src and src.get("voiceover_text"):
                            row["voiceover_text"] = src["voiceover_text"]
            ctx_dir = project.data_dir / "excel_gpt_uploads" / str(node_key)
            ctx_dir.mkdir(parents=True, exist_ok=True)
            ctx_path = ctx_dir / "db_frames.json"
            ctx_path.write_text(
                _json.dumps(db_ctx, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            expected_frame_uuids = [
                str(row.get("uuid") or "").strip()
                for row in (db_ctx.get("frames") or [])
                if str(row.get("uuid") or "").strip()
            ]
            # Только DB-снимок. Никакого project.xlsx / check_report / мусора
            # со стрелок — иначе модель снова думает про Excel.
            data_paths = [ctx_path]
            logger.info(
                "[#{}] enrich_xlsx node={!r}: project_file/DB SoT — "
                "только db_frames.json frames={} characters={} "
                "payload_image_prompt={} payload_vo={}",
                project.id,
                node_key,
                len(db_ctx["frames"]),
                len(db_ctx.get("characters") or []),
                sum(
                    1
                    for row in (db_ctx.get("frames") or [])
                    if str(row.get("image_prompt") or "").strip()
                ),
                sum(
                    1
                    for row in (db_ctx.get("frames") or [])
                    if str(row.get("voiceover_text") or row.get("vo_shot") or "").strip()
                ),
            )
            if scene_grammar:
                hint = _SCENE_GRAMMAR_APPLY_HINT
            elif character_registry:
                hint = _CHARACTER_REGISTRY_APPLY_HINT
            else:
                hint = _APPLY_OPS_HINT
            if scene_grammar:
                accompanying = (
                    f"{accompanying}\n\n{hint}\n"
                    "# DB SoT\n"
                    "db_frames.json — куски закадра (якоря границ). "
                    "Не плоди ops по числу uuid. Пиши characters + scenes "
                    "(полный паспорт + shots[]). ops=[] допустим. "
                    "Пайплайн сам привяжет сцены к кадрам по словам."
                ).strip()
                vo_chunks = [
                    (fr.voiceover_text or "").strip()
                    for fr in frames_for_map
                    if (fr.voiceover_text or "").strip()
                ]
                full_vo = (project.script_text or "").strip() or " ".join(vo_chunks)
                if full_vo:
                    accompanying = (
                        f"{accompanying}\n\n"
                        f"# ПОЛНЫЙ ЗАКАДР (для start_words/end_words)\n"
                        f"{full_vo}"
                    ).strip()
            elif character_registry:
                uuid_frames = [f for f in frames_for_map if f.uuid]
                mapping = ""
                if uuid_frames:
                    mapping = "\n".join(
                        f"кадр {f.number} = {f.uuid}" for f in uuid_frames
                    )
                accompanying = (
                    f"{accompanying}\n\n{hint}\n"
                    f"{mapping}\n"
                    "# DB SoT\n"
                    "db_frames.json: frames[].voiceover_text + персонажи, "
                    "characters[] = текущий Entity (может быть []). "
                    "Пустой реестр — норма: создай characters с нуля. "
                    "Пайплайн запишет JSON в Entity + attrs. Excel нет."
                ).strip()
                vo_chunks = [
                    (fr.voiceover_text or "").strip()
                    for fr in frames_for_map
                    if (fr.voiceover_text or "").strip()
                ]
                full_vo = (project.script_text or "").strip() or " ".join(vo_chunks)
                if full_vo:
                    accompanying = (
                        f"{accompanying}\n\n"
                        f"# ПОЛНЫЙ ЗАКАДР\n{full_vo}"
                    ).strip()
            else:
                uuid_frames = [f for f in frames_for_map if f.uuid]
                mapping = ""
                if uuid_frames:
                    mapping = "\n".join(
                        f"кадр {f.number} = {f.uuid}" for f in uuid_frames
                    )
                accompanying = (
                    f"{accompanying}\n\n{hint}{mapping}"
                ).strip()
                if script_writer:
                    topic_bits = _project_topic_bits(project)
                    if topic_bits:
                        accompanying = (
                            f"{accompanying}\n\n"
                            "# ТЕМА / ПЛАН РОЛИКА (контекст, не для переписывания)\n"
                            + "\n\n".join(topic_bits)
                        ).strip()
                    accompanying = (
                        f"{accompanying}\n\n"
                        "# DB SoT\n"
                        "Файл db_frames.json — VO-ячейки из базы (uuid + number + "
                        "voiceover_text). voiceover_text — ГОТОВЫЙ закадр: "
                        "не пиши, не меняй, не сокращай его. Твоя единственная "
                        "работа — разметить в нём биты. "
                        "Пайплайн сам запишет твой JSON в базу. "
                        "Excel не используется. Отвечай только JSON apply-ops."
                    ).strip()
                elif main_action_node:
                    accompanying = (
                        f"{accompanying}\n\n"
                        "# DB SoT\n"
                        "Файл db_frames.json — VO-ячейки (uuid + voiceover_text + биты). "
                        "Пиши только fields.главное_действие. Даже одна строка закадра "
                        "= «1. место — действие» и следующая строка (весь кусок). "
                        "Слоган без номера = брак. Не пиши закадр и биты."
                    ).strip()
                elif scenes_to_frames:
                    accompanying = (
                        f"{accompanying}\n\n"
                        "# DB SoT\n"
                        "Файл db_frames.json — VO-ячейки (uuid + voiceover_text + "
                        "главное_действие). Пиши только fields.кадры по логике "
                        "этой ячейки: одно место и несколько кадров → parent = "
                        "первый кадр локации; новое место → parent_id null. "
                        "Не копируй чужой сюжет. Не плоди планы ради схемы. "
                        "Не пиши закадр, биты, главное_действие."
                    ).strip()
                elif _is_frame_prompts_prompt(variant, master) or str(
                    node_key or ""
                ).endswith("_fw_frames"):
                    accompanying = (
                        f"{accompanying}\n\n"
                        "# DB SoT\n"
                        "Файл db_frames.json — родительские кадры (uuid + voiceover). "
                        "Пиши только промт_картинки, промт_видео, действие, "
                        "крупность, движение, набор; дети покрытия — в "
                        "промты_детей / промт_картинки_2. "
                        "кадры[] во входе — только читать, не возвращай fields.кадры."
                    ).strip()
                else:
                    accompanying = (
                        f"{accompanying}\n\n"
                        "# DB SoT\n"
                        "Файл db_frames.json — кадры из базы (uuid + voiceover). "
                        "Пайплайн сам запишет твой JSON в базу. "
                        "Excel не используется. Отвечай только JSON apply-ops."
                    ).strip()
        # Отпустить SQLite write-txn на время GPT (иначе UI: database is locked / 30с).
        await session.commit()
        check_streams_n = None
        if check_mode:
            from app.services.check_streams import get_check_streams

            check_streams_n = get_check_streams(project)
        if scene_grammar and output_mode == "project_file" and not check_mode:
            from app.services.scene_grammar_batches import run_scene_grammar_batched

            logger.info(
                "[#{}] enrich_xlsx node={!r}: scene_grammar batched run "
                "(1▶ → outline + shot-batches)",
                project.id,
                node_key,
            )
            api_res = await run_scene_grammar_batched(
                project_dir=project.data_dir,
                node_key=node_key,
                master_prompt=master or "",
                accompanying=accompanying,
                input_paths=data_paths,
                project_id=project.id,
            )
        elif (
            output_mode == "project_file"
            and not check_mode
            and not character_registry
            and isinstance(db_ctx, dict)
            and ctx_path is not None
        ):
            from app.services.apply_ops_batches import (
                CAMERA_MENU_UNITS_PER_BATCH,
                FW_FRAMES_PER_BATCH,
                SCRIPT_FRAMES_QC_UNITS_PER_BATCH,
                SKIP_CAMERA_MENU,
                SKIP_PROMPTS_AND_ACTION,
                VO_PARALLEL_MAX,
                VO_STAGGER_SEC,
                run_apply_ops_batched,
            )
            from app.services import db_apply as _db_apply
            from app.services.node_write_contract import filter_ops_for_node

            nk = str(node_key or "")
            frame_prompts = _is_frame_prompts_prompt(variant, master) or nk.endswith(
                "_fw_frames"
            )
            qc_prompts = _is_qc_prompts_prompt(variant, master) or nk.endswith("_fw_qc")
            write_prompts = frame_prompts or qc_prompts
            scene_analytics = _is_scene_analytics_prompt(variant, master)
            # Нода сценария пишет только биты — свой kind (текст запрещён).
            apply_kind = (
                "excel_gpt_script"
                if script_writer
                else (
                    "excel_gpt_prompts" if write_prompts else "excel_gpt_no_prompts"
                )
            )

            if qc_prompts and _should_skip_qc_gpt(
                frames_for_map, force_full=force_full
            ):
                from app.services.gpt_operator_client import OperatorApiResult

                logger.warning(
                    "[#{}] enrich_xlsx node={!r}: QC skip — промты уже "
                    "в БД (frames={}), GPT не ждём",
                    project.id,
                    node_key,
                    len(frames_for_map),
                )
                api_res = OperatorApiResult(
                    reply_text='{"ops":[]}',
                    output_paths=[ctx_path],
                    apply_ops={"ops": [], "report": "qc_prompts_already_filled"},
                    applied_in_runner=True,
                )
            else:
                async def _apply_batch(payload: dict) -> None:
                    from app.services.db_apply import FIELD_ALIASES

                    raw_ops = list(payload.get("ops") or [])
                    ops = filter_ops_for_node(
                        raw_ops,
                        node_kind=apply_kind,
                    )
                    # excel_gpt не имеет права писать закадр/смысл — включая
                    # ноду сценария (excel_gpt_script): она пишет только биты.
                    _drop = {"voiceover_text", "meaning"}
                    if fw_camera_menu_only:
                        _drop.update(
                            {
                                "image_prompt",
                                "animation_prompt",
                                "image_prompt_shot2",
                                "animation_prompt_shot2",
                                "child_prompts",
                                "shot01_action",
                                "main_action",
                            }
                        )
                    cleaned: list[dict] = []
                    for op in ops:
                        fields = op.get("fields")
                        if not isinstance(fields, dict):
                            cleaned.append(op)
                            continue
                        kept = {}
                        for k, v in fields.items():
                            canon = FIELD_ALIASES.get(
                                str(k).strip().lower().replace(" ", "_"),
                                str(k),
                            )
                            if canon in _drop:
                                continue
                            kept[k] = v
                        if not kept:
                            continue
                        new_op = dict(op)
                        new_op["fields"] = kept
                        cleaned.append(new_op)
                    ops = cleaned
                    if str(node_key or "").endswith(("_fw_frames", "_fw_qc")):
                        from app.services.vo_shot_expand import is_shot_child

                        child_ids = {
                            str(getattr(fr, "uuid", "") or "").strip()
                            for fr in frames_for_map
                            if is_shot_child(fr)
                        }
                        if child_ids:
                            stripped: list[dict] = []
                            for op in ops:
                                uid = str(op.get("frame_uuid") or "").strip()
                                if uid not in child_ids:
                                    stripped.append(op)
                                    continue
                                fields = op.get("fields")
                                if not isinstance(fields, dict):
                                    continue
                                kept = {
                                    k: v
                                    for k, v in fields.items()
                                    if str(k).strip().lower().replace(" ", "_")
                                    not in {
                                        "image_prompt",
                                        "animation_prompt",
                                        "промт_картинки",
                                        "промт_видео",
                                        "промпт_картинки",
                                        "промпт_видео",
                                    }
                                }
                                if kept:
                                    new_op = dict(op)
                                    new_op["fields"] = kept
                                    stripped.append(new_op)
                            ops = stripped
                    await _db_apply.apply_ops(
                        session,
                        project,
                        ops,
                        export_xlsx=bool(payload.get("export_xlsx", False)),
                        node_kind=apply_kind,
                    )
                    await session.commit()
                    await session.refresh(project)

                async def _progress(msg: str) -> None:
                    from app.services.run_sync import update_active_node_progress_text

                    await update_active_node_progress_text(session, project, msg)
                    await session.commit()

                script_frames_qc = (not fw_camera_menu_only) and (
                    _is_script_frames_qc_group_node(variant, master, node_key)
                )
                if fw_camera_menu_only:
                    batch_label = (
                        f"меню съёмки по {CAMERA_MENU_UNITS_PER_BATCH}, "
                        f"параллельно {VO_PARALLEL_MAX}, "
                        f"сдвиг {VO_STAGGER_SEC:g}с"
                    )
                elif script_frames_qc:
                    batch_label = (
                        f"script_frames_qc по {SCRIPT_FRAMES_QC_UNITS_PER_BATCH} "
                        f"отрезков закадра, параллельно {VO_PARALLEL_MAX}, "
                        f"сдвиг {VO_STAGGER_SEC:g}с"
                    )
                elif write_prompts:
                    batch_label = "промты одним вызовом"
                elif scene_analytics:
                    batch_label = "аналитика 54–59 одним вызовом"
                else:
                    batch_label = "dense shot fill"
                logger.info(
                    "[#{}] enrich_xlsx node={!r}: apply-ops adaptive "
                    "1→2→4 ({} )",
                    project.id,
                    node_key,
                    batch_label,
                )
                fw_frames = frame_prompts and str(node_key or "").endswith("_fw_frames")
                api_res = await run_apply_ops_batched(
                    project_dir=project.data_dir,
                    node_key=node_key,
                    role=role,
                    output_mode=output_mode,
                    prompt=master or "",
                    accompanying=accompanying,
                    db_ctx=db_ctx,
                    ctx_path=ctx_path,
                    project_id=project.id,
                    dense=not write_prompts and not scene_analytics,
                    apply_fn=_apply_batch,
                    force_full=force_full,
                    skip_if_field=(
                        None
                        if force_full
                        else (
                            SKIP_CAMERA_MENU
                            if fw_frames and fw_camera_menu_only
                            else (
                                SKIP_PROMPTS_AND_ACTION
                                if fw_frames
                                else (
                                    "image_prompt"
                                    if frame_prompts and not qc_prompts
                                    else None
                                )
                            )
                        )
                    ),
                    chunk_size=(
                        CAMERA_MENU_UNITS_PER_BATCH
                        if fw_camera_menu_only
                        else (
                            FW_FRAMES_PER_BATCH
                            if fw_frames
                            else (
                                SCRIPT_FRAMES_QC_UNITS_PER_BATCH
                                if script_frames_qc
                                else None
                            )
                        )
                    ),
                    parallel_max=(
                        VO_PARALLEL_MAX
                        if fw_camera_menu_only or script_frames_qc
                        else None
                    ),
                    stagger_sec=(
                        VO_STAGGER_SEC
                        if fw_camera_menu_only or script_frames_qc
                        else None
                    ),
                    chunk_by_vo_unit=bool(script_frames_qc and not fw_frames),
                    footer_kind=(
                        "camera_menu"
                        if fw_camera_menu_only
                        else (
                            _script_frames_qc_footer_kind(
                                variant, master, node_key
                            )
                            if script_frames_qc
                            else (
                                "prompts"
                                if write_prompts
                                else ("analytics" if scene_analytics else None)
                            )
                        )
                    ),
                    on_progress=_progress,
                    allow_empty_ops=qc_prompts,
                )
                if fw_frames:
                    from sqlalchemy import select as _sel_fw

                    project_id = int(project.id)
                    session.expire_all()
                    await session.refresh(project)

                    leftover = list(
                        (
                            await session.execute(
                                _sel_fw(Frame)
                                .where(Frame.project_id == project_id)
                                .order_by(Frame.sort_key, Frame.number)
                            )
                        ).scalars().all()
                    )
                    missing_cam = [
                        fr
                        for fr in leftover
                        if getattr(fr, "uuid", None)
                        and not _frame_camera_menu_ready(fr)
                    ]
                    if missing_cam:
                        logger.warning(
                            "[#{}] fw_frames: меню съёмки пустое {}/{} "
                            "— добор в этом же прогоне, без второго ▶",
                            project.id,
                            len(missing_cam),
                            len(leftover),
                        )
                        fw_camera_menu_only = True
                        cam_ctx = build_excel_gpt_db_context(
                            project_id=project.id,
                            slug=project.slug,
                            frames=missing_cam,
                            characters=entity_cards_for_gpt(ents),
                        )
                        ctx_path.write_text(
                            _json.dumps(cam_ctx, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        api_res = await run_apply_ops_batched(
                            project_dir=project.data_dir,
                            node_key=node_key,
                            role=role,
                            output_mode=output_mode,
                            prompt=master or "",
                            accompanying=accompanying,
                            db_ctx=cam_ctx,
                            ctx_path=ctx_path,
                            project_id=project.id,
                            dense=False,
                            apply_fn=_apply_batch,
                            skip_if_field=SKIP_CAMERA_MENU,
                            chunk_size=CAMERA_MENU_UNITS_PER_BATCH,
                            parallel_max=VO_PARALLEL_MAX,
                            stagger_sec=VO_STAGGER_SEC,
                            footer_kind="camera_menu",
                            on_progress=_progress,
                            allow_empty_ops=False,
                        )
                        session.expire_all()
                        await session.refresh(project)
                        leftover = list(
                            (
                                await session.execute(
                                    _sel_fw(Frame)
                                    .where(Frame.project_id == project_id)
                                    .order_by(Frame.sort_key, Frame.number)
                                )
                            ).scalars().all()
                        )
                        still = [
                            fr
                            for fr in leftover
                            if getattr(fr, "uuid", None)
                            and not _frame_camera_menu_ready(fr)
                        ]
                        if still:
                            raise RuntimeError(
                                f"enrich_xlsx node={node_key}: меню съёмки "
                                f"не полное {len(leftover) - len(still)}/"
                                f"{len(leftover)} (крупность/движение/набор). "
                                "Нода не done."
                            )
        else:
            api_res = await run_operator_api(
                project_dir=project.data_dir,
                node_key=node_key,
                role=role,
                output_mode=output_mode,
                prompt=master or "",
                accompanying=accompanying,
                input_paths=data_paths,
                check_mode=check_mode,
                check_fix=check_fix,
                source_prompt_keys=source_prompt_keys,
                check_streams=check_streams_n,
                db_sot_check=db_sot_check,
            )
        await session.refresh(project)
        # После project_file / DB-check: apply-ops JSON → DB (SoT).
        if output_mode == "project_file" or (
            check_mode
            and check_fix
            and db_sot_check
            and getattr(api_res, "apply_ops", None)
        ):
            ops_data = getattr(api_res, "apply_ops", None)
            ops_list = (
                list(ops_data.get("ops") or [])
                if isinstance(ops_data, dict)
                else []
            )
            if check_mode and ops_list:
                ops_list, n_rf = _drop_replace_frames_ops(ops_list)
                if n_rf:
                    logger.warning(
                        "[#{}] enrich_xlsx node={}: checkMode drop "
                        "replace_frames x{} (проверка не переписывает разбивку)",
                        project.id,
                        node_key,
                        n_rf,
                    )
                if check_known_uuids:
                    ops_list, n_unk = _filter_check_frame_ops(
                        ops_list,
                        check_known_uuids,
                        check_number_to_uuid,
                    )
                    if n_unk:
                        logger.warning(
                            "[#{}] enrich_xlsx node={}: checkMode drop "
                            "unknown frame_uuid x{} (гейт без apply)",
                            project.id,
                            node_key,
                            n_unk,
                        )
                if ops_list:
                    logger.warning(
                        "[#{}] enrich_xlsx node={}: checkMode drop "
                        "{} apply-ops (гейт не пишет закадр/промты в кадры)",
                        project.id,
                        node_key,
                        len(ops_list),
                    )
                    ops_list = []
            chars_list = (
                list(ops_data.get("characters") or [])
                if isinstance(ops_data, dict)
                else []
            )
            scenes_list = (
                list(ops_data.get("scenes") or [])
                if isinstance(ops_data, dict)
                else []
            )
            # check/report_only не пишут (или пишут DB-check без стрипа).
            # Shot-fill не пишет промты картинки/видео; frames/QC — пишут.
            apply_node_kind: str | None = None
            if ops_list and not check_mode:
                from app.services.node_write_contract import filter_ops_for_node

                nk = str(node_key or "")
                write_prompts = (
                    _is_frame_prompts_prompt(variant, master)
                    or _is_qc_prompts_prompt(variant, master)
                    or nk.endswith("_fw_frames")
                    or nk.endswith("_fw_qc")
                )
                apply_kind = (
                    "excel_gpt_prompts" if write_prompts else "excel_gpt_no_prompts"
                )
                n_fields_before = sum(
                    len(op.get("fields") or {})
                    for op in ops_list
                    if isinstance(op, dict)
                )
                ops_list = filter_ops_for_node(ops_list, node_kind=apply_kind)
                apply_node_kind = apply_kind
                n_fields_after = sum(
                    len(op.get("fields") or {})
                    for op in ops_list
                    if isinstance(op, dict)
                )
                dropped = n_fields_before - n_fields_after
                if dropped:
                    logger.info(
                        "[#{}] enrich_xlsx node={}: stripped {} fields ({})",
                        project.id,
                        node_key,
                        dropped,
                        apply_kind,
                    )
            if getattr(api_res, "applied_in_runner", False):
                logger.info(
                    "[#{}] enrich_xlsx node={}: apply-ops уже записан "
                    "по батчам ({} ops)",
                    project.id,
                    node_key,
                    len(ops_list),
                )
            elif ops_data and (ops_list or chars_list or scenes_list):
                from app.services import db_apply
                from app.services.node_write_contract import (
                    coverage_report,
                    skip_frame_coverage,
                )

                try:
                    applied = await db_apply.apply_ops(
                        session,
                        project,
                        ops_list,
                        characters=chars_list or None,
                        scenes=scenes_list or None,
                        export_xlsx=True,
                        node_kind=apply_node_kind,
                    )
                    await session.commit()
                    logger.info(
                        "[#{}] enrich_xlsx node={}: apply-ops записано "
                        "ops={} characters={} scenes={} (DB only)",
                        project.id,
                        node_key,
                        applied.get("updated"),
                        applied.get("characters"),
                        applied.get("scenes"),
                    )
                    skip_coverage = skip_frame_coverage(
                        scene_grammar=scene_grammar,
                        character_registry=character_registry,
                        ops_list=ops_list,
                        chars_list=chars_list,
                        scenes_list=scenes_list,
                    )
                    if expected_frame_uuids and not check_mode and not skip_coverage:
                        cov = coverage_report(
                            ops_list, expected_uuids=expected_frame_uuids
                        )
                        if cov.extra:
                            logger.warning(
                                "[#{}] enrich_xlsx node={}: extra frame_uuid {}",
                                project.id,
                                node_key,
                                cov.extra[:8],
                            )
                        if not cov.ok:
                            _persist_excel_gpt_reply_for_ui(
                                project,
                                node_key,
                                data_paths=data_paths,
                                api_res=api_res,
                            )
                            raise RuntimeError(
                                f"enrich_xlsx node={node_key}: покрытие "
                                f"{len(cov.matched)}/{len(expected_frame_uuids)} "
                                f"не N/N, missing={cov.missing}. "
                                "Нода не done."
                            )
                except db_apply.ApplyOpsError as e:
                    # Ответ модели уже на диске — сохраняем в meta, иначе UI
                    # показывает «нет результата» при отклонённых полях.
                    _persist_excel_gpt_reply_for_ui(
                        project,
                        node_key,
                        data_paths=data_paths,
                        api_res=api_res,
                    )
                    raise RuntimeError(
                        f"enrich_xlsx node={node_key}: apply-ops отклонён: {e}"
                    ) from None
            elif ops_data:
                if check_mode:
                    logger.info(
                        "[#{}] enrich_xlsx node={}: checkMode без apply "
                        "после фильтра — только отчёт",
                        project.id,
                        node_key,
                    )
                else:
                    detail = (
                        str(ops_data.get("error") or ops_data.get("report") or "")
                        .strip()
                        or "ops и characters пустые"
                    )
                    _persist_excel_gpt_reply_for_ui(
                        project,
                        node_key,
                        data_paths=data_paths,
                        api_res=api_res,
                    )
                    raise RuntimeError(
                        f"enrich_xlsx node={node_key}: apply-ops JSON пустой — {detail}. "
                        "Нужны непустые ops и/или characters (создай реестр, "
                        "не возвращай пустые списки)."
                    )
            else:
                # DB SoT: без apply-ops шаг не принимаем (TSV/xlsx writeback отключён).
                _persist_excel_gpt_reply_for_ui(
                    project,
                    node_key,
                    data_paths=data_paths,
                    api_res=api_res,
                )
                raise RuntimeError(
                    f"enrich_xlsx node={node_key}: модель не вернула apply-ops JSON. "
                    "Нужен {\"ops\":[…]} и/или {\"characters\":[…]} — "
                    "запись через Excel/TSV больше не поддерживается."
                )
        nk_now = str(node_key or "")
        if nk_now.endswith("_fw_frames") or nk_now.endswith("_fw_qc"):
            from sqlalchemy import select as _sel_cov

            from app.services.vo_shot_expand import (
                apply_shot_voiceover_to_cells,
                distribute_coverage_prompts,
                inherit_camera_on_children,
            )

            cov_frames = list(
                (
                    await session.execute(
                        _sel_cov(Frame)
                        .where(Frame.project_id == project.id)
                        .order_by(Frame.sort_key, Frame.number)
                    )
                ).scalars().all()
            )
            n_dist = distribute_coverage_prompts(cov_frames)
            n_cam = inherit_camera_on_children(cov_frames)
            n_vo = 0
            if nk_now.endswith("_fw_qc"):
                n_vo = apply_shot_voiceover_to_cells(cov_frames)
            await session.flush()
            logger.info(
                "[#{}] {} coverage: vo_cells={} child_prompts={} camera={}",
                project.id,
                nk_now,
                n_vo,
                n_dist,
                n_cam,
            )
        save_operator_result(
            project,
            node_key,
            input_paths=data_paths,
            output_paths=list(api_res.output_paths),
            reply_text=api_res.reply_text,
            gate_status=api_res.gate_status,
            analysis=(
                api_res.analysis.to_dict() if getattr(api_res, "analysis", None) else None
            ),
        )
        if getattr(api_res, "reply_text", None):
            save_gpt_reply_text(project, node_key, api_res.reply_text)
        await session.flush()
        ready_status = _SLOT_MAP[slot_idx][1]
        running_status = _SLOT_MAP[slot_idx][0]
        # status мог уйти в generating_* пока шёл долгий API-check
        await session.refresh(project)
        meta = dict(project.meta or {})
        completed = [
            int(x) for x in (meta.get("enrich_completed_slots") or []) if str(x).isdigit()
        ]
        if slot_idx not in completed:
            completed.append(slot_idx)
            completed.sort()
            meta["enrich_completed_slots"] = completed
        done_keys = [str(k) for k in (meta.get("excel_gpt_completed_keys") or [])]
        if node_key not in done_keys:
            done_keys.append(node_key)
            meta["excel_gpt_completed_keys"] = done_keys
        meta.pop("active_excel_gpt_node_key", None)
        project.meta = meta
        flag_modified(project, "meta")
        await session.flush()
        await session.refresh(project)
        # Юзер мог ▶ другой шаг (split) пока GPT enrich ещё отвечал.
        if project.status is not running_status:
            logger.warning(
                "[#{}] enrich_xlsx slot={}: статус уже {} (ждали {}) — "
                "не пишем enrich ready (stale GPT)",
                project.id,
                slot_idx,
                project.status.value,
                running_status.value,
            )
            return
        await _harness_before_enrich_ready(session, project, check_mode=check_mode)
        _apply_enrich_ready_status(
            project,
            running_status=running_status,
            ready_status=ready_status,
        )
        await session.flush()
        if output_mode == "project_file" and any(
            p.name == "project.xlsx" and p.exists() for p in api_res.output_paths
        ):
            try:
                from app.services.node_xlsx_snapshot import snapshot_and_bind_node_xlsx

                await snapshot_and_bind_node_xlsx(
                    session,
                    project,
                    node_key=node_key,
                    node_type="excel_gpt",
                )
                from app.services.node_xlsx_snapshot import release_upload_display_source

                if release_upload_display_source(project, node_key):
                    flag_modified(project, "meta")
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[#{}] enrich_xlsx API slot={} xlsx snapshot bind failed: {}",
                    project.id,
                    slot_idx,
                    e,
                )
        try:
            from app.services.run_sync import complete_excel_gpt_node_by_key

            await complete_excel_gpt_node_by_key(
                session, project, node_key, enrich_slot=slot_idx
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[#{}] enrich_xlsx API: complete_excel_gpt_node_by_key failed",
                project.id,
            )
        logger.info(
            "[#{}] enrich_xlsx API done → {} gate={} files={}",
            project.id,
            ready_status.value,
            api_res.gate_status,
            [p.name for p in api_res.output_paths],
        )
        await _after_excel_gpt_done(
            session,
            project,
            node_key=node_key,
            slot_idx=slot_idx,
            ready_status=ready_status,
        )
        return

    # ── Legacy browser path (только transport=browser) ────────────────
    data_paths = attachment_paths(project, node_key)
    if not data_paths:
        raise RuntimeError(
            f"enrich_xlsx: нет файла для отправки "
            f"({display_attachment_name(project, node_key)})"
        )
    want_xlsx = expects_xlsx_result(project, node_key)
    mode = work_mode(project, node_key)
    download_path = data_paths[0] if data_paths[0].suffix.lower() in {".xlsx", ".xls"} else xlsx_path
    if not download_path.exists():
        download_path = xlsx_path
    logger.info(
        "[#{}] enrich_xlsx slot={} mode={} want_xlsx={} attach={} (browser)",
        project.id,
        slot_idx,
        mode,
        want_xlsx,
        [p.name for p in data_paths],
    )

    from app.services import chatgpt_xlsx as cx

    tmp_dir = cx.tmp_gpt_dir(project)
    prompt_file = tmp_dir / f"prompt_{prompt_step_code}_{variant}.md"
    prompt_file.write_text((master or "").strip(), encoding="utf-8")
    attach_files = [prompt_file, *data_paths]

    # 3. Round-trip до 3 раз.
    from app.services.step_cancel import StepCancelledError, raise_if_cancelled

    last_err: Exception | None = None
    xlsx_stat_before_run = cx.project_xlsx_stat(xlsx_path)

    # Browser GPT тоже долгий — не держим SQLite write-lock.
    await session.commit()

    for attempt in range(1, _MAX_RETRIES + 1):
        raise_if_cancelled(project.id)
        # ChatGPT re-attach читает те же пути — файл мог быть удалён
        # (параллельный шаг / purge tmp) пока ждали Send.
        prompt_file.write_text((master or "").strip(), encoding="utf-8")
        logger.info(
            "[#{}] enrich_xlsx slot={} attempt {}/{}",
            project.id,
            slot_idx,
            attempt,
            _MAX_RETRIES,
        )
        try:
            if want_xlsx:

                async def _do() -> str:
                    return await xgf.telegram_style_ask_and_download(
                        accompanying.strip(),
                        attach_files,
                        download_path,
                        ask_timeout=1200,
                        download_timeout=600,
                        project_id=project.id,
                        validate_xlsx_download=download_path.suffix.lower()
                        in {".xlsx", ".xls"},
                    )

            else:

                async def _do() -> str:
                    return await xgf.telegram_style_ask_with_files(
                        (accompanying or "").strip()
                        or "Выполни инструкцию из приложенного промта.",
                        attach_files,
                        timeout=1200,
                        project_id=project.id,
                    )

            reply = await xgf.run_under_xlsx_lock(
                project.id, legacy_step_code, _do
            )
            logger.info(
                "[#{}] enrich_xlsx: получен ответ len={} (try={}, want_xlsx={})",
                project.id,
                len(reply or ""),
                attempt,
                want_xlsx,
            )
            if want_xlsx:
                if not download_path.exists() or download_path.stat().st_size < 1024:
                    raise RuntimeError(
                        f"скачанный файл пустой/слишком маленький "
                        f"({download_path.stat().st_size if download_path.exists() else 0} байт)"
                    )
                logger.info(
                    "[#{}] enrich_xlsx: файл обновлён {} ({} байт)",
                    project.id,
                    download_path.name,
                    download_path.stat().st_size,
                )
            else:
                if not (reply or "").strip():
                    raise RuntimeError("GPT вернул пустой ответ (режим без xlsx)")
                saved = save_gpt_reply_text(project, node_key, reply or "")
                logger.info(
                    "[#{}] enrich_xlsx: текстовый ответ сохранён → {}",
                    project.id,
                    saved.name if saved else "?",
                )
            break  # успех
        except Exception as e:  # noqa: BLE001
            last_err = e
            if not want_xlsx:
                logger.warning(
                    "[#{}] enrich_xlsx slot={} text-mode attempt {}/{} FAILED: {}",
                    project.id,
                    slot_idx,
                    attempt,
                    _MAX_RETRIES,
                    e,
                )
                if attempt >= _MAX_RETRIES:
                    project.status = running_status
                    await session.flush()
                    raise RuntimeError(
                        f"enrich_xlsx slot={slot_idx}: 3 попытки failed, last err: {e}"
                    ) from e
                continue
            xlsx_ok = cx.should_accept_xlsx_after_gpt_error(
                xlsx_path, xlsx_stat_before_run, e
            )
            if xlsx_ok:
                logger.warning(
                    "[#{}] enrich_xlsx slot={} GPT error after fresh xlsx "
                    "({} байт) — skip retry, use downloaded file: {}",
                    project.id,
                    slot_idx,
                    xlsx_path.stat().st_size,
                    e,
                )
                break
            from app.bots.browser import _looks_like_cdp_connect_failure

            if (
                not xlsx_ok
                and xlsx_path.exists()
                and xlsx_path.stat().st_size >= 1024
                and validate_xlsx(xlsx_path) is None
                and _looks_like_cdp_connect_failure(e)
            ):
                logger.warning(
                    "[#{}] enrich_xlsx slot={}: CDP/Chrome не ответил — "
                    "старый project.xlsx на диске не считаем успехом: {}",
                    project.id,
                    slot_idx,
                    e,
                )
            logger.warning(
                "[#{}] enrich_xlsx slot={} attempt {}/{} FAILED: {}",
                project.id,
                slot_idx,
                attempt,
                _MAX_RETRIES,
                e,
            )
            if attempt >= _MAX_RETRIES:
                # Откатим статус назад — пайплайн сам поднимет из данных.
                project.status = running_status  # оставляем running, чтобы юзер ткнул retry
                await session.flush()
                raise RuntimeError(
                    f"enrich_xlsx slot={slot_idx}: 3 попытки failed, last err: {e}"
                ) from e
            continue

    # 3b. Проверочная роль: разобрать ответ → analysis.json + gateStatus
    # (иначе ОК/НЕ ОК на канвасе не откроются после browser-run).
    if branching_role and node_key:
        from app.services.gpt_operator import apply_check_reply

        reply_text = locals().get("reply") or ""
        if not str(reply_text).strip():
            from app.services.excel_gpt_node import upload_dir as _udir

            rp = _udir(project, node_key) / "gpt_reply.txt"
            if rp.is_file():
                reply_text = rp.read_text(encoding="utf-8")
        try:
            apply_check_reply(
                project,
                node_key,
                str(reply_text or ""),
                input_paths=list(data_paths),
                extra_output_paths=(
                    [download_path]
                    if want_xlsx and download_path.exists()
                    else None
                ),
            )
            logger.info(
                "[#{}] enrich_xlsx browser check: gateStatus записан для {}",
                project.id,
                node_key,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "[#{}] enrich_xlsx browser check: не удалось сохранить analysis",
                project.id,
            )

    # 4. Excel → DB только через явный Import (не auto после enrich).
    if want_xlsx:
        logger.info(
            "[#{}] enrich_xlsx slot={}: skip auto Excel import "
            "(DB SoT; use Import button if needed)",
            project.id,
            slot_idx,
        )
    else:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "meta")
        await session.flush()

    # 5. Ставим статус slot_<i>_ready (не откатываем более продвинутый шаг).
    await session.refresh(project)
    meta = dict(project.meta or {})
    completed = [int(x) for x in (meta.get("enrich_completed_slots") or []) if str(x).isdigit()]
    if slot_idx not in completed:
        completed.append(slot_idx)
        completed.sort()
        meta["enrich_completed_slots"] = completed
    if node_key:
        done_keys = [str(k) for k in (meta.get("excel_gpt_completed_keys") or [])]
        if node_key not in done_keys:
            done_keys.append(node_key)
            meta["excel_gpt_completed_keys"] = done_keys
        meta.pop("active_excel_gpt_node_key", None)
    # xlsx на диске обновлён — сбросить кэш персонажей, чтобы Hero
    # перечитал лист «Персонажи» (иначе остаётся старый meta['excel_hero']).
    if "excel_hero" in meta:
        meta.pop("excel_hero")
        logger.info(
            "[#{}] enrich_xlsx slot={}: сброшен кэш excel_hero после "
            "обновления xlsx",
            project.id,
            slot_idx,
        )
    project.meta = meta

    await _harness_before_enrich_ready(session, project, check_mode=check_mode)
    _apply_enrich_ready_status(
        project,
        running_status=running_status,
        ready_status=ready_status,
    )
    await session.flush()
    try:
        from app.services.node_xlsx_snapshot import snapshot_and_bind_node_xlsx

        await snapshot_and_bind_node_xlsx(
            session,
            project,
            node_key=node_key,
            node_type="excel_gpt",
        )
        from app.services.node_xlsx_snapshot import release_upload_display_source
        from sqlalchemy.orm.attributes import flag_modified

        if node_key and release_upload_display_source(project, node_key):
            flag_modified(project, "meta")
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[#{}] enrich_xlsx slot={} xlsx snapshot bind failed: {}",
            project.id,
            slot_idx,
            e,
        )
    logger.info(
        "[#{}] enrich_xlsx slot={} → status={}",
        project.id,
        slot_idx,
        project.status.value,
    )

    # NodeRun текущей ноды → done ДО переключения active_key на следующий слот.
    # Иначе complete_active_node / UI видят «в работе» на слоте 1, а слот 3
    # ошибочно done без применённого xlsx.
    try:
        from app.services.run_sync import complete_excel_gpt_node_by_key

        await complete_excel_gpt_node_by_key(
            session, project, node_key, enrich_slot=slot_idx
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[#{}] enrich_xlsx: complete NodeRun slot={} failed",
            project.id,
            slot_idx,
            exc_info=True,
        )

    # 6. Storage sync + auto-chain / advance.
    await _after_excel_gpt_done(
        session,
        project,
        node_key=node_key,
        slot_idx=slot_idx,
        ready_status=ready_status,
    )

    _ = last_err  # keep ref to silence "unused" warning in some linters

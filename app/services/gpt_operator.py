"""Оператор GPT: контракт, сверка файлов со стрелками, без браузера.

Источник правды при запуске:
  meta.excel_gpt_nodes[nodeKey] + meta.canvas_graph.edges + файлы на диске.

UI и node.data — только кэш отрисовки; resolve всегда перепроверяет диск.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from app.models import Project
from app.services.canvas_graph import canvas_graph_from_meta
from app.services.excel_gpt_node import (
    EXCEL_GPT_NODE_TYPE,
    attachment_paths,
    is_excel_gpt_node_type,
    node_config,
    upload_dir,
)

EdgeKind = Literal["after", "gate", "pass", "fail"]
OperatorRole = Literal["assist", "review", "transform", "extract", "compare", "gate"]
OutputMode = Literal["text", "project_file", "sidecar"]
EmitKind = Literal["result", "reply_txt", "analysis", "inputs"]
InputOrigin = Literal["upload", "edge", "project", "snapshot"]
# Критерии проверки: промты со стрелок ИЛИ готовый агент из prompts/check_operator.
CheckPromptSource = Literal["upstream", "agent"]
VALID_CHECK_PROMPT_SOURCES: frozenset[str] = frozenset({"upstream", "agent"})

# Связь = порядок + кандидат на вход. Файлы берёт приёмник (takeFromEdges),
# не отдельный kind «feed». gate — legacy «если ok»; pass/fail — ветки вердикта.
VALID_EDGE_KINDS: frozenset[str] = frozenset({"after", "gate", "pass", "fail"})
VALID_ROLES: frozenset[str] = frozenset(
    {"assist", "review", "transform", "extract", "compare", "gate"}
)
VALID_OUTPUTS: frozenset[str] = frozenset({"text", "project_file", "sidecar"})
# Что нода отдаёт дальше по стрелке (мультивыбор).
VALID_EMIT_KINDS: frozenset[str] = frozenset(
    {"result", "reply_txt", "analysis", "inputs"}
)
_REPLY_TXT_NAMES: frozenset[str] = frozenset(
    {"gpt_reply.txt", "operator_transform.txt", "check_report.txt"}
)
_ANALYSIS_NAMES: frozenset[str] = frozenset({"analysis.json"})

# Роли с вердиктом ок/не ок → две исходящие ветки.
BRANCHING_ROLES: frozenset[str] = frozenset({"review", "gate", "compare"})

ROLE_DEFAULT_LABELS: dict[str, str] = {
    "assist": "ИИ-редактор сцен",
    "review": "ИИ-редактор сцен",
    "transform": "ИИ-редактор сцен",
    "extract": "ИИ-редактор сцен",
    "compare": "ИИ-редактор сцен",
    "gate": "ИИ-редактор сцен",
}
_DEFAULT_LABEL_SET: frozenset[str] = frozenset(
    {*ROLE_DEFAULT_LABELS.values(), "Работа с GPT", "Ок / не ок", ""}
)

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".webm", ".mov", ".mkv"})
_DOC_SUFFIXES = frozenset({".xlsx", ".xls", ".txt", ".md", ".json", ".csv", ".pdf"})
_ANY_SUFFIXES = _IMAGE_SUFFIXES | _VIDEO_SUFFIXES | _DOC_SUFFIXES


def normalize_edge_kind(raw: Any) -> EdgeKind:
    s = str(raw or "after").strip().lower()
    # синонимы UI / legacy
    if s in ("ok", "если ok", "если_ok", "pass_ok"):
        s = "pass"
    if s in ("не ok", "не ок", "neok", "not_ok", "reject"):
        s = "fail"
    # legacy: «файлы» / «проверка» на стрелке → обычная связь (вход решает нода)
    if s in ("feed", "review", "файлы", "проверка"):
        s = "after"
    if s in VALID_EDGE_KINDS:
        return s  # type: ignore[return-value]
    return "after"


def is_pass_edge_kind(kind: str) -> bool:
    """True для ветки «ок» (включая legacy gate)."""
    return kind in ("pass", "gate")


def is_fail_edge_kind(kind: str) -> bool:
    return kind == "fail"


def is_verdict_edge_kind(kind: str) -> bool:
    return is_pass_edge_kind(kind) or is_fail_edge_kind(kind)


def default_label_for_role(role: OperatorRole | str) -> str:
    return ROLE_DEFAULT_LABELS.get(str(role), "ИИ-редактор сцен")


def edge_kind_of(edge: dict[str, Any]) -> EdgeKind:
    data = edge.get("data") if isinstance(edge.get("data"), dict) else {}
    return normalize_edge_kind(data.get("kind") or edge.get("kind"))


def normalize_role(raw: Any, *, fallback: str = "assist") -> OperatorRole:
    s = str(raw or fallback).strip().lower()
    # legacy workMode → role
    if s == "assist":
        return "assist"
    if s in VALID_ROLES:
        return s  # type: ignore[return-value]
    return "assist"  # type: ignore[return-value]


def normalize_output_mode(raw: Any, *, role: OperatorRole) -> OutputMode:
    s = str(raw or "").strip().lower()
    if s in VALID_OUTPUTS:
        return s  # type: ignore[return-value]
    if role in ("review", "extract", "compare", "gate"):
        return "text"
    if role == "transform":
        return "sidecar"
    return "project_file"


def normalize_emit_kinds(raw: Any, *, role: OperatorRole) -> list[EmitKind]:
    """Что нода отдаёт следующей по стрелке.

    Дефолты (как раньше без выбора):
      review/gate/compare → только вход (не analysis/reply);
      остальные → результат + текст ответа.
    """
    out: list[EmitKind] = []
    if isinstance(raw, list):
        for item in raw:
            s = str(item or "").strip().lower()
            if s in VALID_EMIT_KINDS and s not in out:
                out.append(s)  # type: ignore[arg-type]
    if out:
        return out
    if role in BRANCHING_ROLES:
        return ["inputs", "reply_txt"]
    return ["result", "reply_txt"]


def operator_config(project: Project, node_key: str) -> dict[str, Any]:
    cfg = dict(node_config(project, node_key))
    role = normalize_role(cfg.get("role") or cfg.get("workMode") or "assist")
    cfg["role"] = role
    cfg["workMode"] = role if role in ("assist", "review", "transform") else "assist"
    check_mode = bool(cfg.get("checkMode"))
    cfg["checkMode"] = check_mode
    # Чинить по умолчанию; явно false → только отчёт.
    if "checkFix" in cfg:
        cfg["checkFix"] = bool(cfg.get("checkFix"))
    else:
        cfg["checkFix"] = True
    raw_cps = str(cfg.get("checkPromptSource") or "upstream").strip().lower()
    cfg["checkPromptSource"] = (
        raw_cps if raw_cps in VALID_CHECK_PROMPT_SOURCES else "upstream"
    )
    # Для checkMode дефолтный emit — вход + txt-отчёт (если пользователь не задал).
    if check_mode and not (
        isinstance(cfg.get("emitKinds"), list) and cfg.get("emitKinds")
    ):
        cfg["emitKinds"] = ["inputs", "reply_txt"]
    else:
        cfg["emitKinds"] = normalize_emit_kinds(cfg.get("emitKinds"), role=role)
    if check_mode:
        # Отчёт — текст; Excel проекта не трогаем как основной выход отчёта.
        cfg["outputMode"] = normalize_output_mode(
            cfg.get("outputMode") or "text", role="review"
        )
    else:
        cfg["outputMode"] = normalize_output_mode(cfg.get("outputMode"), role=role)
    cfg["useSnapshot"] = bool(cfg.get("useSnapshot"))
    # Вход со стрелок: по умолчанию да (подвели → претендует на файлы прошлой ноды).
    if "takeFromEdges" in cfg:
        cfg["takeFromEdges"] = bool(cfg.get("takeFromEdges"))
    else:
        cfg["takeFromEdges"] = True
    raw_transport = str(cfg.get("transport") or "").strip().lower()
    if raw_transport in ("api", "browser"):
        cfg["transport"] = raw_transport
    else:
        # Полный переход на API: browser только если явно указан.
        cfg["transport"] = "api"
    # multi-file uploads stored as list of names
    names = cfg.get("uploadedFileNames")
    if not isinstance(names, list):
        single = str(cfg.get("uploadedFileName") or "").strip()
        names = [single] if single else []
    cfg["uploadedFileNames"] = [str(x).strip() for x in names if str(x).strip()]
    return cfg


def is_check_operator(cfg_or_role: Any, check_mode: bool | None = None) -> bool:
    """Роль с вердиктом или явный тумблер «Проверка»."""
    if isinstance(cfg_or_role, dict):
        role = str(cfg_or_role.get("role") or "")
        cm = bool(cfg_or_role.get("checkMode")) if check_mode is None else bool(check_mode)
        return cm or role in BRANCHING_ROLES
    role = str(cfg_or_role or "")
    return bool(check_mode) or role in BRANCHING_ROLES


def collect_source_prompts(project: Project, node_key: str) -> list[dict[str, Any]]:
    """Активные мастер-промты нод по входящим стрелкам (для checkMode)."""
    from app.orchestrator.node_registry import NODE_TYPE_TO_STEP_CODE
    from app.services.excel_gpt_node import EXCEL_GPT_STEP_CODE
    from app.services.prompt_library import read_resolved_project_prompt
    from app.services import gpt_text_builder as gtb

    types = _node_type_map(project)
    out: list[dict[str, Any]] = []
    for e in _incoming_edges(project, node_key):
        src = str(e.get("source") or "").strip()
        if not src:
            continue
        typ = types.get(src, "")
        step = NODE_TYPE_TO_STEP_CODE.get(typ) or ""
        if not step and is_excel_gpt_node_type(typ):
            step = EXCEL_GPT_STEP_CODE
        if not step and typ:
            step = typ
        entry: dict[str, Any] = {
            "nodeKey": src,
            "nodeType": typ,
            "stepCode": step or None,
            "ok": False,
            "chars": 0,
            "text": "",
            "accompanying": "",
            "variant": None,
            "source": None,
            "path": None,
            "error": None,
        }
        if not step:
            entry["error"] = "нет step-кода для промта источника"
            out.append(entry)
            continue
        try:
            use_node = is_excel_gpt_node_type(typ)
            variant, path, text, source = read_resolved_project_prompt(
                project,
                step,
                node_key=src if use_node else None,
                slot_id="main" if use_node else None,
            )
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"промт не прочитан: {exc}"
            out.append(entry)
            continue
        accomp = ""
        try:
            accomp = gtb.get_effective_text(project, step) or ""
            if use_node and not accomp.strip():
                accomp = gtb.get_effective_text(project, EXCEL_GPT_STEP_CODE) or ""
        except Exception:  # noqa: BLE001
            accomp = ""
        text_s = (text or "").strip()
        entry.update(
            {
                "ok": bool(text_s),
                "chars": len(text_s),
                "text": text_s,
                "accompanying": (accomp or "").strip(),
                "variant": variant,
                "source": source,
                "path": str(path) if path else None,
                "error": None if text_s else "пустой промт источника",
            }
        )
        out.append(entry)
    return out


def resolve_check_report_format(project: Project, node_key: str) -> tuple[str, bool]:
    """(текст шаблона отчёта, is_custom)."""
    from app.services.check_analysis import default_check_report_format, normalize_check_report_format

    cfg = operator_config(project, node_key)
    custom = normalize_check_report_format(str(cfg.get("checkReportFormat") or ""))
    if custom:
        return custom, True
    return default_check_report_format(), False


def assemble_check_master_prompt(
    sources: list[dict[str, Any]],
    *,
    check_fix: bool = True,
    reviewer_notes: str = "",
    report_format: str | None = None,
    db_sot: bool = False,
) -> str:
    """Собрать master-промт проверки из промтов источников + TXT footer."""
    from app.services.check_analysis import (
        TXT_REPORT_FOOTER_DB_SOT,
        append_txt_report_footer,
        normalize_check_report_format,
    )

    mode = "fix" if check_fix else "report_only"
    effective_format = report_format
    if db_sot and not normalize_check_report_format(report_format):
        effective_format = TXT_REPORT_FOOTER_DB_SOT
    if db_sot:
        attach_rule = (
            "Источник правды — БАЗА: вложение db_check.json "
            "(frames[].uuid + attrs + voiceover, scene_registry, characters). "
            "Excel/TSV/# Лист:/project.xlsx НЕ используются и НЕ упоминай их. "
            + (
                "При mode=fix после отчёта верни JSON apply-ops "
                '{"ops":[...],"characters":[...],"scenes":[...]} '
                "— пайплайн запишет в DB. Не пиши XLSX_WRITEBACK / TSV."
                if check_fix
                else "mode=report_only — только TXT-отчёт по базе, без правок."
            )
        )
        contract = (
            "# КОНТРАКТ API (DB SoT, важнее исходных промтов)\n"
            "Проверяй db_check.json / scene_registry / frame uuid+attrs. "
            "Пустой Excel или отсутствие .xlsx — НЕ ошибка и НЕ finding. "
            "Запрещены findings про TSV, `# Лист:`, `@row=`, «нет frame_uuid "
            "в TSV», «нет project.xlsx». UUID бери из frames[].uuid. "
            + (
                "mode=fix: отчёт + JSON apply-ops в DB."
                if check_fix
                else "mode=report_only: только отчёт."
            )
        )
    else:
        attach_rule = (
            "Вложение — текстовый экспорт xlsx (TSV). Бинарный .xlsx недоступен — это норма, "
            "не отказывай из‑за «нет project.xlsx». При правках: после отчёта блок "
            "--- XLSX_WRITEBACK --- с `# Лист:` TSV; в forward укажи file: fixed."
            if check_fix
            else "НЕ изменяй файл — только отчёт (file: original). TSV-экспорт во вложении — это и есть книга."
        )
        contract = (
            "# КОНТРАКТ API (важнее исходных промтов)\n"
            + (
                "Бинарный project.xlsx в API недоступен — во вложении TSV-экспорт, "
                "это и есть книга. Отказ «нет файла / нет project.xlsx» ЗАПРЕЩЁН. "
                "При mode=fix ОБЯЗАТЕЛЕН блок --- XLSX_WRITEBACK --- с секциями "
                "`# Лист: …` (TSV). Требования исходных промтов «прикрепи/верни .xlsx» "
                "заменяются этим контрактом: правки только через XLSX_WRITEBACK. "
                "В forward укажи file: fixed."
                if check_fix
                else "Бинарный project.xlsx недоступен; TSV во вложении = книга. "
                "Не отказывай из‑за «нет файла». mode=report_only — только отчёт, "
                "без XLSX_WRITEBACK; file: original."
            )
        )
    blocks: list[str] = [
        "Ты — агент проверки результата.",
        "Проверь вход СТРОГО по исходным промтам работы ниже (не придумывай свой этап).",
        f"mode: {mode}",
        attach_rule,
        "",
        "# Исходные промты работы",
    ]
    for s in sources:
        if not s.get("ok"):
            continue
        key = str(s.get("nodeKey") or "?")
        blocks.append(f"### source: {key}")
        blocks.append(str(s.get("text") or "").strip())
        accomp = str(s.get("accompanying") or "").strip()
        if accomp:
            blocks.append(f"(сопровождение источника {key}):\n{accomp}")
        blocks.append("")
    notes = (reviewer_notes or "").strip()
    if notes:
        blocks.append("# Доп. указания ревьюера (эта нода)")
        blocks.append(notes)
    blocks.append("")
    blocks.append(contract)
    return append_txt_report_footer(
        "\n".join(blocks).strip(),
        report_format=effective_format,
    )


def assemble_check_master_prompt_with_hero_hint(
    project: Project,
    node_key: str,
    sources: list[dict[str, Any]],
    *,
    check_fix: bool = True,
    reviewer_notes: str = "",
    report_format: str | None = None,
) -> str:
    """assemble_check_master_prompt + vision_check hint для hero/scenes."""
    from app.services.check_analysis import (
        append_hero_regen_hint,
        append_vision_check_hint,
    )

    typ = upstream_node_type_for_check(project, node_key)
    # PNG vision: только отчёт + db_patch. checkFix/TSV writeback сбивает модель.
    vision_types = (
        "hero",
        "images",
        "img",
        "hitl_images",
        "image_prompts",
        "videos",
        "hitl_videos",
        "animation_prompts",
    )
    effective_fix = False if typ in vision_types else check_fix
    text = assemble_check_master_prompt(
        sources,
        check_fix=effective_fix,
        reviewer_notes=reviewer_notes,
        report_format=report_format,
    )
    if typ == "hero":
        text = append_vision_check_hint(append_hero_regen_hint(text))
        return text
    if typ in (
        "images",
        "img",
        "hitl_images",
        "image_prompts",
        "videos",
        "hitl_videos",
        "animation_prompts",
    ):
        return append_vision_check_hint(text)
    return text


def append_vision_hint_for_upstream(project: Project, node_key: str, prompt: str) -> str:
    """Дописать vision_check / hero regen hint по типу upstream."""
    from app.services.check_analysis import (
        append_hero_regen_hint,
        append_vision_check_hint,
    )

    typ = upstream_node_type_for_check(project, node_key)
    text = prompt or ""
    if typ == "hero":
        return append_vision_check_hint(append_hero_regen_hint(text))
    if typ in (
        "images",
        "hitl_images",
        "image_prompts",
        "videos",
        "hitl_videos",
        "animation_prompts",
    ):
        return append_vision_check_hint(text)
    return text


def check_agent_upload_path(project: Project, node_key: str) -> Path:
    """Фиксированное имя загруженного агента в папке ноды."""
    return upload_dir(project, node_key) / "check_agent.txt"


def load_custom_check_agent_body(project: Project, node_key: str) -> str | None:
    """Текст загруженного .txt/.md агента (если есть)."""
    cfg = operator_config(project, node_key)
    name = str(cfg.get("checkAgentFileName") or "").strip()
    candidates: list[Path] = []
    if name:
        candidates.append(upload_dir(project, node_key) / Path(name).name)
    candidates.append(check_agent_upload_path(project, node_key))
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file() or path.stat().st_size < 1:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            return text
    return None


def load_check_agent_view(
    project: Project, node_key: str
) -> dict[str, Any] | None:
    """Текст агента для кнопки «Просмотр»: свой файл или builtin check_operator."""
    from app.services.check_analysis import (
        load_check_operator_prompt_body,
        resolve_check_operator_step,
    )

    cfg = operator_config(project, node_key)
    custom = load_custom_check_agent_body(project, node_key)
    if custom:
        name = str(cfg.get("checkAgentFileName") or "").strip() or "check_agent.txt"
        return {
            "fileName": name,
            "chars": len(custom),
            "text": custom,
            "source": "upload",
        }
    typ = upstream_node_type_for_check(project, node_key)
    if not typ:
        return None
    step = resolve_check_operator_step(typ)
    body = load_check_operator_prompt_body(typ)
    if not body:
        return None
    return {
        "fileName": f"builtin:{step}",
        "chars": len(body),
        "text": body,
        "source": "builtin",
        "step": step,
    }


def assemble_check_agent_prompt(
    project: Project,
    node_key: str,
    *,
    check_fix: bool = True,
    reviewer_notes: str = "",
    report_format: str | None = None,
    db_sot: bool = False,
) -> tuple[str, str | None]:
    """Master-промт из загруженного .txt или prompts/check_operator.

    Returns (prompt_text, agent_label) — label: upload:name или step builtin.
    """
    from app.services.check_analysis import (
        append_txt_report_footer,
        load_check_operator_prompt_body,
        resolve_check_operator_step,
    )

    cfg = operator_config(project, node_key)
    custom = load_custom_check_agent_body(project, node_key)
    label: str | None = None
    body: str | None = custom
    if body:
        label = f"upload:{cfg.get('checkAgentFileName') or 'check_agent.txt'}"
    else:
        typ = upstream_node_type_for_check(project, node_key)
        if not typ:
            raise RuntimeError(
                "нет файла агента и нет вышестоящей ноды — "
                "загрузите .txt агента или проведите стрелку"
            )
        step = resolve_check_operator_step(typ)
        body = load_check_operator_prompt_body(typ)
        if not body:
            raise RuntimeError(
                f"нет готового агента проверки для типа «{typ}» "
                f"(prompts/check_operator/{step}/default.md) — загрузите свой .txt"
            )
        label = step

    fmt = report_format
    if fmt is None:
        fmt, _ = resolve_check_report_format(project, node_key)

    mode = "fix" if check_fix else "report_only"
    blocks: list[str] = [
        body,
        "",
        f"mode: {mode}",
        (
            (
                "Источник — db_check.json (DB SoT). Excel/TSV не используются. "
                "При mode=fix: отчёт + JSON apply-ops в базу."
                if check_fix
                else "Источник — db_check.json (DB SoT). Только отчёт, без Excel."
            )
            if db_sot
            else (
                "Вложение — текстовый экспорт xlsx (TSV) или файлы со стрелки. "
                "Бинарный .xlsx в рабочей директории модели может быть недоступен — это норма. "
                "При правках: после отчёта блок --- XLSX_WRITEBACK --- с `# Лист:` TSV; "
                "в forward укажи file: fixed."
                if check_fix
                else "НЕ изменяй файл — только отчёт (file: original)."
            )
        ),
        "Отвечай TXT-отчётом по шаблону формата ниже (НЕ JSON vp.check.v1).",
    ]
    notes = (reviewer_notes or "").strip()
    if notes:
        blocks.extend(["", "# Доп. указания ревьюера (эта нода)", notes])
    return append_txt_report_footer(
        "\n".join(blocks).strip(),
        report_format=fmt,
    ), label


def save_check_agent_file(
    project: Project, node_key: str, *, original_name: str, content: bytes
) -> dict[str, Any]:
    """Сохранить .txt/.md агента проверки в uploads ноды."""
    safe = Path(original_name or "check_agent.txt").name
    ext = Path(safe).suffix.lower()
    if ext not in {".txt", ".md"}:
        raise ValueError("нужен файл .txt или .md")
    if not content or not content.strip():
        raise ValueError("пустой файл агента")
    # Нормализуем в UTF-8 текст
    text = content.decode("utf-8", errors="replace").strip()
    if not text:
        raise ValueError("пустой файл агента")
    dest_dir = upload_dir(project, node_key)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = check_agent_upload_path(project, node_key)
    dest.write_text(text + "\n", encoding="utf-8")
    # также копия с оригинальным именем (удобно смотреть в папке)
    named = dest_dir / safe
    if named.resolve() != dest.resolve():
        named.write_text(text + "\n", encoding="utf-8")
    meta = dict(project.meta or {})
    configs = dict(meta.get("excel_gpt_nodes") or {})
    cur = dict(configs.get(node_key) or {})
    cur["checkAgentFileName"] = safe
    cur["checkPromptSource"] = "agent"
    cur["checkMode"] = True
    cur["transport"] = "api"
    configs[node_key] = cur
    meta["excel_gpt_nodes"] = configs
    project.meta = meta
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "meta")
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "fileName": safe,
        "path": str(dest),
        "chars": len(text),
        "resolve": resolve_operator(project, node_key),
    }


def clear_check_agent_file(project: Project, node_key: str) -> dict[str, Any]:
    """Убрать загруженный агент — снова builtin по типу вышестоящей ноды."""
    dest = check_agent_upload_path(project, node_key)
    dest.unlink(missing_ok=True)
    cfg = operator_config(project, node_key)
    name = str(cfg.get("checkAgentFileName") or "").strip()
    if name:
        (upload_dir(project, node_key) / Path(name).name).unlink(missing_ok=True)
    meta = dict(project.meta or {})
    configs = dict(meta.get("excel_gpt_nodes") or {})
    cur = dict(configs.get(node_key) or {})
    cur.pop("checkAgentFileName", None)
    configs[node_key] = cur
    meta["excel_gpt_nodes"] = configs
    project.meta = meta
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "meta")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "resolve": resolve_operator(project, node_key)}


def _file_probe(path: Path, *, origin: InputOrigin, from_node: str | None = None) -> dict[str, Any]:
    exists = path.is_file()
    size = int(path.stat().st_size) if exists else 0
    ok = exists and size > 0
    suffix = path.suffix.lower()
    kind = "other"
    if suffix in _IMAGE_SUFFIXES:
        kind = "image"
    elif suffix in _VIDEO_SUFFIXES:
        kind = "video"
    elif suffix in {".xlsx", ".xls"}:
        kind = "xlsx"
    elif suffix in {".txt", ".md", ".csv", ".json"}:
        kind = "text"
    preview = f"/api/files?path={path}" if ok and kind in ("image", "video", "text") else None
    return {
        "name": path.name,
        "path": str(path),
        "exists": exists,
        "size": size,
        "ok": ok,
        "kind": kind,
        "origin": origin,
        "fromNode": from_node,
        "preview_url": preview,
        "error": None if ok else ("файл отсутствует" if not exists else "файл пустой"),
    }


def _collect_under(root: Path, *, suffixes: frozenset[str], limit: int = 12) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in suffixes:
            continue
        if p.stat().st_size < 1:
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def _node_type_map(project: Project) -> dict[str, str]:
    meta = project.meta if isinstance(project.meta, dict) else {}
    cg = canvas_graph_from_meta(meta) or {}
    out: dict[str, str] = {}
    for n in cg.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "").strip()
        if nid:
            out[nid] = str(n.get("type") or "")
    return out


def _incoming_edges(project: Project, node_key: str) -> list[dict[str, Any]]:
    meta = project.meta if isinstance(project.meta, dict) else {}
    cg = canvas_graph_from_meta(meta) or {}
    edges = cg.get("edges") or []
    result: list[dict[str, Any]] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        if str(e.get("target") or "") != node_key:
            continue
        result.append(e)
    return result


def _outgoing_edges(project: Project, node_key: str) -> list[dict[str, Any]]:
    meta = project.meta if isinstance(project.meta, dict) else {}
    cg = canvas_graph_from_meta(meta) or {}
    edges = cg.get("edges") or []
    result: list[dict[str, Any]] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        if str(e.get("source") or "") != node_key:
            continue
        result.append(e)
    return result


def _snapshot_xlsx_for_node(project: Project, source_key: str) -> Path | None:
    meta = project.meta if isinstance(project.meta, dict) else {}
    snaps = meta.get("xlsx_snapshots_by_node")
    if not isinstance(snaps, dict):
        return None
    entry = snaps.get(source_key)
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip()
    # bind пишет «rel»; старые записи могли держать «path».
    rel = str(entry.get("rel") or entry.get("path") or "").strip().replace("\\", "/")
    candidates: list[Path] = []
    if rel:
        candidates.append(Path(rel))
        candidates.append(project.data_dir / rel)
    if name:
        candidates.append(project.data_dir / "old" / name)
        candidates.append(project.data_dir / name)
    for p in candidates:
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def _resolve_result_path(root: Path, item: Any) -> Path | None:
    raw = str(item or "").strip().replace("\\", "/")
    if not raw:
        return None
    p = Path(raw)
    if not p.is_file():
        p = root / raw
    if p.is_file() and p.stat().st_size > 0:
        return p
    return None


def _paths_for_emit_kinds(
    project: Project,
    source_key: str,
    entry: dict[str, Any],
    kinds: list[EmitKind],
    *,
    limit: int,
) -> list[Path]:
    """Собрать файлы по выбору «что отдаёт» у ноды-источника."""
    root = project.data_dir
    found: list[Path] = []
    seen: set[str] = set()

    def add(p: Path | None) -> None:
        if p is None or not p.is_file() or p.stat().st_size < 1:
            return
        key = str(p.resolve())
        if key in seen:
            return
        seen.add(key)
        found.append(p)

    outputs = [
        x
        for x in (_resolve_result_path(root, i) for i in (entry.get("outputPaths") or []))
        if x is not None
    ]
    inputs = [
        x
        for x in (_resolve_result_path(root, i) for i in (entry.get("inputPaths") or []))
        if x is not None
    ]

    if "result" in kinds:
        for p in outputs:
            if p.name not in _REPLY_TXT_NAMES and p.name not in _ANALYSIS_NAMES:
                add(p)
    if "reply_txt" in kinds:
        for p in outputs:
            if p.name in _REPLY_TXT_NAMES:
                add(p)
        # fallback: файл ответа в папке ноды
        up = upload_dir(project, source_key)
        for name in _REPLY_TXT_NAMES:
            add(up / name)
    if "analysis" in kinds:
        for p in outputs:
            if p.name in _ANALYSIS_NAMES:
                add(p)
        add(upload_dir(project, source_key) / "analysis.json")
    if "inputs" in kinds:
        for p in inputs:
            if p.name not in _ANALYSIS_NAMES:
                add(p)

    return found[:limit]


def files_from_source_node(
    project: Project,
    source_key: str,
    *,
    use_snapshot: bool = False,
    limit: int = 12,
) -> list[Path]:
    """Фактические файлы результата ноды-источника."""
    types = _node_type_map(project)
    typ = types.get(source_key, "")
    root = project.data_dir
    found: list[Path] = []

    if use_snapshot:
        snap = _snapshot_xlsx_for_node(project, source_key)
        if snap is not None:
            return [snap]

    # Явный результат оператора / excel_gpt
    meta = project.meta if isinstance(project.meta, dict) else {}
    results = meta.get("gpt_operator_results")
    entry: dict[str, Any] | None = None
    if isinstance(results, dict) and isinstance(results.get(source_key), dict):
        entry = dict(results[source_key])
    # canvas PATCH часто затирает gpt_operator_results — отчёт на диске остаётся.
    if is_excel_gpt_node_type(typ) or (
        entry is not None and is_check_operator(operator_config(project, source_key))
    ):
        entry = hydrate_check_result_from_disk(project, source_key, entry)

    if isinstance(entry, dict) and entry:
        # vp.check.v1 fix.rewrite_file → исправленный файл + отчёт из emitKinds.
        rewrite = str(entry.get("fixRewriteFile") or "").strip().replace("\\", "/")
        if rewrite:
            rp = Path(rewrite)
            if not rp.is_file():
                rp = root / rewrite
            if rp.is_file() and rp.stat().st_size > 0:
                found = [rp]
                src_cfg = operator_config(project, source_key)
                emit_kinds: list[EmitKind] = list(src_cfg.get("emitKinds") or [])
                report_kinds = [k for k in emit_kinds if k in ("reply_txt", "analysis")]
                if report_kinds:
                    found.extend(
                        _paths_for_emit_kinds(
                            project,
                            source_key,
                            entry,
                            report_kinds,
                            limit=limit,
                        )
                    )
                return found[:limit]
        # vp.check.v1 forward.explicit → только указанные пути
        fwd_paths = entry.get("forwardPaths")
        if isinstance(fwd_paths, list) and fwd_paths:
            for item in fwd_paths:
                p = _resolve_result_path(root, item)
                if p is not None:
                    found.append(p)
            if found:
                # Даже при forward — докинуть check_report, если emit это просит.
                src_cfg = operator_config(project, source_key)
                emit_kinds = list(src_cfg.get("emitKinds") or [])
                report_kinds = [k for k in emit_kinds if k in ("reply_txt", "analysis")]
                if report_kinds:
                    found.extend(
                        _paths_for_emit_kinds(
                            project,
                            source_key,
                            entry,
                            report_kinds,
                            limit=limit,
                        )
                    )
                return found[:limit]
        # Выбор «что отдаёт» на ноде-источнике (emitKinds).
        src_cfg = operator_config(project, source_key)
        emit_kinds: list[EmitKind] = list(src_cfg.get("emitKinds") or [])
        emitted = _paths_for_emit_kinds(
            project, source_key, entry, emit_kinds, limit=limit
        )
        if emitted:
            return emitted
        # Fallback, если emitKinds ничего не нашёл (ещё нет результата).
        for key in ("outputPaths", "inputPaths"):
            raw = entry.get(key) or []
            if isinstance(raw, list):
                for item in raw:
                    p = _resolve_result_path(root, item)
                    if p is not None and p.name not in _ANALYSIS_NAMES:
                        found.append(p)
        if found:
            return found[:limit]

    if typ in ("images", "hitl_images") or typ == "image_prompts":
        found = _collect_under(root / "scenes", suffixes=_IMAGE_SUFFIXES, limit=limit)
        if not found:
            found = _collect_under(root / "images", suffixes=_IMAGE_SUFFIXES, limit=limit)
        return found

    if typ in ("videos", "hitl_videos", "animation_prompts"):
        return _collect_under(root / "videos", suffixes=_VIDEO_SUFFIXES, limit=limit)

    if typ == "hero":
        return _collect_under(root / "characters", suffixes=_IMAGE_SUFFIXES, limit=limit)

    if typ == "storage":
        from app.services.storage_node import stored_paths

        return stored_paths(project, source_key, limit=limit)

    if typ in ("audio", "music"):
        voice = root / "voiceover.txt"
        if voice.is_file():
            return [voice]
        return _collect_under(root / "audio", suffixes=frozenset({".mp3", ".wav", ".m4a", ".txt"}), limit=limit)

    # Закадровый текст: главный артефакт — voiceover.txt (+ xlsx если есть).
    if typ == "script":
        out: list[Path] = []
        voice = root / "voiceover.txt"
        if voice.is_file() and voice.stat().st_size > 0:
            out.append(voice)
        xlsx = root / "project.xlsx"
        if xlsx.is_file():
            out.append(xlsx)
        if out:
            return out[:limit]

    if is_excel_gpt_node_type(typ):
        # emitKinds с диска uploads — даже если meta.gpt_operator_results стёрта.
        src_cfg = operator_config(project, source_key)
        emit_kinds: list[EmitKind] = list(src_cfg.get("emitKinds") or [])
        synthetic: dict[str, Any] = {"outputPaths": [], "inputPaths": []}
        udir = upload_dir(project, source_key)
        if udir.is_dir():
            for p in sorted(udir.iterdir()):
                if p.is_file() and p.stat().st_size > 0:
                    synthetic["outputPaths"].append(str(p))
        emitted = _paths_for_emit_kinds(
            project, source_key, synthetic, emit_kinds, limit=limit
        )
        if emitted:
            return emitted
        if use_snapshot:
            snap = _snapshot_xlsx_for_node(project, source_key)
            if snap is not None:
                return [snap]
        # checkMode: не подменять отчёт голым project.xlsx
        if not is_check_operator(src_cfg):
            xlsx = root / "project.xlsx"
            if xlsx.is_file():
                return [xlsx]

    if typ in ("plan", "split", "items", "excel_feed"):
        if use_snapshot:
            snap = _snapshot_xlsx_for_node(project, source_key)
            if snap is not None:
                return [snap]
        xlsx = root / "project.xlsx"
        if xlsx.is_file():
            return [xlsx]

    # fallback: uploads этой ноды (включая check_report / gpt_reply)
    udir = upload_dir(project, source_key)
    if udir.is_dir():
        for p in sorted(udir.iterdir()):
            if not p.is_file() or p.stat().st_size < 1:
                continue
            if p.suffix.lower() not in _ANY_SUFFIXES and p.name not in _REPLY_TXT_NAMES:
                continue
            found.append(p)
            if len(found) >= limit:
                break
    return found


def _manual_upload_files(project: Project, node_key: str, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    names = list(cfg.get("uploadedFileNames") or [])
    for name in names:
        p = upload_dir(project, node_key) / Path(name).name
        files.append(_file_probe(p, origin="upload"))
    # legacy single attachment_paths for non-edge sources when no uploads listed
    if not files:
        for p in attachment_paths(project, node_key):
            # skip if it's only project.xlsx and role expects edge — still include as project
            origin: InputOrigin = "upload" if "excel_gpt_uploads" in str(p) else "project"
            files.append(_file_probe(p, origin=origin))
    return files


def hydrate_check_result_from_disk(
    project: Project, node_key: str, last: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Подтянуть gate/analysis с диска, если meta протухла после re-run.

    Файлы analysis.json / check_report.txt — источник правды после прогона;
    canvas/web_get иногда перезаписывают meta без gpt_operator_results.
    """
    import json

    from app.services.check_analysis import CHECK_REPORT_NAME, parse_check_analysis

    entry = dict(last or {})
    up = upload_dir(project, node_key)
    analysis_path = up / "analysis.json"
    report_path = up / CHECK_REPORT_NAME
    reply_path = up / "gpt_reply.txt"

    def _merge_upload_paths(dst: dict[str, Any]) -> dict[str, Any]:
        """Всегда тянуть артефакты с диска — meta часто без outputPaths."""
        outs = [str(p) for p in (dst.get("outputPaths") or []) if str(p).strip()]
        for p in (analysis_path, report_path, reply_path):
            if p.is_file() and str(p) not in outs:
                outs.append(str(p))
        if up.is_dir():
            for p in sorted(up.iterdir()):
                if (
                    p.is_file()
                    and p.stat().st_size > 0
                    and p.suffix.lower() in _ANY_SUFFIXES
                    and str(p) not in outs
                ):
                    outs.append(str(p))
        dst["outputPaths"] = outs
        return dst

    gate = str(entry.get("gateStatus") or "").strip().lower()
    if gate in ("pass", "fail") and isinstance(entry.get("analysis"), dict):
        return _merge_upload_paths(entry)

    parsed_dict: dict[str, Any] | None = None
    preview = ""
    if analysis_path.is_file() and analysis_path.stat().st_size > 0:
        try:
            raw = json.loads(analysis_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("verdict"):
                parsed_dict = raw
        except Exception:  # noqa: BLE001
            parsed_dict = None
    if parsed_dict is None and report_path.is_file() and report_path.stat().st_size > 0:
        preview = report_path.read_text(encoding="utf-8")
        parsed = parse_check_analysis(preview)
        if parsed.raw_error is None or parsed.verdict in ("pass", "fail"):
            parsed_dict = parsed.to_dict()
    if parsed_dict is None and reply_path.is_file() and reply_path.stat().st_size > 0:
        preview = reply_path.read_text(encoding="utf-8")
        if "ОТЧЁТ ПРОВЕРКИ" in preview or "vp.check.v1" in preview or "verdict" in preview.lower():
            parsed = parse_check_analysis(preview)
            parsed_dict = parsed.to_dict()

    def _fill_reply_preview(dst: dict[str, Any]) -> dict[str, Any]:
        """assist/project_file: показать gpt_reply даже без check-verdict."""
        if str(dst.get("replyPreview") or "").strip():
            return dst
        for src in (reply_path, report_path):
            if src.is_file() and src.stat().st_size > 0:
                try:
                    dst["replyPreview"] = src.read_text(encoding="utf-8")[:2000]
                    break
                except Exception:  # noqa: BLE001
                    continue
        return dst

    if not parsed_dict:
        # Даже без verdict — отдать файлы uploads + текст ответа GPT.
        if report_path.is_file() or reply_path.is_file():
            return _fill_reply_preview(_merge_upload_paths(entry))
        return entry

    verdict = str(parsed_dict.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "fail"):
        return _fill_reply_preview(_merge_upload_paths(entry))

    entry["gateStatus"] = verdict
    entry["analysis"] = parsed_dict
    if not preview and report_path.is_file():
        preview = report_path.read_text(encoding="utf-8")
    if preview:
        entry["replyPreview"] = preview[:2000]
    return _fill_reply_preview(_merge_upload_paths(entry))


def sanitize_check_reviewer_notes(text: str) -> str:
    """Убрать из сопровода старый JSON-контракт — он ломает TXT-отчёт."""
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower()
    if "vp.check.v1" in low:
        return ""
    if '"schema"' in low and "verdict" in low and "forward" in low:
        return ""
    return t


def resolve_operator(project: Project, node_key: str) -> dict[str, Any]:
    """Полная сверка: стрелки + слоты + диск. Единый ответ для UI и run."""
    cfg = operator_config(project, node_key)
    role: OperatorRole = cfg["role"]
    output_mode: OutputMode = cfg["outputMode"]
    emit_kinds: list[EmitKind] = list(cfg.get("emitKinds") or [])
    use_snapshot = bool(cfg.get("useSnapshot"))
    take_from_edges = bool(cfg.get("takeFromEdges", True))
    errors: list[str] = []
    warnings: list[str] = []

    incoming = _incoming_edges(project, node_key)
    edge_summaries: list[dict[str, Any]] = []
    edge_files: list[dict[str, Any]] = []
    check_mode = bool(cfg.get("checkMode"))
    # project_file = DB SoT (apply-ops). Excel больше НЕ подставляем и не
    # требуем: enrich_xlsx сам отдаёт db_frames.json.
    db_sot = output_mode == "project_file" and not check_mode

    for e in incoming:
        kind = edge_kind_of(e)
        src = str(e.get("source") or "")
        eid = str(e.get("id") or f"{src}->{node_key}")
        summary: dict[str, Any] = {
            "id": eid,
            "source": src,
            "target": node_key,
            "kind": kind,
            "fileCount": 0,
            "ok": True,
            "errors": [],
            "takesFiles": take_from_edges,
        }
        if is_verdict_edge_kind(kind):
            src_cfg = operator_config(project, src) if src else {}
            src_role = str(src_cfg.get("role") or "")
            if src_role not in BRANCHING_ROLES and not is_excel_gpt_node_type(
                _node_type_map(project).get(src, "")
            ):
                warnings.append(
                    f"стрелка {kind} от {src}: ожидается роль «проверяет» / «шлагбаум»"
                )
        # Любая входящая связь — кандидат на файлы; решает takeFromEdges у этой ноды.
        if take_from_edges and src:
            # Vision-check: hero/scenes/videos часто >12 файлов — не режем на 12.
            edge_limit = 80 if is_check_operator(cfg) else 12
            paths = files_from_source_node(
                project,
                src,
                use_snapshot=use_snapshot,
                limit=edge_limit,
            )
            if not paths:
                msg = f"у ноды {src} нет файлов на диске (вход со стрелки)"
                if is_fail_edge_kind(kind):
                    warnings.append(
                        f"{msg} — петля «Не ок»: на первом ▶ файлов проверки ещё нет"
                    )
                    summary["takesFiles"] = False
                elif db_sot:
                    warnings.append(
                        f"{msg} — project_file/DB SoT: ок, будет db_frames.json"
                    )
                else:
                    summary["ok"] = False
                    summary["errors"].append(msg)
                    errors.append(msg)
            for p in paths:
                probe = _file_probe(
                    p,
                    origin="snapshot" if use_snapshot else "edge",
                    from_node=src,
                )
                edge_files.append(probe)
                if not probe["ok"]:
                    summary["ok"] = False
                    summary["errors"].append(probe["error"] or "файл битый")
                    errors.append(f"{src}: {probe['error']}")
            summary["fileCount"] = len(paths)
        edge_summaries.append(summary)

    manual = _manual_upload_files(project, node_key, cfg)
    # Явная загрузка Excel/файла — она и есть вход. Со стрелок не мешаем
    # старый project.xlsx (иначе «загрузил — а в списке старый»).
    src = str(cfg.get("inputSource") or "project_xlsx")
    has_upload_xlsx = any(
        str(f.get("name") or "").lower().endswith((".xlsx", ".xlsm", ".xls"))
        and f.get("origin") == "upload"
        and f.get("ok")
        for f in manual
    )
    if src in ("upload", "image") and (has_upload_xlsx or (manual and src == "image")):
        take_from_edges = False
        edge_files = []
        for s in edge_summaries:
            s["takesFiles"] = False
            s["fileCount"] = 0
    elif take_from_edges and incoming:
        # Если берём со стрелок — legacy project.xlsx из inputSource не дублируем.
        manual = [f for f in manual if f.get("origin") == "upload"]

    all_files = [*manual, *edge_files] if (src in ("upload", "image") and manual) else [*edge_files, *manual]
    # de-dupe by path
    seen: set[str] = set()
    unique_files: list[dict[str, Any]] = []
    for f in all_files:
        key = str(f.get("path") or "")
        if key in seen:
            continue
        seen.add(key)
        unique_files.append(f)

    if db_sot:
        before = len(unique_files)
        unique_files = [
            f
            for f in unique_files
            if not str(f.get("name") or "").lower().endswith(
                (".xlsx", ".xlsm", ".xls")
            )
        ]
        dropped = before - len(unique_files)
        if dropped:
            warnings.append(
                f"project_file/DB SoT: убрано xlsx со входа ({dropped}) — "
                "пишем в базу через apply-ops"
            )

    ok_files = [f for f in unique_files if f.get("ok")]
    for f in unique_files:
        if not f.get("ok"):
            errors.append(f"{f.get('name')}: {f.get('error') or 'файл недоступен'}")
    if role == "compare" and len(ok_files) < 2:
        errors.append("роль «сравнить» требует минимум 2 существующих файла на входе")
    if role == "gate" and not ok_files and not incoming:
        warnings.append("шлагбаум без входных файлов — проверка будет только по промту")
    if not take_from_edges and incoming:
        warnings.append("вход со стрелок выключен — файлы прошлых нод не берутся")

    check_fix = bool(cfg.get("checkFix", True))
    check_prompt_source = str(cfg.get("checkPromptSource") or "upstream")
    if check_prompt_source not in VALID_CHECK_PROMPT_SOURCES:
        check_prompt_source = "upstream"
    source_prompts: list[dict[str, Any]] = []
    check_agent_step: str | None = None
    if check_mode:
        if check_prompt_source == "agent":
            from app.services.check_analysis import (
                load_check_operator_prompt_body,
                resolve_check_operator_step,
            )

            custom = load_custom_check_agent_body(project, node_key)
            if custom:
                check_agent_step = (
                    f"upload:{cfg.get('checkAgentFileName') or 'check_agent.txt'}"
                )
            else:
                typ = upstream_node_type_for_check(project, node_key)
                if not typ:
                    errors.append(
                        "готовый агент: загрузите .txt/.md или проведите стрелку "
                        "от результата (builtin по типу ноды)"
                    )
                else:
                    check_agent_step = resolve_check_operator_step(typ)
                    if not load_check_operator_prompt_body(typ):
                        errors.append(
                            f"нет builtin-агента для «{typ}» — загрузите свой .txt "
                            f"(prompts/check_operator/{check_agent_step}/default.md)"
                        )
        else:
            source_prompts = collect_source_prompts(project, node_key)
            ok_prompts = [s for s in source_prompts if s.get("ok")]
            if not source_prompts:
                errors.append("нет исходного промта для проверки (нет входящих стрелок)")
            elif not ok_prompts:
                errors.append("нет исходного промта для проверки")
            for s in source_prompts:
                if not s.get("ok") and s.get("error"):
                    warnings.append(
                        f"промт {s.get('nodeKey')}: {s.get('error')}"
                    )

    if not ok_files and role in ("assist", "transform", "extract", "review"):
        if db_sot:
            warnings.append(
                "project_file/DB SoT: входных файлов нет — ок, будет db_frames.json"
            )
        else:
            # soft: assist без файлов — ошибка запуска
            errors.append("нет ни одного существующего файла на входе")
    elif not ok_files and check_mode:
        errors.append("нет ни одного существующего файла на входе")

    # исходящие стрелки + ветки ок/не ок
    outgoing = []
    for e in _outgoing_edges(project, node_key):
        outgoing.append(
            {
                "id": str(e.get("id") or ""),
                "source": node_key,
                "target": str(e.get("target") or ""),
                "kind": edge_kind_of(e),
            }
        )

    last = {}
    meta = project.meta if isinstance(project.meta, dict) else {}
    results = meta.get("gpt_operator_results")
    if isinstance(results, dict) and isinstance(results.get(node_key), dict):
        last = dict(results[node_key])
    # Всегда с диска: canvas PATCH / clear_step часто стирают gpt_operator_results,
    # а gpt_reply.txt остаётся — иначе UI «результат пуст» при готовом ответе.
    last = hydrate_check_result_from_disk(project, node_key, last)

    pass_edges = [e for e in outgoing if is_pass_edge_kind(str(e.get("kind") or ""))]
    fail_edges = [e for e in outgoing if is_fail_edge_kind(str(e.get("kind") or ""))]
    branching_enabled = role in BRANCHING_ROLES or check_mode
    if branching_enabled:
        if not pass_edges:
            warnings.append(
                "нет исходящей стрелки «Ок» — проведите связь и выберите тип «Ок»"
            )
        if not fail_edges:
            warnings.append(
                "нет исходящей стрелки «Не ок» — проведите связь и выберите тип «Не ок»"
            )

    verdict = str(last.get("gateStatus") or cfg.get("gateStatus") or "").strip().lower()
    if verdict not in ("pass", "fail"):
        verdict = ""

    analysis_out: dict[str, Any] | None = None
    raw_analysis = last.get("analysis") if isinstance(last.get("analysis"), dict) else None
    if raw_analysis:
        analysis_out = dict(raw_analysis)
    elif verdict:
        # минимальная карточка, если analysis.json ещё не было
        analysis_out = {
            "schema": "vp.check.v1",
            "verdict": verdict,
            "summary": str(cfg.get("lastSummary") or last.get("replyPreview") or "")[:500],
            "checks": [],
            "forward": {
                "mode": "explicit" if last.get("forwardPaths") else "inherit",
                "paths": list(last.get("forwardPaths") or []),
            },
            "fix": {"target": "none", "instructions": "", "rewrite_file": None},
        }

    consistent = len(errors) == 0
    from app.services.check_analysis import default_check_report_format

    report_format_text, report_format_custom = resolve_check_report_format(
        project, node_key
    )
    source_prompt_view = [
        {
            "nodeKey": s.get("nodeKey"),
            "nodeType": s.get("nodeType"),
            "stepCode": s.get("stepCode"),
            "ok": bool(s.get("ok")),
            "chars": int(s.get("chars") or 0),
            "variant": s.get("variant"),
            "source": s.get("source"),
            "path": s.get("path"),
            "error": s.get("error"),
        }
        for s in source_prompts
    ]
    return {
        "nodeKey": node_key,
        "nodeType": EXCEL_GPT_NODE_TYPE,
        "role": role,
        "outputMode": output_mode,
        "emitKinds": emit_kinds,
        "useSnapshot": use_snapshot,
        "takeFromEdges": take_from_edges,
        "checkMode": check_mode,
        "checkFix": check_fix,
        "checkPromptSource": check_prompt_source,
        "checkAgentStep": check_agent_step,
        "checkAgentFileName": str(cfg.get("checkAgentFileName") or "") or None,
        "checkAgentChars": (
            len(load_custom_check_agent_body(project, node_key) or "")
            if check_prompt_source == "agent"
            else 0
        ),
        "checkReportFormat": report_format_text,
        "checkReportFormatCustom": report_format_custom,
        "checkReportFormatDefault": default_check_report_format(),
        "sourcePrompts": source_prompt_view,
        "transport": cfg.get("transport") or "api",
        "label": str(cfg.get("label") or default_label_for_role(role)),
        "files": unique_files,
        "okFileCount": len(ok_files),
        "incomingEdges": edge_summaries,
        "outgoingEdges": outgoing,
        "branching": {
            "enabled": branching_enabled,
            "passEdges": pass_edges,
            "failEdges": fail_edges,
            "hasPass": len(pass_edges) > 0,
            "hasFail": len(fail_edges) > 0,
            "verdict": verdict or None,
        },
        "analysis": analysis_out,
        "errors": errors,
        "warnings": warnings,
        "consistent": consistent,
        "canRun": consistent
        and (len(ok_files) > 0 or role == "gate" or db_sot),
        "lastResult": last,
        "config": {
            "role": role,
            "outputMode": output_mode,
            "emitKinds": emit_kinds,
            "useSnapshot": use_snapshot,
            "takeFromEdges": take_from_edges,
            "checkMode": check_mode,
            "checkFix": check_fix,
            "checkPromptSource": check_prompt_source,
            "checkAgentFileName": str(cfg.get("checkAgentFileName") or "") or None,
            "checkReportFormat": (
                report_format_text if report_format_custom else None
            ),
            "transport": cfg.get("transport") or "api",
            "uploadedFileNames": list(cfg.get("uploadedFileNames") or []),
            "workMode": cfg.get("workMode"),
            "inputSource": cfg.get("inputSource"),
            "label": str(cfg.get("label") or default_label_for_role(role)),
            "gateStatus": verdict or None,
            "lastSummary": str(cfg.get("lastSummary") or "")[:500] or None,
        },
    }


def patch_operator_config(project: Project, node_key: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Обновить meta.excel_gpt_nodes[nodeKey] и вернуть свежий resolve."""
    meta = dict(project.meta or {})
    configs = dict(meta.get("excel_gpt_nodes") or {})
    cur = dict(configs.get(node_key) or {})

    role_changed = False
    if "role" in patch:
        role = normalize_role(patch.get("role"))
        role_changed = normalize_role(cur.get("role") or cur.get("workMode") or "assist") != role
        cur["role"] = role
        cur["transport"] = str(cur.get("transport") or "api")
        if cur["transport"] not in ("api", "browser"):
            cur["transport"] = "api"
        if role in ("assist", "review", "transform"):
            cur["workMode"] = role
        elif role in ("extract", "compare", "gate"):
            cur["workMode"] = "review"
    if "workMode" in patch and "role" not in patch:
        role = normalize_role(patch.get("workMode"))
        role_changed = normalize_role(cur.get("role") or cur.get("workMode") or "assist") != role
        cur["role"] = role
        cur["workMode"] = role if role in ("assist", "review", "transform") else "assist"
    if "outputMode" in patch:
        cur["outputMode"] = normalize_output_mode(
            patch.get("outputMode"),
            role=normalize_role(cur.get("role") or "assist"),
        )
    if "emitKinds" in patch:
        cur["emitKinds"] = normalize_emit_kinds(
            patch.get("emitKinds"),
            role=normalize_role(cur.get("role") or "assist"),
        )
    if "useSnapshot" in patch:
        cur["useSnapshot"] = bool(patch.get("useSnapshot"))
    if "takeFromEdges" in patch:
        cur["takeFromEdges"] = bool(patch.get("takeFromEdges"))
    if "checkMode" in patch:
        cur["checkMode"] = bool(patch.get("checkMode"))
        if cur["checkMode"]:
            # При включении — разумные дефолты отчёта, если emit ещё не выбран.
            if not (isinstance(cur.get("emitKinds"), list) and cur.get("emitKinds")):
                cur["emitKinds"] = ["inputs", "reply_txt"]
            if not str(cur.get("outputMode") or "").strip():
                cur["outputMode"] = "text"
            if "checkFix" not in cur:
                cur["checkFix"] = True
            if "checkPromptSource" not in cur:
                cur["checkPromptSource"] = "upstream"
    if "checkFix" in patch:
        cur["checkFix"] = bool(patch.get("checkFix"))
    if "checkPromptSource" in patch:
        cps = str(patch.get("checkPromptSource") or "upstream").strip().lower()
        cur["checkPromptSource"] = (
            cps if cps in VALID_CHECK_PROMPT_SOURCES else "upstream"
        )
    if "checkReportFormat" in patch:
        from app.services.check_analysis import normalize_check_report_format

        raw = patch.get("checkReportFormat")
        if raw is None:
            cur.pop("checkReportFormat", None)
        else:
            custom = normalize_check_report_format(str(raw))
            if custom:
                cur["checkReportFormat"] = custom
            else:
                cur.pop("checkReportFormat", None)
    if "checkAgentFileName" in patch:
        name = str(patch.get("checkAgentFileName") or "").strip()
        if name:
            cur["checkAgentFileName"] = Path(name).name
        else:
            cur.pop("checkAgentFileName", None)
    if "transport" in patch:
        t = str(patch.get("transport") or "api").strip().lower()
        cur["transport"] = t if t in ("api", "browser") else "api"
    if "label" in patch and patch["label"] is not None:
        cur["label"] = str(patch["label"])
    if "uploadedFileNames" in patch and isinstance(patch["uploadedFileNames"], list):
        cur["uploadedFileNames"] = [str(x) for x in patch["uploadedFileNames"] if x]
        if cur["uploadedFileNames"]:
            cur["uploadedFileName"] = cur["uploadedFileNames"][0]
            cur["inputSource"] = "upload"
    if "inputSource" in patch:
        cur["inputSource"] = patch["inputSource"]
    if "uploadedFileName" in patch:
        cur["uploadedFileName"] = patch["uploadedFileName"]

    configs[node_key] = cur
    meta["excel_gpt_nodes"] = configs
    meta["active_excel_gpt_node_key"] = node_key
    project.meta = meta
    return resolve_operator(project, node_key)


def set_edge_kind_in_canvas(
    project: Project, edge_id: str, kind: EdgeKind
) -> dict[str, Any] | None:
    """Меняет edge.data.kind в canvas_graph. Возвращает обновлённое ребро или None."""
    meta = dict(project.meta or {})
    cg = canvas_graph_from_meta(meta)
    if not cg:
        return None
    edges = list(cg.get("edges") or [])
    updated = None
    new_edges: list[dict[str, Any]] = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        if str(e.get("id") or "") != edge_id:
            new_edges.append(e)
            continue
        data = dict(e.get("data") or {})
        data["kind"] = normalize_edge_kind(kind)
        ne = {**e, "data": data}
        new_edges.append(ne)
        updated = ne
    if updated is None:
        return None
    raw = dict(meta.get("canvas_graph") or {})
    raw["edges"] = new_edges
    raw["nodes"] = list(cg.get("nodes") or [])
    meta["canvas_graph"] = raw
    project.meta = meta
    return updated


def apply_check_reply(
    project: Project,
    node_key: str,
    reply_text: str,
    *,
    input_paths: list[Path] | None = None,
    extra_output_paths: list[Path] | None = None,
) -> dict[str, Any]:
    """Разобрать ответ проверки → analysis.json + gateStatus в meta.

    Для browser и API путей. Битый JSON → fail (ветка «Не ок»).
    """
    from app.services.check_analysis import parse_check_analysis, write_analysis_json
    from app.services.excel_gpt_node import upload_dir

    from app.services.check_analysis import write_check_report_txt

    parsed = parse_check_analysis(reply_text or "")
    cfg = operator_config(project, node_key)
    mode = "fix" if cfg.get("checkFix", True) else "report_only"
    # report_only: не прокидываем rewrite дальше, даже если модель указала path.
    if mode == "report_only":
        parsed.fix.rewrite_file = None
        parsed.forward = type(parsed.forward)(mode="inherit", paths=[])
    out_dir = upload_dir(project, node_key)
    analysis_path = write_analysis_json(out_dir, parsed)
    sources = [
        str(s.get("nodeKey") or "")
        for s in collect_source_prompts(project, node_key)
        if s.get("ok")
    ]
    report_path = write_check_report_txt(
        out_dir, parsed, mode=mode, source_prompts=sources
    )
    outputs = [analysis_path, report_path, *(extra_output_paths or [])]
    reply_file = out_dir / "gpt_reply.txt"
    if (reply_text or "").strip() and not reply_file.is_file():
        reply_file.write_text((reply_text or "").strip() + "\n", encoding="utf-8")
        outputs.append(reply_file)
    elif reply_file.is_file() and reply_file not in outputs:
        outputs.append(reply_file)
    return save_operator_result(
        project,
        node_key,
        input_paths=list(input_paths or []),
        output_paths=outputs,
        reply_text=reply_text or "",
        gate_status=parsed.verdict,
        analysis=parsed.to_dict(),
    )


def save_operator_result(
    project: Project,
    node_key: str,
    *,
    input_paths: list[Path],
    output_paths: list[Path],
    reply_text: str,
    gate_status: str | None = None,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    from app.services.check_analysis import parse_check_analysis

    # Если вердикт не передан явно — пробуем разобрать ответ (vp.check.v1).
    resolved_gate = (gate_status or "").strip().lower() or None
    analysis_dict = analysis
    if analysis_dict is None and reply_text:
        cfg = operator_config(project, node_key)
        if is_check_operator(cfg):
            parsed = parse_check_analysis(reply_text)
            analysis_dict = parsed.to_dict()
            if resolved_gate not in ("pass", "fail"):
                resolved_gate = parsed.verdict

    from app.services.project_meta import set_meta_fields

    meta = project.meta if isinstance(project.meta, dict) else {}
    results = dict(meta.get("gpt_operator_results") or {})
    entry: dict[str, Any] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "inputPaths": [str(p) for p in input_paths],
        "outputPaths": [str(p) for p in output_paths],
        "replyPreview": (reply_text or "")[:2000],
        "gateStatus": resolved_gate,
    }
    if analysis_dict:
        entry["analysis"] = analysis_dict
        fwd = analysis_dict.get("forward") if isinstance(analysis_dict, dict) else None
        if isinstance(fwd, dict) and fwd.get("mode") == "explicit":
            paths = fwd.get("paths") if isinstance(fwd.get("paths"), list) else []
            entry["forwardPaths"] = [str(p) for p in paths]
        # Если агент сам исправил файл — запоминаем путь исправленной версии.
        fix = analysis_dict.get("fix") if isinstance(analysis_dict, dict) else None
        if isinstance(fix, dict):
            rewrite = str(fix.get("rewrite_file") or "").strip()
            if rewrite:
                entry["fixRewriteFile"] = rewrite
    results[node_key] = entry
    # mirror last reply into excel_gpt node config for UI
    configs = dict(meta.get("excel_gpt_nodes") or {})
    cur = dict(configs.get(node_key) or {})
    if output_paths:
        # Предпочитаем человекочитаемый отчёт, не analysis.json.
        preferred = next(
            (p for p in output_paths if p.name == "check_report.txt"),
            None,
        )
        cur["lastReplyPath"] = str(preferred or output_paths[0])
        cur["lastReplyAt"] = entry["at"]
    if resolved_gate:
        cur["gateStatus"] = resolved_gate
    if analysis_dict and isinstance(analysis_dict, dict):
        cur["lastSummary"] = str(analysis_dict.get("summary") or "")[:500]
    configs[node_key] = cur
    # Точечно — не project.meta = dict(...), иначе гонка с canvas PATCH.
    set_meta_fields(
        project,
        gpt_operator_results=results,
        excel_gpt_nodes=configs,
    )
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "meta")
    except Exception:  # noqa: BLE001
        pass
    return entry


def gate_allows_successors(project: Project, gate_node_key: str) -> bool | None:
    """None — нода без вердикта / не branching-роль; True/False — pass/fail."""
    cfg = operator_config(project, gate_node_key)
    if not is_check_operator(cfg):
        return None
    meta = project.meta if isinstance(project.meta, dict) else {}
    results = meta.get("gpt_operator_results")
    entry: dict[str, Any] = {}
    if isinstance(results, dict) and isinstance(results.get(gate_node_key), dict):
        entry = dict(results[gate_node_key])
    entry = hydrate_check_result_from_disk(project, gate_node_key, entry)
    status = str(entry.get("gateStatus") or cfg.get("gateStatus") or "").strip().lower()
    if status == "pass":
        return True
    if status == "fail":
        return False
    return None


def verdict_edge_blocks(
    project: Project, source_key: str, edge_kind: str
) -> bool | None:
    """Блокирует ли стрелка pass/fail/gate переход.

    None — стрелка не вердиктная (после/файлы/проверка).
    True — блок; False — можно идти.
    """
    kind = normalize_edge_kind(edge_kind)
    if not is_verdict_edge_kind(kind):
        return None
    allowed = gate_allows_successors(project, source_key)
    if allowed is None:
        return True  # нет вердикта → ни одна ветка не открыта
    if is_pass_edge_kind(kind):
        return allowed is not True
    # fail
    return allowed is not False


def upstream_node_type_for_check(project: Project, node_key: str) -> str | None:
    """Тип рабочей ноды выше по стрелке от проверочной ноды.

    Проверочная нода стоит «ниже» рабочей и валидирует её выход. Берём
    источник входящей стрелки (приоритет — вердиктные/данные), это и есть
    рабочая нода, чей результат проверяем.
    """
    from app.orchestrator.node_registry import is_work_node_type

    types = _node_type_map(project)
    incoming = _incoming_edges(project, node_key)

    def _pick(edges: list[dict[str, Any]]) -> str | None:
        for e in edges:
            src = str(e.get("source") or "")
            typ = types.get(src, "")
            if typ and (is_work_node_type(typ) or typ.startswith("hitl_")):
                return typ
        return None

    # Сначала вердиктные/gate-стрелки (типичная связь «работа → проверка»),
    # затем любые входящие.
    priority = [e for e in incoming if edge_kind_of(e) in ("after", "gate", "pass", "fail")]
    return _pick(priority) or _pick(incoming)


def default_check_prompt_for_node(project: Project, node_key: str) -> str | None:
    """Универсальный агент-проверки по типу вышестоящей рабочей ноды.

    Возвращает текст промта из `prompts/check_operator/<step>/default.md`
    (с хвостом схемы vp.check.v1) или None, если вышестоящую ноду не нашли.
    """
    from app.services.check_analysis import load_check_operator_prompt

    typ = upstream_node_type_for_check(project, node_key)
    if not typ:
        return None
    return load_check_operator_prompt(typ)


def project_format_hint_for_check(project: Project, node_key: str) -> str:
    """Блок «целевой формат проекта» — только для визуальных вышестоящих нод.

    Формат/разрешение берём из полей проекта (не хардкодим в промтах).
    """
    from app.services.check_analysis import VISUAL_CHECK_TYPES, format_target_hint

    typ = upstream_node_type_for_check(project, node_key)
    if not typ or typ not in VISUAL_CHECK_TYPES:
        return ""
    return format_target_hint(
        getattr(project, "aspect_ratio", None),
        getattr(project, "image_resolution", None),
        getattr(project, "video_resolution", None),
    )

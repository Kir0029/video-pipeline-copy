"""Сборщик «сопр. сообщения» (full_prompt / chat_msg) для шагов пайплайна,
которые шлют текстовый запрос в ChatGPT.

Зачем нужен этот модуль:
  - На каждом этапе бот собирает один длинный текст и отправляет его в GPT
    (через `ask_fresh`, `ask_with_file`, `ask_with_files`). Этот текст
    собирается из мастер-промта (выбранный вариант с диска) + контекстных
    данных проекта (тема, плана, сценария, ссылок на прикреплённые файлы).
  - Пользователь хочет иметь возможность отредактировать ИМЕННО ЭТОТ текст
    перед запуском шага. Для этого мы:
      1) Выносим сборку дефолтного текста в отдельные функции (`build_*`).
      2) Храним пользовательский override в поле `Project.gpt_text_overrides`
         (JSON: {step_code -> текст}).
      3) Возвращаем «эффективный» текст — override если задан, иначе дефолт
         (`get_effective_text`).
      4) Даём UI (см. `app/telegram/bot.py`) отправить файл с дефолтом,
         принять отредактированную версию и сохранить через `set_override`.

Поддерживаемые шаги (имеют мастер-промт + один батч-текст в GPT):
  - `plan`   — Шаг 1 «План»
  - `script` — Шаг 2 «Закадровый текст»
  - `split`  — Шаг 3 «Разбивка на блоки»
  - `hero`   — Шаг 4 «Hero» (per-hero/per-variation, с плейсхолдерами
               `{{BRIEF}}` и `{{HERO_STYLE}}`, см. `_build_hero_default`)
  - `img_pr` — Шаг 6 «Промты картинок» (батч на все кадры разом)
  - `anim_pr` — Шаг 8 «Промты анимации»: одно окно ChatGPT, первое
               сообщение = мастер + закадровый текст; далее пачки картинок
               (см. `animation_prompt_gpt` и `make_animation_prompts.py`)
Шаги 6 «Картинки» и 8 «Видео» не шлют текст в GPT (генерация в outsee),
поэтому override для них тоже не нужен.

Для `hero` шаблон (default или override) содержит литеральные плейсхолдеры
`{{BRIEF}}` (описание конкретного героя) и `{{HERO_STYLE}}` (визуальный
стиль из `prompts/04_hero_style/<name>.md`). Подстановка происходит в
`generate_hero.py` для каждой пары (hero_idx, variation_idx) — так один
и тот же шаблон работает и для нескольких героев в проекте.
"""

from __future__ import annotations

import re
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project
from app.generation_options import OUTSEE_PROMPT_MAX_CHARS
from app.services.prompt_library import (
    STEP_HUMAN_NAMES,
    get_project_prompt,
)

# Шаги, для которых поддерживается edit-override «сопр. сообщения».
SUPPORTED_STEPS: tuple[str, ...] = (
    "plan", "script", "hero", "split", "img_pr", "anim_pr", "music",
    # Слоты «Доп работа с EXCEL» (шаг 5). Каждый слот хранит свой
    # override в `Project.gpt_text_overrides["enrich_<i>"]`. В отличие
    # от других шагов, тут «сопр. сообщение» = ТОЛЬКО сопровождающий
    # текст (без мастер-промта). Мастер-промт лежит отдельно в
    # `prompts/05<a..e>_enrich_<i>/<name>.md`, в `enrich_xlsx.py` они
    # склеиваются: master + "\n\n---\n\n" + accompanying.
    "enrich_1", "enrich_2", "enrich_3", "enrich_4", "enrich_5",
    "excel_gpt",
)

# Дефолтный «сопровождающий текст» для enrich-слотов. Единый источник
# правды — используется и в UI (через `_build_enrich_default`), и в
# самом воркере (см. `app/orchestrator/steps/enrich_xlsx.py`,
# `_get_accompanying_text` через `get_effective_text`).
ENRICH_DEFAULT_ACCOMPANYING_TEXT = (
    "Внеси изменения в данные проекта согласно инструкциям выше.\n"
    "ВАЖНО: ответ — только JSON apply-ops "
    '({"ops":[{"frame_uuid":"…","fields":{…}}]}), без xlsx и без TSV # Лист:.'
)

# Плейсхолдеры в шаблоне «сопр. сообщения» шага `hero`. Подставляются
# в `generate_hero.py` отдельно для каждой пары (hero_idx, variation_idx)
# — поэтому в самом шаблоне они хранятся литерально.
HERO_PLACEHOLDER_BRIEF = "{{BRIEF}}"
HERO_PLACEHOLDER_STYLE = "{{HERO_STYLE}}"

# Плейсхолдеры темы в мастер-промтах (шаг 1 и др.)
TOPIC_PLACEHOLDER_PATTERNS: tuple[tuple[str, bool], ...] = (
    (r"\[ВСТАВЬ ТЕМУ\]", True),
    (r"\(тема ролика\)", True),
    (r"\{\{TOPIC\}\}", False),
    (r"\{\{VAR:PROJECT_TOPIC\}\}", False),
)


def inject_topic_placeholders(text: str, topic: str) -> str:
    """Подставляет тему ролика в плейсхолдеры мастер-промта.

    ``[ВСТАВЬ ТЕМУ]`` и ``(тема ролика)`` → ``(тема)`` в круглых скобках.
    ``{{TOPIC}}`` / ``{{VAR:PROJECT_TOPIC}}`` → текст темы без скобок.
    """
    t = (topic or "").strip()
    if not t or not text:
        return text
    out = text
    bracketed = f"({t})"
    for pattern, wrap in TOPIC_PLACEHOLDER_PATTERNS:
        repl = bracketed if wrap else t
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    return out


# Строка «Тема ролика: …» в сопр. сообщении / override — часто устаревает
# после смены topic у дочернего (override скопирован с родителя).
_TOPIC_LINE_RE = re.compile(
    r"(Тема ролика:\s*)([«\"'(\[]?)([^»\"')\]\n]+)([»\"')\]]?)",
    re.IGNORECASE,
)


def refresh_topic_line_in_text(text: str, topic: str) -> str:
    """Переписать строку «Тема ролика: …» на актуальную тему проекта."""
    t = (topic or "").strip()
    if not t or not text:
        return text

    def _repl(match: re.Match[str]) -> str:
        prefix, open_q, _old, close_q = match.groups()
        if open_q in ("«", '"', "'") and close_q in ("»", '"', "'"):
            return f"{prefix}{open_q}{t}{close_q}"
        if open_q == "(" and close_q == ")":
            return f"{prefix}({t})"
        if open_q == "[" and close_q == "]":
            return f"{prefix}[{t}]"
        if open_q or close_q:
            return f"{prefix}({t})"
        return f"{prefix}{t}"

    return _TOPIC_LINE_RE.sub(_repl, text, count=1)

def is_supported(step_code: str) -> bool:
    return step_code in SUPPORTED_STEPS


# --------------------------------------------------------------------------- #
# Сборка дефолтного текста по шагам
# --------------------------------------------------------------------------- #


def _build_topic_context_block(project: Project) -> str:
    """Возвращает текстовый блок с расширенным контекстом ролика (карточка
    темы + постоянный продукт массового). Используется в `_build_plan_default`
    и `_build_script_default`, чтобы GPT увидел весь нюанс.

    Если у проекта нет карточки и продукта (одиночный проект) — возвращает
    пустую строку (текст не меняется).
    """
    meta = getattr(project, "meta", None) or {}
    card = meta.get("topic_card") or {}
    product = meta.get("permanent_product") or {}

    lines: list[str] = []

    # Карточка темы (только непустые поля).
    card_lines: list[str] = []
    if card.get("style"):
        card_lines.append(f"  • Стиль: {card['style']}")
    if card.get("hook_type"):
        card_lines.append(f"  • Тип хука: {card['hook_type']}")
    if card.get("emotion"):
        card_lines.append(f"  • Эмоциональный фон: {card['emotion']}")
    if card.get("fact"):
        card_lines.append(f"  • Научпоп ядро / факт: {card['fact']}")
    if card.get("logic"):
        card_lines.append(f"  • Логическое объяснение: {card['logic']}")
    if card.get("integration"):
        card_lines.append(f"  • Интеграция продукта: {card['integration']}")
    if card.get("shoot_note"):
        card_lines.append(f"  • Примечание по съёмке: {card['shoot_note']}")
    if card_lines:
        lines.append("📋 Карточка ролика:")
        lines.extend(card_lines)

    # Постоянный продукт массового.
    if product and product.get("name"):
        if lines:
            lines.append("")
        lines.append("📦 Постоянный продукт (должен фигурировать в каждом ролике):")
        lines.append(f"  • Название: {product['name']}")
        if product.get("description"):
            lines.append(f"  • Описание: {product['description']}")
        if product.get("reference_image_path"):
            lines.append(
                "  • Референс-изображение приложено отдельно "
                f"({product['reference_image_path']})."
            )
        lines.append(
            "  • ВАЖНО: на этапе интеграции (по «Карточке ролика») органично "
            "ввести этот продукт в сюжет, не противореча историческому/"
            "научному контексту. Не оборачивать в кавычки и не заменять "
            "плейсхолдером — использовать именно указанное название."
        )

    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


def _build_plan_default(
    project: Project,
    *,
    topic: str | None = None,
    prompt_file_name: str = "prompt_plan.md",
) -> str:
    """Шаг 1 «План» (xlsx-flow): к чату прикладываются prompt_plan.md и
    project.xlsx. Возвращает «сопр. сообщение» — короткий текст в чат,
    без дублирования содержимого мастер-промта (он идёт файлом).
    """
    actual_topic = topic if topic is not None else (project.topic or "")
    context_block = _build_topic_context_block(project)
    return (
        f"Тема ролика: ({actual_topic}).\n\n"
        f"{context_block}"
        f"Прикреплены 2 файла:\n"
        f"  1. {prompt_file_name} — инструкция, что именно делать.\n"
        f"  2. project.xlsx — рабочая таблица ролика.\n\n"
        "Сделай всё, что написано в первом файле (инструкция), опираясь на "
        "второй (project.xlsx). Заполни лист «Общий план» (не лист «план»), "
        "остальные листы не меняй, и пришли мне обратно полный .xlsx "
        "(без обрезок и компрессии). Кратким текстом ответь — что сделал — "
        "но главное верни файл."
    )


def _build_script_default(project: Project, *, prompt_file_name: str = "prompt.txt") -> str:
    """Шаг 2 — закадровый текст. В xlsx-flow к чату прикладываются
    `prompt.txt` (мастер-промт + тема) и `project.xlsx`. Возвращаем
    «сопр. сообщение» — основной текст письма в чат.
    """
    topic = (project.topic or "").strip()
    context_block = _build_topic_context_block(project)
    from app.services.voiceover_sanitize import VOICEOVER_OUTPUT_FORMAT

    return (
        f"Тема ролика: «{topic}».\n\n"
        f"{context_block}"
        f"Прикреплены 2 файла:\n"
        f"  1. {prompt_file_name} — инструкция, что именно делать.\n"
        f"  2. project.xlsx — рабочая таблица ролика (план, структура).\n\n"
        "Сделай всё, что написано в первом файле (инструкция), опираясь на "
        "второй (project.xlsx).\n\n"
        "Пришли результат txt файлом в чат.\n\n"
        f"{VOICEOVER_OUTPUT_FORMAT}"
    )


def _build_split_default(
    project: Project, *, prompt_file_name: str = "prompt.txt"
) -> str:
    """Шаг 3 — разбивка на блоки (xlsx-flow). К чату прикладываются
    `prompt.txt`, `project.xlsx`, `voiceover.txt`. Возвращаем chat_msg.
    """
    topic = (project.topic or "").strip()
    context_block = _build_topic_context_block(project)
    return (
        f"Тема ролика: «{topic}».\n\n"
        + (context_block + "\n\n" if context_block else "")
        + f"Прикреплены 3 файла:\n"
        f"  1. {prompt_file_name} — инструкция, что именно делать.\n"
        f"  2. project.xlsx — рабочая таблица ролика (план, структура).\n"
        f"  3. voiceover.txt — закадровый текст, который нужно разбить "
        f"на блоки.\n\n"
        "Сделай всё, что написано в первом файле (инструкция), опираясь "
        "на структуру из project.xlsx и применяя к voiceover.txt.\n\n"
        "Все результаты ЗАПИШИ В project.xlsx (в нужные листы и ячейки) и "
        "пришли мне обратно ОБНОВЛЁННЫЙ project.xlsx как .xlsx-файл "
        "(без обрезок и компрессии). Кратким текстом ответь — что сделал — "
        "но главное верни файл."
    )


def assert_hero_master_is_sheet(project: Project) -> None:
    """Fail-closed: в слоте hero должен быть turnaround sheet, не реестр."""
    from app.services.hero_prompt_contract import validate_hero_master_or_error
    from app.services.prompt_library import resolve_project_prompt_name

    overrides = getattr(project, "prompt_overrides", None) or {}
    meta = getattr(project, "meta", None) if isinstance(getattr(project, "meta", None), dict) else {}
    name = resolve_project_prompt_name(overrides, "hero", meta=meta) or ""
    master = get_project_prompt(project, "hero").strip()
    err = validate_hero_master_or_error(master, source_name=str(name))
    if err:
        raise RuntimeError(err)


def _build_hero_default(project: Project) -> str:
    """Шаг 4 — Hero. Один и тот же шаблон используется для всех героев
    проекта и всех их вариаций. Конкретные `BRIEF` (описание героя) и
    `HERO_STYLE` (визуальный стиль) подставляются в `generate_hero.py`
    в момент запуска для каждой пары (hero_idx, variation_idx).

    В шаблоне они оставлены литералами `{{BRIEF}}` / `{{HERO_STYLE}}`.
    Override (если задан) тоже должен содержать эти плейсхолдеры — иначе
    конкретное описание героя/стиля просто не попадёт в GPT.
    """
    assert_hero_master_is_sheet(project)
    hero_master = get_project_prompt(project, "hero").strip()
    return (
        "Сделай промт для генерации персонажа, который описан ниже. "
        "Ты должен интегрировать персонажа в промт и прислать готовый "
        "промт для генерации персонажа.\n\n"
        "КРИТИЧНО: вид существа (человек, кот и т.д.) бери ТОЛЬКО из "
        "блока «Описание персонажа» ниже. Не заменяй людей на "
        "антропоморфных котов (и наоборот), даже если в шаблоне выше "
        "есть другие правила «мира».\n\n"
        "Структура промта (turnaround sheet) — ниже шаблоном. "
        "Подставь в него характеристики персонажа из описания ниже, "
        "верни ТОЛЬКО готовый текст промта (на английском, без кавычек, "
        "без markdown-обрамления, без пояснений).\n\n"
        "ВАЖНО: ОБЯЗАТЕЛЬНО учитывай блок «Visual style» ниже — он "
        "описывает визуальный стиль (рендер, освещение, lens, цвет). "
        "Эти инструкции должны быть отражены в финальном промте — "
        "никакого «default» style; используем именно этот блок.\n\n"
        f"ЛИМИТ: финальный промт должен быть НЕ ДЛИННЕЕ "
        f"{OUTSEE_PROMPT_MAX_CHARS} символов (включая пробелы). "
        f"Если получается длиннее — сожми описание, убери дубликаты, "
        f"оставь только самое важное. Главное чтобы влезло в "
        f"{OUTSEE_PROMPT_MAX_CHARS}.\n\n"
        "Шаблон:\n\n"
        + hero_master
        + "\n\n---\n\nVisual style (применять обязательно):\n"
        + HERO_PLACEHOLDER_STYLE
        + "\n\n---\n\nОписание персонажа:\n"
        + HERO_PLACEHOLDER_BRIEF
    )


def render_hero_text(
    template: str, *, brief: str, hero_style: str
) -> str:
    """Подставляет в шаблон «сопр. сообщения» шага `hero` конкретные
    значения для одной пары (hero_idx, variation_idx).

    Если в `hero_style` пусто — подставляется placeholder-фраза, чтобы
    GPT не оставил пустую секцию (поведение совпадает с предыдущей
    версией `generate_hero.py`).
    """
    style = (hero_style or "").strip() or (
        "(не задан — используй кинематографический фото-реализм)"
    )
    out = template.replace(HERO_PLACEHOLDER_STYLE, style)
    out = out.replace(HERO_PLACEHOLDER_BRIEF, (brief or "").strip())
    # Если в пользовательском шаблоне не было плейсхолдера {{HERO_STYLE}},
    # гарантированно добавляем блок Visual style в конец, чтобы стиль не терялся.
    if HERO_PLACEHOLDER_STYLE not in template and style:
        out = out + f"\n\n---\n\nVisual style (применять обязательно):\n{style}"
    if HERO_PLACEHOLDER_BRIEF not in template and brief:
        out = out + f"\n\n---\n\nОписание персонажа:\n{brief.strip()}"
    return out


def build_anim_pr_initial_default(
    project: Project,
    frames: list | None = None,
    *,
    prompt_file_name: str = "prompt_anim_pr.md",
) -> str:
    """Первое сообщение: сопр. текст (мастер-промт — отдельным файлом, без картинок)."""
    _ = frames
    return (
        f"Прикреплён файл: {prompt_file_name} — мастер-промт для промтов анимации. "
        f"Следуй ему.\n\n"
        "Дальше пришлю изображения пачками (до 6 за сообщение). "
        "К каждой пачке — frame_uuid и закадровый по кадрам. "
        "На каждую пачку отвечай ТОЛЬКО JSON apply-ops на ВСЕ кадры пачки:\n"
        '{"ops":[{"frame_uuid":"…","fields":{"промт_видео":"…"}}]}'
    )


def _build_anim_pr_default(
    project: Project, *, prompt_file_name: str = "prompt_anim_pr.md", **_ctx
) -> str:  # noqa: ARG001
    """Дефолт «сопр. сообщения» без списка кадров (Studio / TG)."""
    return build_anim_pr_initial_default(project, [], prompt_file_name=prompt_file_name)


def _build_img_pr_default(
    project: Project,
    *,
    voiceover_line: str = "",
    n_frames: int = 0,
    prompt_file_name: str = "prompt_img_pr.md",
) -> str:
    """Шаг 6 «Промты картинок» (xlsx-flow).

    К чату прикладываются prompt_img_pr.md и project.xlsx.
    Возвращает «сопр. сообщение» — короткий текст в чат,
    без дублирования содержимого мастер-промта (он идёт файлом).
    """
    context_block = _build_topic_context_block(project)
    return (
        (context_block + "\n\n" if context_block else "")
        + f"Прикреплены файлы:\n"
        f"  1. {prompt_file_name} — инструкция (стиль + структура промта).\n"
        f"  2. db_frames.json — кадры ЭТОГО батча из Базы "
        f"(uuid + place/lighting/shot01_*/accent/scene_sense/scene_feature "
        f"+ Entity characters).\n"
        f"  3. voiceover.txt — полный закадр (НЕ копировать в промт).\n\n"
        f"Кадров в этом запросе: {n_frames or 'см. db_frames'}.\n"
        "Сделай всё по инструкции. Верни ТОЛЬКО JSON apply-ops "
        '{"ops":[{"frame_uuid":"…","fields":{"промт_картинки":"…"}}]} '
        "для каждого uuid из db_frames.json. Без xlsx, без TSV, без markdown."
    )


# --------------------------------------------------------------------------- #
# Публичные функции
# --------------------------------------------------------------------------- #

def _build_music_default(project: Project, **_ctx) -> str:  # noqa: ARG001
    """Сопроводительный текст для GPT → Suno (шаг «Музыка»)."""
    topic = (project.topic or "").strip() or "(тема не задана)"
    return (
        f"Тема ролика: {topic}\n\n"
        "На основе приложенного voiceover.txt составь один промт для генерации "
        "фоновой инструментальной музыки в Suno (без вокала).\n"
        "Верни ТОЛЬКО текст промта для Suno, без пояснений и кавычек."
    )


def _build_enrich_default(project: Project, **_ctx) -> str:  # noqa: ARG001
    """Дефолтный «сопровождающий текст» для enrich-слотов (1..5).

    Здесь, в отличие от других шагов, возвращаем ТОЛЬКО короткую
    инструкцию (без мастер-промта). Мастер-промт пользователь редактирует
    отдельно — через picker → «✏ Редактировать выбранный». А «сопр.
    сообщение» — это короткое сопровождение, которое склеивается с
    мастер-промтом в `enrich_xlsx.py` через `master + "\\n---\\n" + ...`.
    """
    return ENRICH_DEFAULT_ACCOMPANYING_TEXT


def build_default_text(project: Project, step_code: str, **ctx) -> str:
    """Собирает дефолтный текст «сопр. сообщения» для шага.

    `**ctx` — контекстные данные, специфичные для шага (для img_pr —
    `voiceover_line`, `n_frames`; для script — `prompt_file_name`).
    """
    if step_code == "plan":
        return _build_plan_default(project, **ctx)
    if step_code == "script":
        return _build_script_default(project, **ctx)
    if step_code == "split":
        return _build_split_default(project, **ctx)
    if step_code == "hero":
        return _build_hero_default(project, **ctx)
    if step_code == "img_pr":
        return _build_img_pr_default(project, **ctx)
    if step_code == "anim_pr":
        frames = ctx.get("frames") or []
        return build_anim_pr_initial_default(
            project, frames, prompt_file_name=ctx.get("prompt_file_name", "prompt_anim_pr.md")
        )
    if step_code.startswith("enrich_") or step_code == "excel_gpt":
        return _build_enrich_default(project, **ctx)
    if step_code == "music":
        return _build_music_default(project, **ctx)
    raise ValueError(f"build_default_text: шаг {step_code!r} не поддерживается")


def get_override(project: Project, step_code: str) -> str | None:
    """Возвращает пользовательский override-текст для шага или None."""
    overrides = getattr(project, "gpt_text_overrides", None) or {}
    val = overrides.get(step_code)
    if isinstance(val, str) and val.strip():
        return val
    return None


def has_override(project: Project, step_code: str) -> bool:
    return get_override(project, step_code) is not None


def get_display_text(project: Project, step_code: str, **ctx) -> str:
    """Текст для редактора в UI — без автоблока параметров plan/script/split."""
    override = get_override(project, step_code)
    if override is not None:
        return override
    return build_default_text(project, step_code, **ctx)


def get_effective_text(project: Project, step_code: str, **ctx) -> str:
    """Возвращает текст, который реально пойдёт в GPT.

    1. Если в `project.gpt_text_overrides[step_code]` есть непустой текст —
       вернёт его.
    2. Иначе соберёт дефолт из мастер-промта + контекста проекта.
    3. Для plan/script/split — дописывает блок параметров из meta.
    4. Строка «Тема ролика» всегда синхронизируется с актуальным `project.topic`
       (и `ctx['topic']`, если передан) — иначе override с родителя шлёт старое имя.
    """
    from app.services.node_step_params import append_step_params_to_gpt_text

    body = get_display_text(project, step_code, **ctx)
    actual_topic = ctx.get("topic")
    if actual_topic is None:
        actual_topic = project.topic or ""
    body = refresh_topic_line_in_text(body, str(actual_topic))
    body = inject_topic_placeholders(body, str(actual_topic))
    if step_code in ("plan", "script", "split"):
        body = append_step_params_to_gpt_text(project, step_code, body)
    if step_code == "script":
        from app.services.voiceover_sanitize import ensure_voiceover_format_instruction

        body = ensure_voiceover_format_instruction(body)
    return body


async def set_override(
    session: AsyncSession,
    project: Project,
    step_code: str,
    text: str,
) -> None:
    """Сохраняет пользовательский override-текст для шага.

    Пустая строка → удаляет override (см. `clear_override`).
    """
    if not text or not text.strip():
        await clear_override(session, project, step_code)
        return
    if not is_supported(step_code):
        raise ValueError(f"set_override: шаг {step_code!r} не поддерживается")
    overrides = dict(getattr(project, "gpt_text_overrides", None) or {})
    overrides[step_code] = text
    project.gpt_text_overrides = overrides
    await session.flush()


async def clear_override(
    session: AsyncSession,
    project: Project,
    step_code: str,
) -> None:
    """Удаляет пользовательский override для шага."""
    overrides = dict(getattr(project, "gpt_text_overrides", None) or {})
    if step_code in overrides:
        overrides.pop(step_code, None)
        project.gpt_text_overrides = overrides
        await session.flush()


def step_human_name(step_code: str) -> str:
    return STEP_HUMAN_NAMES.get(step_code, step_code)

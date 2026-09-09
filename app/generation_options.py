"""Каталог вариантов генерации для мастера настроек проекта.

8 вопросов (Studio / Telegram wizard):
  1. Генератор картинок
  2. Соотношение сторон
  3. Разрешение картинки (зависит от модели — см. IMAGE_RESOLUTIONS_BY_GENERATOR)
  4. Качество / «Детализация» (только GPT Image)
  5. Безлимит картинок (outsee Relax)
  6–8. Видео: генератор / разрешение / relax

`outsee_slug` сверстан с live UI outsee.io/image (июль 2026):
  gpt-image-2, gpt-image-1.5, nano-banana-2, seedream-4.5, seedream-5-lite, …
URL: `https://outsee.io/image?model=<slug>` (также есть /create?type=image&model=…).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OptionChoice:
    """Одна кнопка в мастере настроек.

    id          — строковый ID (будет лежать в БД)
    label       — текст на кнопке (видит юзер)
    outsee_slug — slug для URL / для клика в UI outsee
    short_desc  — одна строка пояснений (для GPT-контекста)
    """

    id: str
    label: str
    outsee_slug: str
    short_desc: str = ""


# ---- 1. Генераторы картинок (без Topaz Upscale — это не генератор) ---------

IMAGE_GENERATORS: list[OptionChoice] = [
    OptionChoice(
        "flux_2_pro", "Flux 2 Pro", "flux-2-pro",
        "Kie · Black Forest Labs Flux 2 Pro",
    ),
    OptionChoice(
        "seedream_5_pro", "ByteDance Seedream 5 Pro", "seedream-5-pro",
        "Kie · Seedream 5 Pro",
    ),
    OptionChoice(
        "gpt_image_2_vip", "+ GPT Image 2", "gpt-image-2-vip",
        "Outsee · GPT Image 2 (до 4K)",
    ),
    OptionChoice(
        "nano_banana_2", "+ Nano Banana 2", "nano-banana-2",
        "Outsee · Nano Banana 2",
    ),
    OptionChoice(
        "nano_banana_2_lite", "+ Nano Banana 2 Lite", "nano-banana-2-lite",
        "Outsee · Nano Banana 2 Lite",
    ),
    OptionChoice(
        "z_image", "Z-Image", "z-image",
        "Kie · Z-Image (быстрая генерация)",
    ),
    OptionChoice(
        "qwen3_image", "Alibaba Qwen Image 3", "qwen3-image",
        "Kie · Qwen Image 3",
    ),
]


# ---- 2. Соотношения сторон (outsee image UI) --------------------------------

ASPECT_RATIOS: list[OptionChoice] = [
    OptionChoice("1_1", "1:1", "1:1", "Квадрат (Instagram-пост)"),
    OptionChoice("16_9", "16:9", "16:9", "Широкий (YouTube, TV, landscape)"),
    OptionChoice("9_16", "9:16", "9:16", "Вертикаль (Reels, TikTok, Shorts)"),
    OptionChoice("4_3", "4:3", "4:3", "Классика (ТВ старое, iPad)"),
    OptionChoice("3_4", "3:4", "3:4", "Вертикальная классика (портрет)"),
    OptionChoice("2_3", "2:3", "2:3", "Вертикальный (портрет-фото)"),
    OptionChoice("3_2", "3:2", "3:2", "Горизонтальный (DSLR-фото)"),
    OptionChoice("21_9", "21:9", "21:9", "Ультра-широкий (cinematic)"),
    OptionChoice("5_4", "5:4", "5:4", "Чуть шире квадрата"),
    OptionChoice("4_5", "4:5", "4:5", "Чуть выше квадрата (портрет)"),
]


# ---- 3. Разрешение картинки ------------------------------------------------

IMAGE_RESOLUTIONS: list[OptionChoice] = [
    OptionChoice("1k", "1K", "1K", "1K — компактное разрешение"),
    OptionChoice("2k", "2K", "2K", "2K — стандартное разрешение"),
    OptionChoice("4k", "4K", "4K", "4K — максимальное качество"),
]

# Какие кнопки разрешения реально есть у модели на outsee.io/image (из UI JS).
IMAGE_RESOLUTIONS_BY_GENERATOR: dict[str, tuple[str, ...]] = {
    "nano_banana_2": ("1k", "2k", "4k"),
    "nano_banana_2_lite": ("1k", "2k"),
    "nano_banana_pro": ("1k", "2k", "4k"),
    "nano_banana_fast": ("1k", "2k"),
    "nano_banana": ("1k", "2k"),
    "seedream_4_5": ("2k", "4k"),
    "seedream_5_0_lite": ("2k",),
    "seedream_5_pro": ("1k", "2k"),
    "gpt_image_1_5": ("2k",),
    "gpt_image_2": ("1k",),
    "gpt_image_2_vip": ("1k", "2k", "4k"),
}


# ---- 3b. Качество / «Детализация» (GPT Image 1.5 / 2) -----------------------

IMAGE_QUALITIES: list[OptionChoice] = [
    OptionChoice("low", "Низкое", "Низкое", "Детализация: низкая — быстрее"),
    OptionChoice("medium", "Среднее", "Среднее", "Детализация: средняя — баланс"),
    OptionChoice("high", "Высокое", "Высокое", "Детализация: высокая — детальнее"),
]

# outsee button value=… рядом с русским label.
IMAGE_QUALITY_DOM_VALUE: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "Низкое": "low",
    "Среднее": "medium",
    "Высокое": "high",
}

GPT_IMAGE_GENERATOR_IDS = frozenset({"gpt_image_1_5", "gpt_image_2", "gpt_image_2_vip"})


def is_gpt_image_generator(generator_id: str | None) -> bool:
    return (generator_id or "") in GPT_IMAGE_GENERATOR_IDS


def allowed_image_resolution_ids(generator_id: str | None) -> tuple[str, ...]:
    gid = generator_id or "gpt_image_2"
    return IMAGE_RESOLUTIONS_BY_GENERATOR.get(gid, ("2k", "4k"))


def clamp_image_resolution_id(
    generator_id: str | None,
    resolution_id: str | None,
) -> str:
    """Если выбранное разрешение недоступно модели — ближайшее из её списка."""
    allowed = allowed_image_resolution_ids(generator_id)
    rid = (resolution_id or "2k").lower()
    if rid in allowed:
        return rid
    order = ("1k", "2k", "4k")
    try:
        want = order.index(rid) if rid in order else order.index("2k")
    except ValueError:
        want = 1
    return min(allowed, key=lambda a: abs(order.index(a) - want))


# ---- 4. Видео-генераторы (13 штук, без Topaz Video Upscale) ---------------

VIDEO_GENERATORS: list[OptionChoice] = [
    OptionChoice(
        "seedance_2_5", "Seedance 2.5 (ByteDance)", "seedance-2-5",
        "Kie · Флагман ByteDance Seedance 2.5",
    ),
    OptionChoice(
        "seedance_1_5_pro", "Seedance 1.5 Pro", "seedance-1-5-pro",
        "Kie · Seedance 1.5 Pro (баланс цены и качества)",
    ),
    OptionChoice(
        "kling_3_0", "Kling 3.0 Pro (1080p + звук)", "kling-3-0",
        "Kie · Kling 3.0 Pro (1080p + нативный звук)",
    ),
    OptionChoice(
        "kling_v3_turbo_t2v", "Kling 3.0 Turbo (текст)", "kling-v3-turbo-t2v",
        "Kie · Kling 3.0 Turbo Text-to-Video",
    ),
    OptionChoice(
        "kling_v3_turbo_i2v", "Kling 3.0 Turbo (оживление фото)", "kling-v3-turbo-i2v",
        "Kie · Kling 3.0 Turbo Image-to-Video",
    ),
    OptionChoice(
        "kling_3_0_omni_t2v", "Kling 3.0 Omni (со звуком)", "kling-3-0-omni-t2v",
        "Kie · Kling 3.0 Omni с аудиодорожкой",
    ),
    OptionChoice(
        "hailuo_2_3_i2v", "MiniMax Hailuo 2.3 (фото)", "hailuo-2-3-i2v",
        "Kie · MiniMax Hailuo 2.3 Image-to-Video",
    ),
    OptionChoice(
        "wan_2_7_t2v", "Alibaba WAN 2.7 (текст)", "wan-2-7-t2v",
        "Kie · Alibaba WAN 2.7 Text-to-Video",
    ),
    OptionChoice(
        "pixverse_v6_t2v", "PixVerse V6 (текст)", "pixverse-v6-t2v",
        "Kie · PixVerse V6 Text-to-Video",
    ),
    OptionChoice(
        "topaz_video_upscale", "Topaz Video AI (апскейл)", "topaz-video-upscale",
        "Kie · Topaz Video AI Апскейл",
    ),
    OptionChoice(
        "veo_3_1_lite", "+ Veo 3.1 Lite", "veo-3-1-lite",
        "Outsee · Google Veo 3.1 Lite",
    ),
    OptionChoice(
        "kling_2_6", "Kling 2.6", "kling-2-6",
        "Kie · Kling 2.6 (KIE_API_KEY)",
    ),
]


# ---- 5. Разрешение видео ---------------------------------------------------

VIDEO_RESOLUTIONS: list[OptionChoice] = [
    OptionChoice("720p", "720p", "720p", "720p — HD"),
    OptionChoice("1080p", "1080p", "1080p", "1080p — Full HD"),
]


# ---- Справочники для поиска по id ------------------------------------------

def _by_id(choices: list[OptionChoice]) -> dict[str, OptionChoice]:
    return {c.id: c for c in choices}


IMAGE_GENERATORS_BY_ID = _by_id(IMAGE_GENERATORS)
IMAGE_GENERATORS_BY_ID["gpt_image_2"] = IMAGE_GENERATORS_BY_ID["gpt_image_2_vip"]
ASPECT_RATIOS_BY_ID = _by_id(ASPECT_RATIOS)
IMAGE_RESOLUTIONS_BY_ID = _by_id(IMAGE_RESOLUTIONS)
IMAGE_QUALITIES_BY_ID = _by_id(IMAGE_QUALITIES)
VIDEO_GENERATORS_BY_ID = _by_id(VIDEO_GENERATORS)
VIDEO_RESOLUTIONS_BY_ID = _by_id(VIDEO_RESOLUTIONS)

# ---- Video retry ladder (Veo primary → Kling 2.6 fallback) -----------------
#
# Контракт на клип:
#   1) primary (обычно Veo): 3 попытки с заменой текста промта
#   2) primary: ещё 1 финальная попытка (без новой rewrite-серии)
#   3) один раз смена модели → Kling 2.6 (kie.ai), без отката назад
#   4) Kling: 3 попытки → ошибка и пропуск кадра
#
# См. app/services/video_error_policy.py, app/bots/kie_kling.py.

VIDEO_PRIMARY_REWRITE_ATTEMPTS = 3
VIDEO_PRIMARY_FINAL_ATTEMPTS = 1
VIDEO_PRIMARY_TOTAL_ATTEMPTS = (
    VIDEO_PRIMARY_REWRITE_ATTEMPTS + VIDEO_PRIMARY_FINAL_ATTEMPTS
)  # 4
VIDEO_FALLBACK_ATTEMPTS = 3

# Совместимость со старыми тестами/импортами: порог смены модели = primary total.
OUTSEE_VIDEO_FALLBACK_AFTER_FAILURES = VIDEO_PRIMARY_TOTAL_ATTEMPTS
OUTSEE_VIDEO_FALLBACK_GENERATOR_ID = "kling_2_6"
OUTSEE_VIDEO_FALLBACK_RESOLUTION_ID = "720p"
# Для CDP/Outsee UI (если fallback когда-то идёт туда): «Исходное» по кадру.
OUTSEE_VIDEO_FALLBACK_ASPECT_LABEL = "Исходное"
# kie.ai Kling 2.6 i2v не принимает resolution; aspect только для t2v.
KIE_KLING_FALLBACK_SLUG = "kling-2-6"
KIE_KLING_PROMPT_MAX_CHARS = 1000


def outsee_video_fallback_fields() -> dict[str, str]:
    """Параметры запасной модели Kling 2.6 (slug + дефолты для логов/CDP)."""
    vg = VIDEO_GENERATORS_BY_ID[OUTSEE_VIDEO_FALLBACK_GENERATOR_ID]
    vr = VIDEO_RESOLUTIONS_BY_ID[OUTSEE_VIDEO_FALLBACK_RESOLUTION_ID]
    return {
        "model_slug": vg.outsee_slug,
        "resolution": vr.outsee_slug,
        "aspect_ratio": OUTSEE_VIDEO_FALLBACK_ASPECT_LABEL,
    }


def outsee_video_fallback_kwargs(
    current: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Совместимость: те же поля, что `outsee_video_fallback_fields()`."""
    del current
    return outsee_video_fallback_fields()


# ---- Дефолты (используются если юзер ещё не прошёл мастер) -----------------

DEFAULTS = {
    "image_generator": "gpt_image_2_vip",
    "aspect_ratio": "16_9",
    "image_resolution": "2k",
    "image_quality": "medium",
    "video_generator": "veo_3_fast",
    "video_resolution": "1080p",
}


def resolve_image_quality_slug(
    generator_id: str | None,
    quality_id: str | None = None,
) -> str | None:
    """Метка кнопки качества на outsee (Низкое/Среднее/Высокое) или None."""
    gid = generator_id or DEFAULTS["image_generator"]
    if not is_gpt_image_generator(gid):
        return None
    qid = quality_id or DEFAULTS.get("image_quality", "medium")
    choice = IMAGE_QUALITIES_BY_ID.get(qid)
    return choice.outsee_slug if choice else None


# ---- Функция-рендер полной сводки настроек проекта ------------------------

def render_settings_summary(
    image_generator: str | None,
    aspect_ratio: str | None,
    image_resolution: str | None,
    video_generator: str | None,
    video_resolution: str | None,
    image_quality: str | None = None,
    image_relax: bool | None = None,
    video_relax: bool | None = None,
) -> str:
    """Человекочитаемая сводка настроек — для карточки проекта в TG."""
    ig = IMAGE_GENERATORS_BY_ID.get(image_generator or "")
    ar = ASPECT_RATIOS_BY_ID.get(aspect_ratio or "")
    ir = IMAGE_RESOLUTIONS_BY_ID.get(image_resolution or "")
    iq = IMAGE_QUALITIES_BY_ID.get(image_quality or "")
    vg = VIDEO_GENERATORS_BY_ID.get(video_generator or "")
    vr = VIDEO_RESOLUTIONS_BY_ID.get(video_resolution or "")
    img_relax_str = (
        "Безлимит" if image_relax else ("—" if image_relax is None else "без Безлимита")
    )
    vid_relax_str = (
        "Безлимит" if video_relax else ("—" if video_relax is None else "без Безлимита")
    )
    qual_part = f" · {iq.label}" if iq else ""
    return (
        f"img-gen: {ig.label if ig else '—'} · "
        f"{ar.label if ar else '—'} · "
        f"{ir.label if ir else '—'}{qual_part} · "
        f"{img_relax_str}\n"
        f"video-gen: {vg.label if vg else '—'} · "
        f"{vr.label if vr else '—'} · "
        f"{vid_relax_str}"
    )


def render_settings_for_gpt(
    image_generator: str | None,
    aspect_ratio: str | None,
    image_resolution: str | None,
    video_generator: str | None,
    video_resolution: str | None,
    image_quality: str | None = None,
) -> str:
    """Блок для вставки в начало master-промта ChatGPT.

    Помогает модели стилизовать image/video промты под возможности
    конкретного генератора.
    """
    ig = IMAGE_GENERATORS_BY_ID.get(image_generator or "")
    ar = ASPECT_RATIOS_BY_ID.get(aspect_ratio or "")
    ir = IMAGE_RESOLUTIONS_BY_ID.get(image_resolution or "")
    iq = IMAGE_QUALITIES_BY_ID.get(image_quality or "")
    vg = VIDEO_GENERATORS_BY_ID.get(video_generator or "")
    vr = VIDEO_RESOLUTIONS_BY_ID.get(video_resolution or "")
    lines = ["=== TECHNICAL SETTINGS (от пользователя) ==="]
    if ig:
        lines.append(
            f"Image generator: {ig.label} — {ig.short_desc}. "
            f"Подгоняй image-промты под её стиль и ограничения."
        )
    if ar:
        lines.append(f"Aspect ratio: {ar.label} — {ar.short_desc}.")
    if ir:
        lines.append(f"Image resolution: {ir.label} — {ir.short_desc}.")
    if iq:
        lines.append(f"Image quality: {iq.label} — {iq.short_desc}.")
    if vg:
        lines.append(
            f"Video generator: {vg.label} — {vg.short_desc}. "
            f"Подгоняй animation/video-промты под её возможности."
        )
    if vr:
        lines.append(f"Video resolution: {vr.label} — {vr.short_desc}.")
    lines.append("")  # пустая строка-отбивка
    return "\n".join(lines)


# ---- Уникальный ID перед промтом -------------------------------------------

# Outsee.io: лимит textarea (полный промт: `[ID: …]` + текст).
OUTSEE_PROMPT_MAX_CHARS = 4900
# Целевой лимит тела промта без ID-строки — запас под prepend_gen_id (~25 симв).
OUTSEE_PROMPT_TARGET_BODY_CHARS = 4877

def build_gen_id_prefix(
    project_id: int, frame_number: int | None, short_uuid: str
) -> str:
    """Формат: `[ID: P12-F3-a7f2b01c]`  (или `[ID: P12-HERO-a7f2b01c]`).

    Нужен чтобы однозначно отличать картинки/промты в истории outsee. При
    match'е ищем в DOM текст, содержащий этот префикс, и берём
    соответствующую картинку. Старые картинки (от прошлых попыток) не
    имеют этого конкретного префикса, поэтому не будут случайно выбраны.
    """
    kind = f"F{frame_number}" if frame_number is not None else "HERO"
    return f"[ID: P{project_id}-{kind}-{short_uuid}]"


_PROMPT_ID_LINE_RE = re.compile(r"^\s*\[ID:\s*[^\]]+\]\s*$", re.IGNORECASE)

# Заглушки GPT/xlsx — только короткие/чистые placeholder (не подстрока в длинном промте).
_PLACEHOLDER_PHRASES: tuple[str, ...] = (
    "нет исходных данных для заполнения",
)

# Shot_02: обязательная фраза без содержания сцены — не генерация.
_SHOT2_PREFIX_ONLY = (
    "на основе референса, запрещено делать идентичную иллюстрацию "
    "без смены положения камеры",
    "на основе референса, запрещено делать идентичную иллюстрацию "
    "без смены положения",
)


def _normalize_prompt_ws(text: str) -> str:
    return " ".join(text.lower().split())


def is_skippable_empty_prompt(prompt: str) -> bool:
    """True — промт пустой или заглушка; в outsee не отправляем."""
    body = strip_prompt_id_lines((prompt or "").strip())
    if not body:
        return True
    low = body.lower().strip()
    if low in ("нет исходных данных", "нет исходных данных для заполнения"):
        return True
    for phrase in _PLACEHOLDER_PHRASES:
        if phrase in low and len(body) < 200:
            return True
    norm = _normalize_prompt_ws(body)
    for prefix in _SHOT2_PREFIX_ONLY:
        pnorm = _normalize_prompt_ws(prefix)
        if norm == pnorm:
            return True
        if norm.startswith(pnorm):
            rest = norm[len(pnorm) :].strip(" ,.;:-")
            if not rest:
                return True
            if any(phrase in rest for phrase in _PLACEHOLDER_PHRASES) and len(rest) < 120:
                return True
            if rest.startswith("кадр ") and "prompt_" in rest and len(rest) < 80:
                return True
    if re.fullmatch(
        r"(кадр\s*\d+\s*/\s*prompt_\d+\s*:?\s*)+",
        low,
        flags=re.IGNORECASE,
    ):
        return True
    return False


def strip_prompt_id_lines(prompt: str) -> str:
    """Убирает строки `[ID: …]` из тела промта.

    ID всегда добавляет `prepend_gen_id` один раз. Без этого GPT-rewrite
    тащит `[ID: …]` из текста ошибки outsee → в textarea два ID с разными
    хвостами uuid.
    """
    if not prompt:
        return ""
    kept = [
        ln
        for ln in prompt.splitlines()
        if not _PROMPT_ID_LINE_RE.match(ln)
    ]
    return "\n".join(kept).strip()


def prepend_gen_id(prompt: str, gen_id_prefix: str) -> str:
    """Ставит gen_id_prefix на первую строку промта (перед оригинальным текстом)."""
    body = strip_prompt_id_lines(prompt or "")
    if not body:
        return gen_id_prefix
    return f"{gen_id_prefix}\n\n{body}"

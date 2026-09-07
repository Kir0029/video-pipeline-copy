"""Каталог тарифов и калькулятор стоимости вызовов API.

Поддерживает:
- Текстовые LLM (расчёт за 1M входных/выходных/кэшированных токенов)
- Видео-генераторы (за секунду или за генерацию)
- Генераторы изображений (за изображение)
- Озвучку / TTS (за 1k символов)
"""

from __future__ import annotations

from typing import Any

# Базовые тарифы по моделям (в USD)
DEFAULT_PRICING: dict[str, dict[str, Any]] = {
    # --- Google Gemini (актуальные тарифы) ---
    "gemini-3.7-flash": {
        "type": "text",
        "input_usd_per_m": 0.147622,
        "output_usd_per_m": 0.738108,
        "cache_read_usd_per_m": 0.014762,
        "provider": "Google / Vibecode",
    },
    "gemini-3.8-flash": {
        "type": "text",
        "input_usd_per_m": 0.75,
        "output_usd_per_m": 3.75,
        "cache_read_usd_per_m": 0.075,
        "provider": "Google",
    },
    "gemini-3.6-flash": {
        "type": "text",
        "input_usd_per_m": 0.295243,
        "output_usd_per_m": 1.476216,
        "cache_read_usd_per_m": 0.029524,
        "provider": "Google / Vibecode",
    },
    "gemini-3-flash-preview": {
        "type": "text",
        "input_usd_per_m": 0.098414,
        "output_usd_per_m": 0.590486,
        "cache_read_usd_per_m": 0.009841,
        "provider": "Google / Vibecode",
    },
    "gemini-3.1-pro-preview": {
        "type": "text",
        "input_usd_per_m": 0.393658,
        "output_usd_per_m": 2.361946,
        "cache_read_usd_per_m": 0.039366,
        "provider": "Google / Vibecode",
    },

    # --- OpenAI / Vibecode ---
    "gpt-5.6-sol": {
        "type": "text",
        "input_usd_per_m": 1.20,
        "output_usd_per_m": 4.80,
        "cache_read_usd_per_m": 0.30,
        "provider": "OpenAI / Vibecode",
    },
    "gpt-5.6-terra": {
        "type": "text",
        "input_usd_per_m": 1.50,
        "output_usd_per_m": 6.00,
        "cache_read_usd_per_m": 0.375,
        "provider": "OpenAI / Vibecode",
    },
    "gpt-5.6-luna": {
        "type": "text",
        "input_usd_per_m": 2.00,
        "output_usd_per_m": 8.00,
        "cache_read_usd_per_m": 0.50,
        "provider": "OpenAI / Vibecode",
    },
    "gpt-4o": {
        "type": "text",
        "input_usd_per_m": 2.50,
        "output_usd_per_m": 10.00,
        "cache_read_usd_per_m": 1.25,
        "provider": "OpenAI",
    },
    "gpt-4o-mini": {
        "type": "text",
        "input_usd_per_m": 0.15,
        "output_usd_per_m": 0.60,
        "cache_read_usd_per_m": 0.075,
        "provider": "OpenAI",
    },

    # --- DeepSeek ---
    "deepseek-v4-flash": {
        "type": "text",
        "input_usd_per_m": 0.291896,
        "output_usd_per_m": 0.875687,
        "cache_read_usd_per_m": 0.00973,
        "provider": "DeepSeek / Vibecode",
    },
    "deepseek-v4-pro": {
        "type": "text",
        "input_usd_per_m": 0.55,
        "output_usd_per_m": 2.19,
        "cache_read_usd_per_m": 0.014,
        "provider": "DeepSeek / Vibecode",
    },

    # --- Видеогенераторы ---
    "kling-3-0": {
        "type": "video",
        "usd_per_gen": 0.12,
        "usd_per_sec": 0.024,
        "provider": "Kling AI",
    },
    "kling-v3-turbo-t2v": {
        "type": "video",
        "usd_per_gen": 0.08,
        "provider": "Kling AI",
    },
    "kling-v3-turbo-i2v": {
        "type": "video",
        "usd_per_gen": 0.08,
        "provider": "Kling AI",
    },
    "seedance-2-5": {
        "type": "video",
        "usd_per_gen": 0.06,
        "provider": "ByteDance",
    },
    "seedance-1-5-pro": {
        "type": "video",
        "usd_per_gen": 0.07,
        "provider": "ByteDance",
    },
    "veo-3-1-lite": {
        "type": "video",
        "usd_per_gen": 0.15,
        "provider": "Google DeepMind / Outsee",
    },
    "wan-2-7-t2v": {
        "type": "video",
        "usd_per_gen": 0.05,
        "provider": "Alibaba",
    },
    "hailuo-2-3-i2v": {
        "type": "video",
        "usd_per_gen": 0.09,
        "provider": "MiniMax",
    },

    # --- Генерация изображений ---
    "flux-2-pro": {
        "type": "image",
        "usd_per_image": 0.05,
        "provider": "Black Forest Labs / Kie",
    },
    "seedream-5-pro": {
        "type": "image",
        "usd_per_image": 0.045,
        "provider": "ByteDance / Kie",
    },
    "z-image": {
        "type": "image",
        "usd_per_image": 0.02,
        "provider": "Z-Image / Kie",
    },
    "qwen3-image": {
        "type": "image",
        "usd_per_image": 0.035,
        "provider": "Alibaba / Kie",
    },
    "gpt-image-2-vip": {
        "type": "image",
        "usd_per_image": 0.04,
        "provider": "OpenAI / Kie",
    },

    # --- Аудио / Озвучка ---
    "elevenlabs": {
        "type": "audio",
        "usd_per_1k_chars": 0.30,
        "provider": "ElevenLabs",
    },
}

# Синонимы и алиасы названий моделей
MODEL_ALIASES: dict[str, str] = {
    "gemini-3.7-flash-vibecode": "gemini-3.7-flash",
    "gemini-3.6-flash-vibecode": "gemini-3.6-flash",
    "gemini-3-flash": "gemini-3-flash-preview",
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
    "gpt-5.6-sol-vibecode": "gpt-5.6-sol",
    "gpt-5.6-terra-vibecode": "gpt-5.6-terra",
    "gpt-5.6-luna-vibecode": "gpt-5.6-luna",
    "deepseek-v4-flash-vibecode": "deepseek-v4-flash",
    "deepseek-v4-pro-vibecode": "deepseek-v4-pro",
    # Media models
    "flux-2-pro": "flux-2-pro",
    "flux-2/pro-text-to-image": "flux-2-pro",
    "flux-2/pro-image-to-image": "flux-2-pro",
    "seedream-5-pro": "seedream-5-pro",
    "bytedance-seedream-5-pro": "seedream-5-pro",
    "alibaba-qwen-image-3": "qwen3-image",
    "gpt-image-2": "gpt-image-2-vip",
}


def normalize_model_name(model: str) -> str:
    """Привести имя модели к каноническому виду."""
    m = (model or "").strip().lower().replace("_", "-").replace(" ", "-")
    return MODEL_ALIASES.get(m, m)


def calculate_cost(
    model: str,
    *,
    call_type: str = "text",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    media_count: int = 0,
    duration_sec: float = 0.0,
    chars: int = 0,
    custom_pricing: dict[str, dict[str, Any]] | None = None,
) -> float:
    """Рассчитать точную стоимость запроса в USD.
    
    Возвращает float с округлением до 6 знаков (например, 0.001452).
    """
    canon = normalize_model_name(model)
    catalog = custom_pricing or DEFAULT_PRICING
    rule = catalog.get(canon)

    # Если модель не найдена, пробуем префиксный поиск (например "gpt-4o-mini-2024-07-18" -> "gpt-4o-mini")
    if not rule:
        for k, v in catalog.items():
            if canon.startswith(k) or k.startswith(canon):
                rule = v
                break

    if not rule:
        # Резервные средние тарифы по типу
        if call_type == "image":
            return round(max(media_count, 1) * 0.04, 6)
        if call_type == "video":
            return round(max(duration_sec, 5.0) * 0.02, 6)
        if call_type == "audio":
            return round((chars / 1000.0) * 0.30, 6)
        # Для текста дефолт $0.5 / 1M in, $2.0 / 1M out
        cost = (prompt_tokens / 1_000_000.0) * 0.50 + (completion_tokens / 1_000_000.0) * 2.00
        return round(cost, 6)

    mtype = rule.get("type", call_type)

    if mtype == "text":
        in_rate = rule.get("input_usd_per_m", 0.50)
        out_rate = rule.get("output_usd_per_m", 2.00)
        cache_rate = rule.get("cache_read_usd_per_m", in_rate * 0.5)

        cost = (
            (prompt_tokens / 1_000_000.0) * in_rate
            + (completion_tokens / 1_000_000.0) * out_rate
            + (cached_tokens / 1_000_000.0) * cache_rate
        )
        return round(cost, 6)

    if mtype == "video":
        if "usd_per_sec" in rule and duration_sec > 0:
            return round(duration_sec * rule["usd_per_sec"], 6)
        return round(rule.get("usd_per_gen", 0.08) * max(media_count, 1), 6)

    if mtype == "image":
        return round(rule.get("usd_per_image", 0.04) * max(media_count, 1), 6)

    if mtype == "audio":
        rate = rule.get("usd_per_1k_chars", 0.30)
        return round((chars / 1000.0) * rate, 6)

    return 0.0

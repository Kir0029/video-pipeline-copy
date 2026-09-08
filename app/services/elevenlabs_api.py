"""Прямой HTTP REST API клиент ElevenLabs для синтеза речи (TTS).

Заменяет устаревший Playwright/Chrome-кликер. Вызывает официальный
эндпоинт POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
с авторизацией через заголовок xi-api-key и сохраняет MP3 напрямую.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from app.services.elevenlabs_voices import DEFAULT_ELEVENLABS_VOICE_ID
from app.settings import settings

ELEVENLABS_API_BASE_URL = "https://api.elevenlabs.io/v1"


class ElevenLabsApiError(RuntimeError):
    """Ошибка вызова ElevenLabs API."""


def elevenlabs_api_configured() -> bool:
    """Проверяет наличие API-ключа ElevenLabs в настройках или переменных окружения."""
    key = getattr(settings, "elevenlabs_api_key", None) or os.environ.get("ELEVENLABS_API_KEY", "")
    return bool(key and str(key).strip())


def _resolve_api_key() -> str:
    key = getattr(settings, "elevenlabs_api_key", None) or os.environ.get("ELEVENLABS_API_KEY", "")
    k = str(key or "").strip()
    if not k:
        raise ElevenLabsApiError(
            "Не задан ELEVENLABS_API_KEY в .env — укажите ключ для генерации озвучки."
        )
    return k


def _resolve_proxy_url() -> str | None:
    proxy = getattr(settings, "elevenlabs_proxy_url", None) or os.environ.get("ELEVENLABS_PROXY_URL", "")
    p = str(proxy or "").strip()
    return p or None


import re

_YEAR_CASES = {
    "2050": "две тысячи пятидесятый",
    "2030": "две тысячи тридцатый",
    "2025": "две тысячи двадцать пятый",
    "2024": "две тысячи двадцать четвёртый",
}


def normalize_text_for_tts(text: str) -> str:
    """Подготавливает текст для максимально естественной озвучки диктором TTS.

    Преобразует типичные цифровые конструкции, годы и проценты в слова.
    """
    if not text:
        return ""
    t = text
    # Обработка годов в падежах с сохранением регистра
    t = re.sub(
        r"\b([Вв])\s+2050(\s*году|\s*-?м\s*году|\s*-?м)\b",
        lambda m: f"{m.group(1)} две тысячи пятидесятом году",
        t,
    )
    t = re.sub(
        r"\b([Вв])\s+2050\b",
        lambda m: f"{m.group(1)} две тысячи пятидесятом",
        t,
    )
    t = re.sub(
        r"\b([Кк])\s+2050(\s*году|\s*-?му)?\b",
        lambda m: f"{m.group(1)} две тысячи пятидесятому году",
        t,
    )
    t = re.sub(
        r"\b([Дд])о\s+2050(\s*года)?\b",
        lambda m: f"{m.group(1)}о две тысячи пятидесятого года",
        t,
    )
    t = re.sub(
        r"\b2050(\s*год[а-я]*|\s*-?й)?\b",
        "две тысячи пятидесятый год",
        t,
        flags=re.IGNORECASE,
    )

    t = re.sub(
        r"\b([Вв])\s+2030(\s*году|\s*-?м\s*году|\s*-?м)\b",
        lambda m: f"{m.group(1)} две тысячи тридцатом году",
        t,
    )
    t = re.sub(
        r"\b2030(\s*год[а-я]*|\s*-?й)?\b",
        "две тысячи тридцатый год",
        t,
        flags=re.IGNORECASE,
    )

    # Проценты
    t = re.sub(r"\b100\s*%", "сто процентов", t)
    t = re.sub(r"\b50\s*%", "пятьдесят процентов", t)
    t = re.sub(r"(\d+)\s*%", r"\1 процентов", t)

    # Лишние пробелы
    t = re.sub(r"\s+", " ", t).strip()
    return t


async def synthesize_speech(
    text: str,
    out_path: Path,
    *,
    voice_id: str | None = None,
    model_id: str | None = None,
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    timeout: float = 240.0,
) -> Path:
    """Синтезирует аудиофайл через официальный ElevenLabs HTTP REST API.

    Args:
        text: Закадровый текст для озвучки.
        out_path: Целевой путь для сохранения MP3-файла.
        voice_id: ID голоса (по умолчанию Адам / DEFAULT_ELEVENLABS_VOICE_ID).
        model_id: ID модели (по умолчанию eleven_multilingual_v2).
        stability: Стабильность тембра (0.0 .. 1.0).
        similarity_boost: Схожесть с оригинальным голосом (0.0 .. 1.0).
        timeout: Таймаут HTTP-запроса в секундах.

    Returns:
        Path к сохранённому файлу out_path.
    """
    clean_text = normalize_text_for_tts(text)
    if not clean_text:
        raise ElevenLabsApiError("Текст для озвучки пуст.")

    api_key = _resolve_api_key()
    proxy_url = _resolve_proxy_url()
    effective_voice_id = str(voice_id or DEFAULT_ELEVENLABS_VOICE_ID).strip()
    effective_model = str(
        model_id
        or getattr(settings, "elevenlabs_model_id", "eleven_multilingual_v2")
        or "eleven_multilingual_v2"
    ).strip()

    from app.services.api_tracker_hook import check_api_allowed
    check_api_allowed(provider="ElevenLabs", model=effective_model)

    url = f"{ELEVENLABS_API_BASE_URL}/text-to-speech/{effective_voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload: dict[str, Any] = {
        "text": clean_text,
        "model_id": effective_model,
        "voice_settings": {
            "stability": max(0.0, min(1.0, float(stability))),
            "similarity_boost": max(0.0, min(1.0, float(similarity_boost))),
        },
    }

    logger.info(
        "11Labs API: запрос TTS voice_id={} model={} text_len={} симв. (proxy={})",
        effective_voice_id,
        effective_model,
        len(clean_text),
        "on" if proxy_url else "off",
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)

    async def _do_post(active_proxy: str | None) -> httpx.Response:
        client_kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
        }
        if active_proxy:
            client_kwargs["proxy"] = active_proxy
        async with httpx.AsyncClient(**client_kwargs) as client:
            return await client.post(url, headers=headers, json=payload)

    import asyncio

    resp: httpx.Response | None = None
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            resp = await _do_post(proxy_url)
        except (httpx.TimeoutException, httpx.RequestError, httpx.RemoteProtocolError) as exc:
            if proxy_url:
                logger.warning(
                    "11Labs API: ошибка через прокси ({}), выполняем прямой запрос без прокси (попытка {}/{})...",
                    exc,
                    attempt,
                    max_retries,
                )
                try:
                    resp = await _do_post(None)
                except Exception as direct_exc:  # noqa: BLE001
                    if attempt < max_retries:
                        await asyncio.sleep(2.0 * attempt)
                        continue
                    raise ElevenLabsApiError(
                        f"11Labs API: ошибка сетевого соединения: {direct_exc}"
                    ) from direct_exc
            else:
                if attempt < max_retries:
                    await asyncio.sleep(2.0 * attempt)
                    continue
                raise ElevenLabsApiError(
                    f"11Labs API: ошибка сетевого соединения: {exc}"
                ) from exc

        if resp is not None and resp.status_code in {429, 502, 503, 504}:
            if attempt < max_retries:
                wait_sec = 2.5 * attempt
                logger.warning(
                    "11Labs API: HTTP {} (Rate limit / upstream) — повтор через {:.1f}с ({}/{})",
                    resp.status_code,
                    wait_sec,
                    attempt,
                    max_retries,
                )
                await asyncio.sleep(wait_sec)
                continue

        break

    if resp is None:
        raise ElevenLabsApiError("11Labs API: не получен ответ от сервера.")

    if resp.status_code != 200:
        err_msg = f"HTTP {resp.status_code}"
        try:
            err_json = resp.json()
            detail = err_json.get("detail")
            if isinstance(detail, dict):
                err_msg = f"HTTP {resp.status_code}: {detail.get('message') or detail.get('code') or err_json}"
            elif isinstance(detail, str):
                err_msg = f"HTTP {resp.status_code}: {detail}"
            else:
                err_msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
        except Exception:  # noqa: BLE001
            err_msg = f"HTTP {resp.status_code}: {resp.text[:300]}"

        try:
            from app.services.api_tracker_hook import record_api_call
            record_api_call(
                provider="ElevenLabs",
                model=effective_model,
                call_type="audio",
                prompt_tokens=len(clean_text),
                duration_sec=0.0,
                cost_usd=0.0,
                status_code=resp.status_code,
                error_message=err_msg[:300],
                project_source="elevenlabs_tts",
                metadata={"voice_id": effective_voice_id},
            )
        except Exception:
            pass

        if resp.status_code == 401:
            raise ElevenLabsApiError(
                f"11Labs API: неверный API-ключ (401 Unauthorized) — {err_msg}"
            )
        if resp.status_code == 402:
            raise ElevenLabsApiError(
                f"11Labs API: недостаточно кредитов или выбранный голос требует платный тариф (402 Payment Required) — {err_msg}"
            )
        if resp.status_code == 429:
            raise ElevenLabsApiError(
                f"11Labs API: превышен лимит запросов Rate Limit (429) после {max_retries} попыток — {err_msg}"
            )

        raise ElevenLabsApiError(f"11Labs API ошибка генерации: {err_msg}")

    audio_bytes = resp.content
    if len(audio_bytes) < 1000:
        raise ElevenLabsApiError(
            f"11Labs API вернул слишком короткий файл ({len(audio_bytes)} байт) — возможно повреждение данных."
        )

    out_path.write_bytes(audio_bytes)
    logger.info(
        "11Labs API: озвучка успешно сгенерирована → {} ({} байт)",
        out_path.name,
        len(audio_bytes),
    )
    try:
        from app.services.api_tracker_hook import record_api_call
        char_count = len(clean_text)
        cost_est = round(char_count * 0.00015, 6)
        record_api_call(
            provider="ElevenLabs",
            model=effective_model,
            call_type="audio",
            prompt_tokens=char_count,
            duration_sec=0.0,
            cost_usd=cost_est,
            media_count=1,
            status_code=200,
            project_source="elevenlabs_tts",
            metadata={"voice_id": effective_voice_id, "chars": char_count},
        )
    except Exception:
        pass
    return out_path

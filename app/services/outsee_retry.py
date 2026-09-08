"""Утилита-обёртка вокруг `OutseeBot.generate_image` и `.generate_video`,
которая:

  1) Повторяет попытку генерации до `MAX_ATTEMPTS_PER_PROMPT` раз
     (по умолчанию 3) на любой `OutseeImageError` / `OutseeVideoError`,
     включая модерационную ошибку «Контент отклонён»
     (`OutseeContentRejectedError`). Между попытками — пауза 2 сек.

  2) Если все 3 попытки провалились, опционально просит ChatGPT
     переписать промт без триггеров модерации, отправляя в новый чат
     ровно тот meta-промт, который определил пользователь:

       «пришли только готовый текст без твоих рассуждений, на промт
        ниже генератор ругается, исправь ошибки и триггеры, которые
        считаешь нужным и пришли только отредактированный текст
        \n\n<сам_промт>»

     После этого делает ещё одну серию из 3 попыток уже с переписанным
     промтом. Если и она провалилась — пробрасывает последнюю ошибку
     из второй серии (caller сам решит, что делать).

  Видео (`generate_video_with_retries`) — лестница на клип:
    1) primary (Veo / модель проекта): 3 попытки с заменой текста промта
    2) primary: ещё 1 финальная попытка
    3) один раз смена модели → Kling 2.6 (kie.ai), без отката
    4) Kling: 3 попытки → VideoLadderExhaustedError (пропуск кадра)

  3) Если `gpt=None` или GPT-rewrite сам упал — идём с локальной санацией.

Использование:
    result = await generate_image_with_retries(
        outsee, gpt,
        prompt=prompt_text,
        out_path=out_path,
        max_attempts_per_prompt=3,
        gpt_rewrite=True,
        ...kwargs,  # все аргументы outsee.generate_image
    )

Caller'ы:
  - app/orchestrator/steps/generate_hero.py
  - app/orchestrator/steps/generate_images.py
  - app/orchestrator/steps/generate_videos.py
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

# Должен быть ≥ timeout в gpt.ask_fresh, иначе сжатие обрывается раньше ответа.
_GPT_COMPRESS_OUTER_TIMEOUT_S = 620.0
_GPT_REWRITE_OUTER_TIMEOUT_S = 620.0
# Модерация: очередь картинок последовательная — длинный GPT = «всё встало».
# Один rewrite с коротким таймаутом; overrun режем локально, без GPT-сжатия.
_GPT_MODERATION_REWRITE_OUTER_TIMEOUT_S = 180.0
_GPT_MODERATION_REWRITE_ASK_TIMEOUT_S = 150.0
# Хвост uniquify `[… r9a9]` — резерв при расчёте max_body.
_UNIQUIFY_SUFFIX_RESERVE = 8

from typing import Any

from loguru import logger

from app.bots.outsee import (
    GenerationResult,
    OutseeBot,
    OutseeContentRejectedError,
    OutseeDownloadError,
    OutseeImageError,
    OutseePromptTooLongError,
    outsee_error_is_moderation,
    outsee_error_kind,
    outsee_error_kind_label,
)
from app.generation_options import (
    KIE_KLING_FALLBACK_SLUG,
    KIE_KLING_PROMPT_MAX_CHARS,
    OUTSEE_PROMPT_MAX_CHARS,
    OUTSEE_VIDEO_FALLBACK_AFTER_FAILURES,
    VIDEO_FALLBACK_ATTEMPTS,
    VIDEO_PRIMARY_REWRITE_ATTEMPTS,
    VIDEO_PRIMARY_TOTAL_ATTEMPTS,
    outsee_video_fallback_fields,
    prepend_gen_id,
    strip_prompt_id_lines,
)
from app.services.step_cancel import abort_if_cancelled, sleep_cancellable
from app.services.step_cancel import StepCancelledError
from app.services.video_error_policy import (
    VideoErrorAction,
    VideoLadderExhaustedError,
    classify_video_error,
    ladder_summary,
    should_rewrite_after_primary_burn,
)
from app.services.video_prompt_sanitize import (
    ensure_silent_video_prompt,
    sanitize_video_prompt_after_errors,
)


def _apply_local_prompt_sanitize(
    prompt: str,
    *,
    where: str,
    force_simplify: bool = True,
) -> str:
    """Локальная замена триггеров (+ упрощение). Без GPT."""
    cleaned, stats = sanitize_video_prompt_after_errors(
        prompt, simplify=force_simplify
    )
    if cleaned.strip() == (prompt or "").strip():
        return prompt
    logger.info(
        "outsee_retry: локальная санация промта [{}]: "
        "triggers={} simplified={} {}→{} симв",
        where,
        stats["trigger_replacements"],
        stats["simplified"],
        stats["chars_before"],
        stats["chars_after"],
    )
    return cleaned


def _gpt_moderation_rewrite_meta(body_limit: int) -> str:
    """Meta для GPT-rewrite при модерации — лимит = тело без [ID:], не full."""
    return (
        "измени промт ниже, но сохрани смысл картины и деталей, "
        f"промт не должен быть больше {body_limit} символов "
        "(это лимит тела без строки [ID: …]), "
        "замени опасные, триггерные слова на синонимы более нейтральные "
        "и пришли только текст промта в ответе."
    )


# STYLE / Final style lock / Negative — не отдаём в GPT-сжатие и не режем с хвоста.
_STYLE_LOCK_LINE_RE = re.compile(
    r"(?im)^(?:\*\*)?(?:STYLE|Final style lock|Negative)\b"
)


def _split_style_lock(text: str) -> tuple[str, str]:
    """Сцена и блок стиля. Стиль не сжимаем."""
    raw = text or ""
    match = _STYLE_LOCK_LINE_RE.search(raw)
    start = match.start() if match else -1
    if start < 0:
        start = raw.find("STYLE:")
    if start < 0:
        return raw, ""
    return raw[:start].rstrip(), raw[start:].strip()


def _cut_at_space(text: str, body_limit: int) -> str:
    if len(text) <= body_limit:
        return text
    cut = text[:body_limit]
    sp = cut.rfind(" ")
    if sp > int(body_limit * 0.85):
        cut = cut[:sp]
    return cut


def _join_scene_style(scene: str, style: str) -> str:
    scene = (scene or "").rstrip()
    style = (style or "").strip()
    if scene and style:
        return f"{scene}\n\n{style}"
    return scene or style


def _hard_truncate_prompt(text: str, body_limit: int) -> str:
    """Обрезка тела: режем сцену, STYLE/Negative оставляем."""
    if len(text) <= body_limit:
        return text
    scene, style = _split_style_lock(text)
    if not style:
        return _cut_at_space(text, body_limit)
    sep_len = 2 if scene else 0
    scene_limit = body_limit - len(style) - sep_len
    if scene_limit < 40:
        if len(style) <= body_limit:
            return style
        return _cut_at_space(style, body_limit)
    return _join_scene_style(_cut_at_space(scene, scene_limit), style)

# Fallback для rewrite не из-за модерации (редко — второй раунд после других сбоев).
_GPT_REWRITE_META = (
    "пришли только готовый текст без рассуждений: исправь промт ниже "
    "и пришли только отредактированный текст."
)

# Минимальная длина «осмысленного» rewrite — отсекает «ok», «готово» и
# прочие пустышки, которые ChatGPT иногда возвращает при перегрузке.
_MIN_REWRITE_LEN = 30


def _outsee_full_prompt(body: str, prefix: str | None) -> str:
    if prefix:
        return prepend_gen_id(body, prefix)
    return body


def _prefix_reserve(prefix: str | None) -> int:
    """Запас под `[ID: …]` + `\n\n` + хвост uniquify ` r9a9]`."""
    if not prefix:
        return _UNIQUIFY_SUFFIX_RESERVE
    return len(prefix) + 2 + _UNIQUIFY_SUFFIX_RESERVE


def _max_body_for_prefix(prefix: str | None, *, cap: int | None = None) -> int:
    limit = cap if cap is not None else OUTSEE_PROMPT_MAX_CHARS
    return max(400, limit - _prefix_reserve(prefix))


_PROMPT_ERROR_MARKERS = (
    "промт обрезан",
    "лимит outsee",
    "не попал в поле",
    "id промта не найден",
    "не удалось сжать",
    "gpt недоступен для сжатия",
)

# Outsee API: аккаунт держит ≤N queued/processing; локальный семафор уже
# отпущен после «connection failed», а слоты на стороне API ещё заняты.
_CONCURRENCY_MARKERS = (
    "лимит одновремен",
    "одновременных генерац",
    "дождитесь завершения текущих",
    "too many concurrent",
    "concurrent generation",
    "concurrency limit",
)
_CONCURRENCY_MAX_WAITS = 24
_CONCURRENCY_BACKOFF_S = (45.0, 75.0, 120.0, 180.0)


def _is_concurrency_limit_error(err: BaseException) -> bool:
    """Лимит одновременных генераций Outsee — ждать слот, не GPT-rewrite."""
    reason = ""
    if isinstance(err, OutseeImageError):
        reason = (err.reason or "").lower()
        code = str((err.context or {}).get("code") or "").lower()
        if code in {"rate_limit", "too_many_requests", "concurrency_limit"}:
            return True
    else:
        reason = str(err).lower()
    return any(m in reason for m in _CONCURRENCY_MARKERS)


def _is_start_frame_content_policy_error(err: BaseException) -> bool:
    """CONTENT_POLICY на стартовом кадре (celebrity / лицо) — не лечится rewrite."""
    reason = str(getattr(err, "reason", None) or err).lower()
    body = ""
    if isinstance(err, OutseeImageError):
        ctx = err.context or {}
        body = str(ctx.get("body") or ctx.get("status") or "").lower()
        code = str(ctx.get("code") or "").lower()
        if code == "content_policy" and (
            "изображен" in reason
            or "личност" in reason
            or "известн" in reason
            or "image" in reason
        ):
            return True
    blob = f"{reason}\n{body}"
    if "content_policy" not in blob and "контент" not in blob:
        return False
    return any(
        m in blob
        for m in (
            "загруженное изображение",
            "известную личность",
            "известной личности",
            "uploaded image",
            "celebrity",
            "known person",
            "на нём известн",
        )
    )


# Сколько раз пробуем «размыть» start_frame до обычного fail (кадр НЕ снимаем).
_START_FRAME_SOFTEN_MAX = 2


def _soften_start_frame_for_policy(src: Path, *, strength: int) -> Path | None:
    """JPEG + blur/downscale — чаще проходит celebrity-модерацию Outsee.

    Возвращает новый файл рядом с src (не трогает оригинал). None = не смогли.
    strength 1 = мягко, 2 = сильнее.
    """
    try:
        from io import BytesIO

        from PIL import Image, ImageFilter
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(src, Path) or not src.is_file():
        return None
    s = max(1, min(int(strength), 3))
    # Раньше s1 давал ~80KB JPEG (q62+blur) → Veo выдавал «мыло» как 360p.
    # Держим лицо/детали: лёгкий blur, высокий JPEG, почти полный 1080/2K side.
    max_side = {1: 1920, 2: 1600, 3: 1280}[s]
    quality = {1: 85, 2: 76, 3: 68}[s]
    blur = {1: 0.55, 2: 1.15, 3: 1.9}[s]
    try:
        with Image.open(src) as im:
            img = im.convert("RGB")
            w, h = img.size
            scale = min(1.0, float(max_side) / float(max(w, h, 1)))
            if scale < 0.999:
                img = img.resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))),
                    Image.Resampling.LANCZOS,
                )
            if blur > 0:
                img = img.filter(ImageFilter.GaussianBlur(radius=blur))
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            raw = buf.getvalue()
        if len(raw) < 64:
            return None
        out = src.with_name(f"{src.stem}.policy_s{s}.jpg")
        out.write_bytes(raw)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "outsee_retry: soften start_frame failed ({}): {}",
            src.name,
            exc,
        )
        return None


def _is_audio_content_policy_error(err: BaseException) -> bool:
    """Veo всё равно клеит звук — CONTENT_POLICY по аудиодорожке."""
    reason = str(getattr(err, "reason", None) or err).lower()
    body = ""
    code = ""
    if isinstance(err, OutseeImageError):
        ctx = err.context or {}
        body = str(ctx.get("body") or ctx.get("status") or "").lower()
        code = str(ctx.get("code") or "").lower()
    blob = f"{reason}\n{body}\n{code}"
    if "аудиодорож" in blob or "audio track" in blob or "audio moderation" in blob:
        return True
    if "content_policy" in blob and "аудио" in blob:
        return True
    return False


def _is_transient_network_error(err: BaseException) -> bool:
    """Сеть к Outsee / host рефов — retry, не wipe кадра."""
    if isinstance(err, (OSError, TimeoutError)):
        return True
    try:
        import httpx

        if isinstance(err, httpx.HTTPError):
            return True
    except Exception:  # noqa: BLE001
        pass
    msg = str(getattr(err, "reason", None) or err).lower()
    ctx = ""
    if isinstance(err, OutseeImageError):
        ctx = str((err.context or {}).get("network") or "")
    blob = f"{msg} {ctx}"
    return any(
        m in blob
        for m in (
            "all connection attempts failed",
            "connection reset",
            "connection refused",
            "temporarily unavailable",
            "network",
            "timed out",
            "timeout",
            "frame upload failed",
            "image_url не получен",
            "name or service not known",
            "getaddrinfo failed",
            "server disconnected",
        )
    )


def _concurrency_backoff_s(wait_n: int) -> float:
    idx = max(0, min(wait_n - 1, len(_CONCURRENCY_BACKOFF_S) - 1))
    return _CONCURRENCY_BACKOFF_S[idx]


def _is_prompt_related_error(err: OutseeImageError) -> bool:
    """Ошибки длины/обрезки промта — нужно GPT-сжатие, не rewrite модерации."""
    if outsee_error_is_moderation(err):
        return False
    if isinstance(err, OutseePromptTooLongError):
        return True
    if isinstance(err, OutseeContentRejectedError):
        return False
    reason = (err.reason or "").lower()
    if any(m in reason for m in _PROMPT_ERROR_MARKERS):
        return True
    ctx = err.context or {}
    if ctx.get("actual_len") is not None and ctx.get("expected_len") is not None:
        try:
            actual = int(ctx["actual_len"])
            expected = int(ctx["expected_len"])
        except (TypeError, ValueError):
            return False
        return expected >= 200 and actual < int(expected * 0.85)
    return False


def _target_body_chars_from_error(
    err: OutseeImageError,
    prefix: str | None,
) -> int | None:
    """Целевая длина тела промта после обрезки outsee (если известна)."""
    ctx = err.context or {}
    actual = ctx.get("actual_len")
    if actual is not None:
        try:
            actual_i = int(actual)
        except (TypeError, ValueError):
            actual_i = 0
        if actual_i >= 200:
            # ID-prefix уже в textarea — вычитаем его из фактической длины.
            reserve = _prefix_reserve(prefix)
            return max(400, actual_i - reserve - 20)
    prompt_len = ctx.get("prompt_len")
    if prompt_len is not None:
        try:
            pl = int(prompt_len)
        except (TypeError, ValueError):
            pl = 0
        if pl > OUTSEE_PROMPT_MAX_CHARS:
            return _max_body_for_prefix(prefix) - 200
    return None


async def _gpt_shrink_text(
    gpt: Any,
    text: str,
    max_chars: int,
    *,
    project_id: int | None = None,
    meta: str,
) -> str | None:
    """GPT-сжатие одного куска текста до max_chars."""
    last = text.strip()
    if len(last) <= max_chars:
        return last
    for attempt in range(1, 4):
        if attempt == 1:
            ask = f"{meta}\n\n{last}"
        else:
            ask = (
                f"Прошлый ответ был {len(last)} символов — нужно ≤{max_chars}. "
                f"Сожми ещё сильнее, сохрани суть. Верни ТОЛЬКО текст.\n\n"
                f"Прошлый промт:\n\n{last}"
            )
        logger.info(
            "outsee_retry: GPT-сжатие attempt {}/{} — жду ответ ChatGPT "
            "(промт {} симв, лимит {})",
            attempt,
            3,
            len(ask),
            max_chars,
        )
        try:
            reply = await asyncio.wait_for(
                gpt.ask_fresh(ask, timeout=600, project_id=project_id),
                timeout=_GPT_COMPRESS_OUTER_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.error(
                "outsee_retry: GPT-сжатие таймаут {:.0f}с — кадр failed, "
                "воркер идёт к следующему",
                _GPT_COMPRESS_OUTER_TIMEOUT_S,
            )
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "outsee_retry: GPT-сжатие упало ({}: {})", type(e).__name__, e
            )
            return None
        last = strip_prompt_id_lines((reply or "").strip())
        if len(last) < _MIN_REWRITE_LEN:
            continue
        if len(last) <= max_chars:
            return last
        logger.warning(
            "outsee_retry: GPT-сжатие attempt {}: {} симв (нужно ≤{})",
            attempt, len(last), max_chars,
        )
    return None


async def _compress_prompt_for_outsee(
    gpt: Any,
    prompt_body: str,
    *,
    prefix: str | None = None,
    project_id: int | None = None,
    max_body: int | None = None,
) -> str | None:
    """Сжимает тело промта до лимита outsee. STYLE/Negative не отдаём в GPT."""
    max_body = max_body if max_body is not None else _max_body_for_prefix(prefix)
    last = prompt_body.strip()
    if len(last) <= max_body:
        return last
    scene, style = _split_style_lock(last)
    if style:
        scene_limit = max_body - len(style) - (2 if scene else 0)
        if scene_limit < 40:
            joined = _hard_truncate_prompt(last, max_body)
            logger.info(
                "outsee_retry: STYLE {} симв почти весь лимит {} — сцена обрезана",
                len(style),
                max_body,
            )
            return joined
        if len(scene) <= scene_limit:
            joined = _join_scene_style(scene, style)
            if len(joined) <= max_body:
                logger.info(
                    "outsee_retry: сцена уже влезает, STYLE не сжимал: {} → {}",
                    len(last),
                    len(joined),
                )
                return joined
        meta = (
            f"Сожми ТОЛЬКО сцену кадра до ≤{scene_limit} символов. "
            "Убери повторы и воду, оставь визуальные детали. "
            "Не пиши STYLE / Final style lock / Negative — их добавлю сам. "
            "Верни ТОЛЬКО сжатую сцену без пояснений."
        )
        shrunk = await _gpt_shrink_text(
            gpt, scene, scene_limit, project_id=project_id, meta=meta
        )
        if not shrunk:
            return None
        scene_only, leaked_style = _split_style_lock(shrunk)
        if leaked_style:
            logger.warning(
                "outsee_retry: GPT вернул STYLE в сжатии — отбросил, "
                "оставил исходный lock {} симв",
                len(style),
            )
        joined = _join_scene_style(scene_only or shrunk, style)
        if len(joined) > max_body:
            joined = _hard_truncate_prompt(joined, max_body)
        logger.info(
            "outsee_retry: GPT-сжатие OK: {} → {} симв (лимит {}, STYLE {} сохранён)",
            len(prompt_body),
            len(joined),
            max_body,
            len(style),
        )
        return joined

    meta = (
        f"Сожми промт для outsee.io до ≤{max_body} символов (включая пробелы). "
        "Убери повторы и воду, оставь суть и визуальные детали. "
        "Верни ТОЛЬКО новый текст без пояснений."
    )
    shrunk = await _gpt_shrink_text(
        gpt, last, max_body, project_id=project_id, meta=meta
    )
    if shrunk:
        logger.info(
            "outsee_retry: GPT-сжатие OK: {} → {} симв (лимит {})",
            len(prompt_body), len(shrunk), max_body,
        )
    return shrunk


# HTTP API /api/v1/videos/generate жёстко режет на 4096; CDP textarea — 4900.
_OUTSEE_API_VIDEO_PROMPT_MAX = 4096


async def _prepare_prompt_for_outsee(
    gpt: Any | None,
    prompt_body: str,
    prefix: str | None,
    *,
    project_id: int | None = None,
    max_body: int | None = None,
    max_full: int | None = None,
) -> str:
    prompt_body = strip_prompt_id_lines(prompt_body)
    full_limit = max_full if max_full is not None else OUTSEE_PROMPT_MAX_CHARS
    full = _outsee_full_prompt(prompt_body, prefix)
    body_limit = (
        max_body
        if max_body is not None
        else _max_body_for_prefix(prefix, cap=full_limit)
    )
    if len(prompt_body) <= body_limit and len(full) <= full_limit:
        return prompt_body
    logger.warning(
        "outsee_retry: промт {} симв (full {}), лимит body {} / outsee {} — "
        "сжимаю через GPT",
        len(prompt_body),
        len(full),
        body_limit,
        full_limit,
    )
    if gpt is None:
        cut = _hard_truncate_prompt(prompt_body, body_limit)
        logger.warning(
            "outsee_retry: GPT недоступен — hard-truncate {} → {} (лимит {})",
            len(prompt_body),
            len(cut),
            full_limit,
        )
        return cut
    compressed = await _compress_prompt_for_outsee(
        gpt,
        prompt_body,
        prefix=prefix,
        project_id=project_id,
        max_body=body_limit,
    )
    if not compressed:
        cut = _hard_truncate_prompt(prompt_body, body_limit)
        logger.warning(
            "outsee_retry: GPT-сжатие не удалось — hard-truncate {} → {}",
            len(prompt_body),
            len(cut),
        )
        return cut
    full2 = _outsee_full_prompt(compressed, prefix)
    if len(full2) > full_limit:
        cut = _hard_truncate_prompt(compressed, body_limit)
        logger.warning(
            "outsee_retry: после сжатия full {} > {} — hard-truncate to {}",
            len(full2),
            full_limit,
            len(cut),
        )
        return cut
    return compressed


async def _ask_gpt_to_rewrite(
    gpt: Any,
    original_prompt: str,
    *,
    project_id: int | None = None,
    last_error: OutseeImageError | None = None,
    prefix: str | None = None,
) -> str | None:
    """Запрашивает у ChatGPT переписанный промт без триггеров модерации.
    Возвращает stripped-текст, либо None если rewrite не получился."""
    body_limit = _max_body_for_prefix(prefix)
    moderation = last_error is not None and outsee_error_is_moderation(last_error)
    if moderation:
        full_request = (
            f"{_gpt_moderation_rewrite_meta(body_limit)}\n\n{original_prompt}"
        )
    else:
        err_hint = ""
        if last_error is not None:
            err_hint = (
                f"\n\nПоследняя ошибка outsee:\n{last_error.reason[:500]}"
            )
            if _is_prompt_related_error(last_error):
                err_hint += (
                    f"\n\nOutsee не принимает такую длину — сожми до ≤{body_limit} "
                    "символов в теле промта (без ID-строки)."
                )
        full_request = (
            f"{_GPT_REWRITE_META} Лимит outsee: ≤{body_limit} символов в теле "
            f"промта (без строки [ID: …]).\n\n"
            f"{original_prompt}{err_hint}"
        )
    ask_timeout = (
        _GPT_MODERATION_REWRITE_ASK_TIMEOUT_S if moderation else 600.0
    )
    outer_timeout = (
        _GPT_MODERATION_REWRITE_OUTER_TIMEOUT_S
        if moderation
        else _GPT_REWRITE_OUTER_TIMEOUT_S
    )
    try:
        reply = await asyncio.wait_for(
            gpt.ask_fresh(
                full_request, timeout=ask_timeout, project_id=project_id
            ),
            timeout=outer_timeout,
        )
    except asyncio.TimeoutError:
        logger.error(
            "outsee_retry: GPT-rewrite таймаут {:.0f}с{} — кадр дальше "
            "не держим, очередь картинок продолжит",
            outer_timeout,
            " (модерация)" if moderation else "",
        )
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "outsee_retry: GPT-rewrite не получился ({}: {}) — "
            "продолжать нечем",
            type(e).__name__, e,
        )
        return None
    text = strip_prompt_id_lines((reply or "").strip())
    if len(text) < _MIN_REWRITE_LEN:
        logger.warning(
            "outsee_retry: GPT-rewrite вернул слишком короткий ответ "
            "({} симв) — игнорирую",
            len(text),
        )
        return None
    logger.info(
        "outsee_retry: GPT-rewrite OK, новый промт {} симв (был {})",
        len(text), len(original_prompt),
    )
    if len(text) > body_limit:
        # После модерации НЕ зовём GPT-сжатие: очередь img последовательная,
        # сжатие = минуты без единой генерации (как в логе P47-F43).
        if moderation:
            cut = _hard_truncate_prompt(text, body_limit)
            logger.warning(
                "outsee_retry: GPT-rewrite {} симв > лимит {} — "
                "hard-truncate до {} (без GPT-сжатия, очередь не блокируем)",
                len(text),
                body_limit,
                len(cut),
            )
            return cut
        over_full = len(_outsee_full_prompt(text, prefix)) > OUTSEE_PROMPT_MAX_CHARS
        overrun = len(text) - body_limit
        if overrun <= max(120, body_limit // 20) or over_full:
            if overrun <= max(120, body_limit // 20):
                cut = _hard_truncate_prompt(text, body_limit)
                logger.warning(
                    "outsee_retry: GPT-rewrite {} симв > лимит {} "
                    "(+{}) — hard-truncate до {} симв",
                    len(text),
                    body_limit,
                    overrun,
                    len(cut),
                )
                return cut
            logger.warning(
                "outsee_retry: GPT-rewrite {} симв > лимит (body {}, full {}) — сожму",
                len(text),
                body_limit,
                len(_outsee_full_prompt(text, prefix)),
            )
            compressed = await _compress_prompt_for_outsee(
                gpt, text, prefix=prefix, project_id=project_id, max_body=body_limit
            )
            if compressed:
                text = compressed
            elif len(text) > body_limit:
                cut = _hard_truncate_prompt(text, body_limit)
                logger.warning(
                    "outsee_retry: GPT-сжатие не удалось — hard-truncate "
                    "{} → {} симв",
                    len(text),
                    len(cut),
                )
                text = cut
    return text


async def _fix_prompt_after_outsee_error(
    gpt: Any,
    prompt_body: str,
    err: OutseeImageError,
    *,
    prefix: str | None,
    project_id: int | None,
) -> str | None:
    """Сразу после ошибки — сжать (длина) или переписать (модерация)."""
    if outsee_error_is_moderation(err):
        logger.info(
            "outsee_retry: модерация outsee — GPT-rewrite промта ({} симв)",
            len(prompt_body),
        )
        return await _ask_gpt_to_rewrite(
            gpt,
            prompt_body,
            project_id=project_id,
            last_error=err,
            prefix=prefix,
        )
    if isinstance(err, OutseePromptTooLongError) or _is_prompt_related_error(err):
        target = _target_body_chars_from_error(err, prefix)
        body_limit = target if target is not None else _max_body_for_prefix(prefix)
        logger.info(
            "outsee_retry: лимит символов outsee после «{}» — "
            "GPT-сжатие до ≤{} симв",
            err.reason[:80],
            body_limit,
        )
        return await _compress_prompt_for_outsee(
            gpt,
            prompt_body,
            prefix=prefix,
            project_id=project_id,
            max_body=body_limit,
        )
    return None


def _retry_err_label(e: OutseeImageError) -> str:
    return outsee_error_kind_label(outsee_error_kind(e))


def _apply_video_fallback_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Kling 2.6 slug + дефолты поверх настроек проекта (для логов/CDP)."""
    merged = dict(kwargs)
    merged.update(outsee_video_fallback_fields())
    return merged


def _should_use_video_fallback(*, round_idx: int, gen_failures: int) -> bool:
    """True после исчерпания primary (4 burns) или со 2-го раунда (compat)."""
    if round_idx > 0:
        return True
    return gen_failures >= OUTSEE_VIDEO_FALLBACK_AFTER_FAILURES


def _video_fallback_active(kwargs: dict[str, Any]) -> bool:
    fb = outsee_video_fallback_fields()
    slug = str(kwargs.get("model_slug") or "")
    return slug in (fb["model_slug"], KIE_KLING_FALLBACK_SLUG, "kling_2_6")


def _uniquify_prompt_id(base: str | None, round_idx: int, attempt: int) -> str | None:
    """Делает `prompt_id_prefix` уникальным для текущей retry-итерации.

    Без этого ВСЕ попытки повторной генерации одной и той же картинки
    приходят в outsee с одинаковым ID (`[ID: P11-EXCEL-c01]`) — и
    анти-дубликат-чек в `_generate_image_on_page` находит ОСТАВШУЮСЯ
    карточку прошлой провалившейся попытки, решает «генерация уже идёт,
    не кликаю Generate повторно» и пытается скачать ту же отбракованную
    модерацией картинку. Из-за этого retry превращался в три «не делай
    ничего» на одной и той же сломанной карточке.

    Меняем хвост ID на `[… r{round}a{attempt}]` — каждая новая попытка
    становится отдельной картой outsee со своим ID, и анти-дуп-чек
    больше не путает её с прошлыми.
    """
    if not base:
        return base
    if round_idx == 0 and attempt == 1:
        return base  # первая попытка — оригинальный ID без шума
    # base = "[ID: P11-EXCEL-c01]" → "[ID: P11-EXCEL-c01 r1a2]"
    stripped = base.strip()
    if stripped.endswith("]"):
        return f"{stripped[:-1]} r{round_idx + 1}a{attempt}]"
    return f"{stripped} r{round_idx + 1}a{attempt}"


async def generate_image_with_retries(
    outsee: OutseeBot | None,
    gpt: Any | None,
    *,
    prompt: str,
    out_path: Path,
    max_attempts_per_prompt: int = 3,
    gpt_rewrite: bool = True,
    **kwargs: Any,
) -> GenerationResult:
    """Обёртка над `OutseeBot.generate_image` / Grsai с авто-ретраем и
    GPT-rewrite. Подробности — в docstring модуля.

    Все `kwargs` пробрасываются как есть в `outsee.generate_image`
    (или в `grsai.generate_image`, если IMAGE_PROVIDER=grsai).
    `prompt_id_prefix` один на весь кадр (все retry и GPT-rewrite) —
    формат `[ID: P12-F3-a7f2b01c]`, где `a7f2b01c` = gen_id этой генерации.
    """
    from app.bots.outsee_http import (
        generate_image as outsee_api_generate_image,
        outsee_api_configured,
        studio_id_to_outsee_image_slug,
    )
    from app.services.media_route import image_provider_for
    from app.settings import settings as _settings

    raw_slug = kwargs.get("model_slug") or getattr(
        _settings, "outsee_default_image_model", None
    )
    backend = image_provider_for(str(raw_slug) if raw_slug else None)
    use_outsee_api = backend == "outsee" and outsee_api_configured()
    if backend == "outsee" and not outsee_api_configured() and outsee is None:
        raise OutseeImageError(
            "OUTSEE_API_KEY пуст — генерация идёт через ключ Outsee",
            context={"error_kind": "no_key", "provider": "outsee"},
        )
    last_err: OutseeImageError | None = None
    current_prompt = prompt
    base_prompt_id = kwargs.get("prompt_id_prefix")
    rounds: list[tuple[str, str]] = [("original", current_prompt)]
    if gpt_rewrite and gpt is not None:
        rounds.append(("rewritten", ""))  # placeholder, заполним если дойдём
    _DOWNLOAD_ONLY_RETRIES = 2

    for round_idx, (round_label, _) in enumerate(rounds):
        pid = kwargs.get("project_id")
        attempt = 0
        concurrency_waits = 0
        while attempt < max_attempts_per_prompt:
            attempt += 1
            abort_if_cancelled(pid if isinstance(pid, int) else None)
            from app.services.api_tracker_hook import check_api_allowed
            check_api_allowed(provider=str(backend or "image"), model=str(kwargs.get("model_slug") or "image"))
            attempt_kwargs = dict(kwargs)
            attempt_kwargs["prompt_id_prefix"] = base_prompt_id
            try:
                send_prompt = await _prepare_prompt_for_outsee(
                    gpt,
                    current_prompt,
                    attempt_kwargs.get("prompt_id_prefix")
                    if isinstance(attempt_kwargs.get("prompt_id_prefix"), str)
                    else None,
                    project_id=pid if isinstance(pid, int) else None,
                )
                if use_outsee_api:
                    refs = attempt_kwargs.get("reference_image")
                    ref_list: list[Any] | None = None
                    if isinstance(refs, Path):
                        ref_list = [refs]
                    elif isinstance(refs, list):
                        ref_list = [p for p in refs if p is not None]
                    raw_slug = attempt_kwargs.get("model_slug") or getattr(
                        _settings, "outsee_default_image_model", None
                    )
                    slug = studio_id_to_outsee_image_slug(
                        str(raw_slug) if raw_slug else None
                    )
                    ar = attempt_kwargs.get("aspect_ratio") or "9:16"
                    res = attempt_kwargs.get("resolution") or attempt_kwargs.get(
                        "image_resolution"
                    )
                    if ref_list:
                        logger.info(
                            "outsee_retry: {} ref(s) → Outsee HTTP API image_urls",
                            len(ref_list),
                        )
                    if backend == "kie":
                        from app.bots.kie_http import generate_kie_image

                        result = await generate_kie_image(
                            send_prompt,
                            out_path,
                            model_slug=str(raw_slug or "seedream-5-pro"),
                            aspect_ratio=str(ar).replace("_", ":"),
                            resolution=str(res) if res else "2K",
                            reference_images=ref_list,
                            timeout=float(attempt_kwargs.get("timeout") or 600),
                            gen_id=attempt_kwargs.get("gen_id"),
                            project_id=pid if isinstance(pid, int) else None,
                        )
                        try:
                            from app.services.generation_storage import write_sidecar

                            write_sidecar(
                                result.file_path,
                                media="image",
                                model=str(raw_slug),
                                prompt=send_prompt,
                                params={
                                    "aspect": str(ar).replace("_", ":"),
                                    "resolution": str(res) if res else "2K",
                                    "project_id": pid,
                                    "gen_id": attempt_kwargs.get("gen_id"),
                                },
                                raw_url=result.raw_url,
                                quote=None,
                                provider="kie",
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug("kie sidecar write skipped", exc_info=True)
                        try:
                            from app.services.api_tracker_hook import record_api_call

                            record_api_call(
                                provider="kie",
                                model=str(raw_slug or "image-model"),
                                call_type="image",
                                media_count=1,
                                status_code=200,
                                project_source=f"pipeline_project_{pid}" if pid else "pipeline",
                                metadata={"prompt": send_prompt[:500]},
                            )
                        except Exception:
                            pass
                        return result
                    if use_outsee_api:
                        result = await outsee_api_generate_image(
                            send_prompt,
                            out_path,
                            model_slug=slug,
                            aspect_ratio=str(ar).replace("_", ":"),
                            resolution=str(res) if res else "2K",
                            detail_level=attempt_kwargs.get("quality"),
                            reference_images=ref_list,
                            prompt_id_prefix=attempt_kwargs.get("prompt_id_prefix"),
                            timeout=float(attempt_kwargs.get("timeout") or 600),
                            gen_id=attempt_kwargs.get("gen_id"),
                            project_id=pid if isinstance(pid, int) else None,
                        )
                        try:
                            from app.services.generation_storage import write_sidecar

                            write_sidecar(
                                result.file_path,
                                media="image",
                                model=slug,
                                prompt=send_prompt,
                                params={
                                    "aspect": str(ar).replace("_", ":"),
                                    "resolution": str(res) if res else "2K",
                                    "project_id": pid,
                                    "gen_id": attempt_kwargs.get("gen_id"),
                                },
                                raw_url=result.raw_url,
                                quote=None,
                                provider="outsee",
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug("outsee sidecar write skipped", exc_info=True)
                        try:
                            from app.services.api_tracker_hook import record_api_call

                            record_api_call(
                                provider="outsee",
                                model=slug,
                                call_type="image",
                                media_count=1,
                                status_code=200,
                                project_source=f"pipeline_project_{pid}" if pid else "pipeline",
                                metadata={"prompt": send_prompt[:500]},
                            )
                        except Exception:
                            pass
                        return result
                    return await outsee.generate_image(
                        send_prompt, out_path, **attempt_kwargs
                    )
            except StepCancelledError:
                raise
            except OutseeDownloadError as e:
                # Generate уже прошёл — только повтор скачивания, без нового Generate.
                raw_url = e.context.get("img_url")
                gen_id = str(e.context.get("gen_id") or "")
                prefix = attempt_kwargs.get("prompt_id_prefix")
                from app.bots.outsee import _resolve_best_download_url

                img_url = (
                    _resolve_best_download_url(raw_url)
                    if isinstance(raw_url, str) and raw_url
                    else raw_url
                )
                if isinstance(img_url, str) and img_url and gen_id:
                    for dl_try in range(1, _DOWNLOAD_ONLY_RETRIES + 1):
                        abort_if_cancelled(
                            pid if isinstance(pid, int) else None
                        )
                        try:
                            return await outsee.retry_image_download(
                                img_url=img_url,
                                out_path=out_path,
                                gen_id=gen_id,
                                prompt_id_prefix=(
                                    prefix if isinstance(prefix, str) else None
                                ),
                                project_id=pid if isinstance(pid, int) else None,
                                model_slug=attempt_kwargs.get("model_slug"),
                            )
                        except OutseeDownloadError as dl_err:
                            last_err = dl_err
                            logger.warning(
                                "outsee.retry_image_download [{}] {}/{}: {}",
                                round_label,
                                dl_try,
                                _DOWNLOAD_ONLY_RETRIES,
                                dl_err.reason,
                            )
                            if dl_try < _DOWNLOAD_ONLY_RETRIES:
                                await sleep_cancellable(
                                    2.0,
                                    pid if isinstance(pid, int) else None,
                                )
                    logger.warning(
                        "outsee.generate_image [{}] download-only retries "
                        "исчерпаны (id={}) — без нового Generate",
                        round_label,
                        prefix or "—",
                    )
                # Карточка уже на outsee: повторный Generate только orphan'ит результат.
                raise last_err or e
            except OutseeImageError as e:
                last_err = e
                if _is_concurrency_limit_error(e):
                    concurrency_waits += 1
                    if concurrency_waits > _CONCURRENCY_MAX_WAITS:
                        raise
                    delay = _concurrency_backoff_s(concurrency_waits)
                    attempt -= 1
                    logger.warning(
                        "outsee.generate_image [{}] лимит одновременных "
                        "генераций — жду {:.0f}с (wait {}/{}, id={})",
                        round_label,
                        delay,
                        concurrency_waits,
                        _CONCURRENCY_MAX_WAITS,
                        attempt_kwargs.get("prompt_id_prefix") or "—",
                    )
                    await sleep_cancellable(
                        delay,
                        pid if isinstance(pid, int) else None,
                    )
                    continue
                err_kind = _retry_err_label(e)
                is_moderation = outsee_error_is_moderation(e)
                if is_moderation:
                    logger.warning(
                        "outsee.generate_image [{}] МОДЕРАЦИЯ outsee "
                        "попытка {}/{} (id={}): {}",
                        round_label,
                        attempt,
                        max_attempts_per_prompt,
                        attempt_kwargs.get("prompt_id_prefix") or "—",
                        e.reason,
                    )
                else:
                    logger.warning(
                        "outsee.generate_image [{}] попытка {}/{} ({}, id={}): {}",
                        round_label, attempt, max_attempts_per_prompt,
                        err_kind,
                        attempt_kwargs.get("prompt_id_prefix") or "—",
                        e.reason,
                    )
                prefix = (
                    attempt_kwargs.get("prompt_id_prefix")
                    if isinstance(attempt_kwargs.get("prompt_id_prefix"), str)
                    else None
                )
                moderation_rewrite_failed = False
                if (
                    gpt is not None
                    and attempt < max_attempts_per_prompt
                    and (
                        isinstance(e, OutseePromptTooLongError)
                        or _is_prompt_related_error(e)
                        or is_moderation
                    )
                ):
                    fixed = await _fix_prompt_after_outsee_error(
                        gpt,
                        current_prompt,
                        e,
                        prefix=prefix,
                        project_id=pid if isinstance(pid, int) else None,
                    )
                    if fixed and fixed.strip() != current_prompt.strip():
                        action = (
                            "GPT-rewrite" if is_moderation else "GPT-сжатие"
                        )
                        logger.info(
                            "outsee.generate_image [{}]: {} OK "
                            "({} → {} симв, ошибка={})",
                            round_label,
                            action,
                            len(current_prompt),
                            len(fixed),
                            err_kind,
                        )
                        current_prompt = fixed
                    elif is_moderation:
                        moderation_rewrite_failed = True
                        logger.warning(
                            "outsee.generate_image [{}]: модерация — GPT не "
                            "переписал промт, не повторяю тот же текст",
                            round_label,
                        )
                elif is_moderation and gpt is None:
                    # Без GPT бессмысленно долбить тот же banned-промт 3×.
                    logger.warning(
                        "outsee.generate_image [{}]: модерация без GPT — "
                        "не повторяю тот же текст",
                        round_label,
                    )
                    break
                if moderation_rewrite_failed:
                    break
                if attempt < max_attempts_per_prompt:
                    await sleep_cancellable(2.0, pid if isinstance(pid, int) else None)

        # Все попытки в этом раунде провалились. Если ещё есть раунд
        # «rewritten» — попробуем переписать промт через GPT.
        is_last_round = round_idx == len(rounds) - 1
        if is_last_round:
            break
        if gpt is None:
            logger.warning(
                "outsee.generate_image [{}]: GPT недоступен — rewrite пропущен",
                round_label,
            )
            break
        logger.info(
            "outsee.generate_image: GPT-rewrite после раунда «{}»",
            round_label,
        )
        rewritten = await _ask_gpt_to_rewrite(
            gpt,
            current_prompt,
            project_id=pid if isinstance(pid, int) else None,
            last_error=last_err,
            prefix=base_prompt_id,
        )
        if not rewritten:
            logger.warning(
                "outsee.generate_image: GPT-rewrite не вернул текст — выхожу"
            )
            break
        current_prompt = rewritten

    if last_err is None:
        # сюда мы попасть не должны (raise/return должны были отработать)
        raise RuntimeError("generate_image_with_retries: unreachable")
    raise last_err


async def generate_video_with_retries(
    outsee: OutseeBot | None,
    gpt: Any | None,
    *,
    prompt: str,
    out_path: Path,
    max_attempts_per_prompt: int = 3,
    gpt_rewrite: bool = True,
    project_id: int | None = None,
    uniquify_prompt_id: bool = False,
    **kwargs: Any,
) -> GenerationResult:
    """Лестница видео на клип: Veo (3 rewrite + 1 final) → Kling 2.6 ×3 → skip.

    `max_attempts_per_prompt` сохранён для совместимости вызовов; фактические
    лимиты — VIDEO_PRIMARY_TOTAL_ATTEMPTS / VIDEO_FALLBACK_ATTEMPTS.
    """
    del max_attempts_per_prompt  # лестница фиксирована константами
    last_err: BaseException | None = None
    current_prompt = prompt
    base_prompt_id = kwargs.get("prompt_id_prefix")
    primary_kwargs = dict(kwargs)
    primary_slug = str(primary_kwargs.get("model_slug") or "veo")
    from app.bots.kie_kling import (
        generate_video as kie_kling_generate_video,
        kie_api_configured,
        truncate_kling_prompt,
    )
    from app.bots.outsee_http import (
        generate_video as outsee_api_generate_video,
        outsee_api_configured,
        studio_id_to_outsee_video_slug,
    )
    from app.services.media_route import video_provider_for
    from app.settings import settings as _settings

    primary_backend = video_provider_for(primary_slug)
    use_outsee_api_video = primary_backend == "outsee" and outsee_api_configured()
    primary_is_kling = primary_backend == "kie"
    if primary_backend == "outsee" and not outsee_api_configured() and outsee is None:
        raise OutseeImageError(
            "OUTSEE_API_KEY пуст — генерация идёт через ключ Outsee",
            context={"error_kind": "no_key", "provider": "outsee"},
        )

    _DOWNLOAD_ONLY_RETRIES = 2
    primary_burns = 0
    fallback_burns = 0
    local_sanitized = False
    start_frame_policy_hits = 0
    concurrency_waits = 0
    transient_streak = 0
    fallback_started = False
    phase = "fallback" if primary_is_kling else "primary"
    if primary_is_kling:
        fallback_started = True
        logger.info("outsee_retry: video primary Kling 2.6 via kie (KIE_API_KEY)")

    async def _prepare_send(prompt_body: str, attempt_kwargs: dict[str, Any], *, kling: bool) -> str:
        prefix = (
            attempt_kwargs.get("prompt_id_prefix")
            if isinstance(attempt_kwargs.get("prompt_id_prefix"), str)
            else None
        )
        send_prompt = ensure_silent_video_prompt(prompt_body)
        if kling:
            send_prompt = truncate_kling_prompt(send_prompt)
            return ensure_silent_video_prompt(send_prompt)
        api_full_cap = (
            _OUTSEE_API_VIDEO_PROMPT_MAX
            if use_outsee_api_video
            else OUTSEE_PROMPT_MAX_CHARS
        )
        send_prompt = await _prepare_prompt_for_outsee(
            gpt,
            send_prompt,
            prefix,
            project_id=project_id,
            max_full=api_full_cap,
        )
        send_prompt = ensure_silent_video_prompt(send_prompt)
        full_len = len(_outsee_full_prompt(send_prompt, prefix))
        if full_len > api_full_cap:
            body_cap = _max_body_for_prefix(prefix, cap=api_full_cap)
            send_prompt = _hard_truncate_prompt(send_prompt, body_cap)
            logger.warning(
                "outsee_retry: video prompt still {} > {} — hard-truncate to {}",
                full_len,
                api_full_cap,
                len(send_prompt),
            )
        return send_prompt

    async def _dispatch(
        send_prompt: str,
        attempt_kwargs: dict[str, Any],
        *,
        kling: bool,
    ) -> GenerationResult:
        attempt_kwargs = dict(attempt_kwargs)
        attempt_kwargs["generate_audio"] = False
        if kling:
            raw_slug = attempt_kwargs.get("model_slug") or "kling-2-6"
            from app.bots.kie_http import generate_kie_video

            result = await generate_kie_video(
                send_prompt,
                out_path,
                model_slug=str(raw_slug),
                start_frame=attempt_kwargs.get("start_frame"),
                aspect_ratio=str(attempt_kwargs.get("aspect_ratio") or "9:16"),
                resolution=str(attempt_kwargs.get("resolution") or "720p"),
                duration=attempt_kwargs.get("duration") or 5,
                generate_audio=False,
                timeout=float(attempt_kwargs.get("timeout") or 900),
                project_id=project_id,
                gen_id=attempt_kwargs.get("gen_id"),
            )
            try:
                from app.services.generation_storage import write_sidecar

                write_sidecar(
                    result.file_path,
                    media="video",
                    model=str(raw_slug),
                    prompt=send_prompt,
                    params={
                        "aspect": str(attempt_kwargs.get("aspect_ratio") or "9:16"),
                        "duration": attempt_kwargs.get("duration") or 5,
                        "project_id": project_id,
                        "ladder": "fallback" if phase == "fallback" else "primary",
                    },
                    raw_url=result.raw_url,
                    quote=None,
                    provider="kie",
                )
            except Exception:  # noqa: BLE001
                logger.debug("kie video sidecar skipped", exc_info=True)
            return result
        if use_outsee_api_video:
            raw_slug = attempt_kwargs.get("model_slug") or getattr(
                _settings, "outsee_default_video_model", None
            )
            slug = studio_id_to_outsee_video_slug(str(raw_slug) if raw_slug else None)
            ar = attempt_kwargs.get("aspect_ratio") or "9:16"
            res = attempt_kwargs.get("resolution") or "720p"
            dur = attempt_kwargs.get("duration")
            result = await outsee_api_generate_video(
                send_prompt,
                out_path,
                model_slug=slug,
                aspect_ratio=str(ar).replace("_", ":"),
                resolution=str(res),
                duration=int(dur) if dur else None,
                generate_audio=False,
                reference_image=attempt_kwargs.get("start_frame"),
                prompt_id_prefix=attempt_kwargs.get("prompt_id_prefix"),
                timeout=float(attempt_kwargs.get("timeout") or 900),
                gen_id=attempt_kwargs.get("gen_id"),
                project_id=project_id,
            )
            try:
                from app.services.generation_storage import write_sidecar

                write_sidecar(
                    result.file_path,
                    media="video",
                    model=slug,
                    prompt=send_prompt,
                    params={
                        "aspect": str(ar).replace("_", ":"),
                        "resolution": str(res),
                        "duration": dur,
                        "project_id": project_id,
                        "ladder": "primary",
                    },
                    raw_url=result.raw_url,
                    quote=None,
                    provider="outsee",
                )
            except Exception:  # noqa: BLE001
                logger.debug("outsee video sidecar skipped", exc_info=True)
            try:
                from app.services.api_tracker_hook import record_api_call

                record_api_call(
                    provider="outsee",
                    model=slug,
                    call_type="video",
                    media_count=1,
                    duration_sec=float(dur or 5),
                    status_code=200,
                    project_source=f"pipeline_project_{project_id}" if project_id else "pipeline",
                    metadata={"prompt": send_prompt[:500]},
                )
            except Exception:
                pass
            return result
        return await outsee.generate_video(
            send_prompt, out_path, project_id=project_id, **attempt_kwargs
        )

    async def _rewrite_prompt_after_fail(err: BaseException) -> None:
        nonlocal current_prompt, local_sanitized
        prefix = base_prompt_id if isinstance(base_prompt_id, str) else None
        fixed: str | None = None
        if gpt_rewrite and gpt is not None and isinstance(err, OutseeImageError):
            fixed = await _fix_prompt_after_outsee_error(
                gpt, current_prompt, err, prefix=prefix, project_id=project_id
            )
        if fixed and fixed.strip() != current_prompt.strip():
            current_prompt = _apply_local_prompt_sanitize(
                fixed, where="video ladder rewrite", force_simplify=False
            )
            local_sanitized = True
            return
        # GPT недоступен / не помог — локальная санация (триггеры + simplify)
        current_prompt = _apply_local_prompt_sanitize(
            current_prompt, where="video ladder local sanitize"
        )
        local_sanitized = True

    while True:
        abort_if_cancelled(project_id)
        from app.services.api_tracker_hook import check_api_allowed
        check_api_allowed(provider="kling" if phase == "fallback" else "video", model=str(primary_slug or "video"))
        use_kling = phase == "fallback"
        if use_kling:
            if fallback_burns >= VIDEO_FALLBACK_ATTEMPTS:
                break
            attempt_n = fallback_burns + 1
            attempt_max = VIDEO_FALLBACK_ATTEMPTS
            attempt_kwargs = _apply_video_fallback_kwargs(primary_kwargs)
            if not fallback_started:
                logger.info(
                    "outsee_retry: video FALLBACK → Kling 2.6 ({})",
                    ladder_summary(
                        primary_burns=primary_burns,
                        fallback_burns=fallback_burns,
                        model_primary=primary_slug,
                        model_fallback=KIE_KLING_FALLBACK_SLUG,
                    ),
                )
                fallback_started = True
                if not local_sanitized:
                    current_prompt = _apply_local_prompt_sanitize(
                        current_prompt, where="video before Kling fallback"
                    )
                    local_sanitized = True
                current_prompt = truncate_kling_prompt(
                    ensure_silent_video_prompt(current_prompt),
                    max_chars=KIE_KLING_PROMPT_MAX_CHARS,
                )
        else:
            if primary_burns >= VIDEO_PRIMARY_TOTAL_ATTEMPTS:
                phase = "fallback"
                continue
            attempt_n = primary_burns + 1
            attempt_max = VIDEO_PRIMARY_TOTAL_ATTEMPTS
            attempt_kwargs = dict(primary_kwargs)

        if uniquify_prompt_id:
            attempt_kwargs["prompt_id_prefix"] = _uniquify_prompt_id(
                base_prompt_id if isinstance(base_prompt_id, str) else None,
                1 if use_kling else 0,
                attempt_n,
            )
        else:
            attempt_kwargs["prompt_id_prefix"] = base_prompt_id

        provider_label = (
            "kie-kling"
            if use_kling
            else ("outsee-api" if use_outsee_api_video else "outsee-cdp")
        )
        logger.info(
            "outsee_retry: video [{}] попытка {}/{} model={} provider={} {}",
            phase,
            attempt_n,
            attempt_max,
            attempt_kwargs.get("model_slug"),
            provider_label,
            ladder_summary(
                primary_burns=primary_burns,
                fallback_burns=fallback_burns,
                model_primary=primary_slug,
                model_fallback=KIE_KLING_FALLBACK_SLUG,
            ),
        )
        try:
            send_prompt = await _prepare_send(
                current_prompt, attempt_kwargs, kling=use_kling
            )
            return await _dispatch(send_prompt, attempt_kwargs, kling=use_kling)
        except StepCancelledError:
            raise
        except OutseeDownloadError as e:
            last_err = e
            video_url = e.context.get("video_url")
            gen_id = str(e.context.get("gen_id") or "")
            if isinstance(video_url, str) and video_url and gen_id and not use_kling:
                for dl_try in range(1, _DOWNLOAD_ONLY_RETRIES + 1):
                    abort_if_cancelled(project_id)
                    try:
                        return await outsee.retry_video_download(
                            video_url=video_url,
                            out_path=out_path,
                            gen_id=gen_id,
                            prompt_id_prefix=attempt_kwargs.get("prompt_id_prefix"),
                            project_id=project_id,
                            model_slug=attempt_kwargs.get("model_slug"),
                        )
                    except OutseeDownloadError as dl_err:
                        last_err = dl_err
                        if dl_try < _DOWNLOAD_ONLY_RETRIES:
                            await sleep_cancellable(2.0, project_id)
                raise last_err
            # download без повторного Generate — считаем burn
            classified = classify_video_error(e)
        except OutseeImageError as e:
            last_err = e
            classified = classify_video_error(e)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, VideoLadderExhaustedError):
                raise
            if not _is_transient_network_error(e):
                raise
            last_err = OutseeImageError(
                f"outsee video network: {e}",
                context={"network": True, "exc_type": type(e).__name__},
            )
            classified = classify_video_error(last_err)

        action = classified.action
        logger.warning(
            "outsee_retry: video [{}] {}/{} code={} action={} :: {}",
            phase,
            attempt_n,
            attempt_max,
            classified.code,
            action.value,
            classified.detail[:180],
        )

        if action == VideoErrorAction.STOP_CANCEL:
            raise last_err or StepCancelledError("video cancelled")
        if action == VideoErrorAction.STOP_AUTH:
            raise last_err or OutseeImageError(classified.detail)

        if action == VideoErrorAction.RETRY_SAME or (
            action == VideoErrorAction.DOWNLOAD_RETRY and use_kling
        ):
            if _is_concurrency_limit_error(last_err or Exception()):
                concurrency_waits += 1
                if concurrency_waits > _CONCURRENCY_MAX_WAITS:
                    raise last_err or OutseeImageError("video concurrency exhausted")
                delay = _concurrency_backoff_s(concurrency_waits)
                await sleep_cancellable(delay, project_id)
                continue
            transient_streak += 1
            if transient_streak <= 2:
                await sleep_cancellable(3.0, project_id)
                continue
            # слишком много transient подряд — сжигаем попытку
            action = VideoErrorAction.RETRY_BURN

        if action == VideoErrorAction.SOFTEN_FRAME:
            sf = primary_kwargs.get("start_frame")
            sf_path = sf if isinstance(sf, Path) else (Path(str(sf)) if sf else None)
            start_frame_policy_hits += 1
            if (
                sf_path is not None
                and start_frame_policy_hits <= _START_FRAME_SOFTEN_MAX
            ):
                softened = _soften_start_frame_for_policy(
                    sf_path, strength=start_frame_policy_hits
                )
                if softened is not None:
                    primary_kwargs = dict(primary_kwargs)
                    primary_kwargs["start_frame"] = softened
                    current_prompt = _apply_local_prompt_sanitize(
                        current_prompt,
                        where="video soften start_frame CONTENT_POLICY",
                    )
                    local_sanitized = True
                    await sleep_cancellable(2.0, project_id)
                    continue
            current_prompt = _apply_local_prompt_sanitize(
                current_prompt, where="video keep start_frame CONTENT_POLICY"
            )
            local_sanitized = True
            action = VideoErrorAction.RETRY_BURN

        if action == VideoErrorAction.SILENT_AUDIO:
            current_prompt = ensure_silent_video_prompt(
                _apply_local_prompt_sanitize(
                    current_prompt, where="video audio CONTENT_POLICY"
                )
            )
            local_sanitized = True
            action = VideoErrorAction.RETRY_REWRITE

        # burn attempt
        transient_streak = 0
        if phase == "primary":
            primary_burns += 1
            do_rewrite = (
                action in (VideoErrorAction.RETRY_REWRITE, VideoErrorAction.RETRY_BURN)
                and should_rewrite_after_primary_burn(primary_burns)
            )
            # На попытках 1..3 после fail — всегда меняем текст (контракт).
            if primary_burns <= VIDEO_PRIMARY_REWRITE_ATTEMPTS:
                do_rewrite = True
            if do_rewrite:
                await _rewrite_prompt_after_fail(last_err or Exception(classified.detail))
            if primary_burns >= VIDEO_PRIMARY_TOTAL_ATTEMPTS:
                phase = "fallback"
            else:
                await sleep_cancellable(2.0, project_id)
            continue

        # fallback burns
        fallback_burns += 1
        if action == VideoErrorAction.RETRY_REWRITE or fallback_burns < VIDEO_FALLBACK_ATTEMPTS:
            current_prompt = truncate_kling_prompt(
                _apply_local_prompt_sanitize(
                    current_prompt, where=f"kling attempt {fallback_burns}"
                ),
                max_chars=KIE_KLING_PROMPT_MAX_CHARS,
            )
        if fallback_burns >= VIDEO_FALLBACK_ATTEMPTS:
            break
        await sleep_cancellable(2.0, project_id)

    summary = ladder_summary(
        primary_burns=primary_burns,
        fallback_burns=fallback_burns,
        model_primary=primary_slug,
        model_fallback=KIE_KLING_FALLBACK_SLUG,
    )
    detail = str(last_err.reason if isinstance(last_err, OutseeImageError) else last_err)
    try:
        from app.services.api_tracker_hook import record_api_call
        record_api_call(
            provider="kling" if fallback_burns > 0 else "outsee",
            model=KIE_KLING_FALLBACK_SLUG if fallback_burns > 0 else primary_slug,
            call_type="video",
            media_count=1,
            duration_sec=0.0,
            cost_usd=0.0,
            status_code=500,
            error_message=f"Video ladder exhausted: {detail[:250]}",
            project_source="video_ladder",
            metadata={"primary_burns": primary_burns, "fallback_burns": fallback_burns},
        )
    except Exception:
        pass
    raise VideoLadderExhaustedError(
        f"video ladder exhausted: {summary}; last={detail[:240]}",
        context={
            "primary_burns": primary_burns,
            "fallback_burns": fallback_burns,
            "primary_model": primary_slug,
            "fallback_model": KIE_KLING_FALLBACK_SLUG,
            "last_error": detail[:300],
        },
    )


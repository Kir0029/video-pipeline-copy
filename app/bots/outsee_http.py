"""Outsee.io Developer API — Bearer OUTSEE_API_KEY (не Grsai, не cookies).

Документированные эндпоинты (проверено живым ключом):
  GET  /api/v1/models
  GET  /api/v1/balance | /api/v1/usage
  POST /api/v1/images/generate  → {id, status: queued|…}
  POST /api/v1/videos/generate
  GET  /api/v1/generations/{id} → {status, result_url, result_urls, error}

Auth: Authorization: Bearer <OUTSEE_API_KEY>
"""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from app.bots.outsee import GenerationResult, OutseeImageError
from app.generation_options import prepend_gen_id
from app.settings import settings

_DATA_URL_RE = re.compile(
    r"^data:(image/(?:png|jpeg|jpg|webp));base64,([A-Za-z0-9+/=\s]+)$",
    re.IGNORECASE,
)

_DEFAULT_BASE = "https://outsee.io"
_POLL_INTERVAL_S = 2.5
_DEFAULT_TIMEOUT_S = 600.0

# Живой каталог /api/v1/models (image). Nano Banana Pro запрещена навсегда.
OUTSEE_WIRED_IMAGE_MODELS: tuple[str, ...] = (
    "nano-banana-2",
    "gpt-image-2",
)

NANO_BANANA_PRO_OUTSEE_BAN = (
    "Nano Banana Pro запрещена на Outsee — нельзя генерировать этой моделью через Outsee"
)
# Живой каталог /api/v1/models (video)
OUTSEE_WIRED_VIDEO_MODELS: tuple[str, ...] = ("veo-3-1-lite",)


class OutseeApiError(OutseeImageError):
    """Ошибка Outsee Developer API (совместима с outsee_retry)."""


class NanoBananaProOutseeBannedError(OutseeApiError):
    """Жёсткий запрет: Nano Banana Pro через Outsee нельзя никогда."""


# Обратная совместимость с cookie-era hooks в OutseeBot
OutseeHttpError = OutseeApiError
OutseeHttpAuthError = OutseeApiError


def outsee_api_key() -> str:
    return (settings.outsee_api_key or "").strip()


def outsee_api_configured() -> bool:
    return bool(outsee_api_key())


def outsee_api_enabled_for_image() -> bool:
    """IMAGE_PROVIDER=outsee + ключ."""
    return outsee_api_configured() and (
        (settings.image_provider or "").lower() == "outsee"
    )


def outsee_api_enabled_for_video() -> bool:
    return outsee_api_configured() and (
        (getattr(settings, "video_provider", None) or "").lower() == "outsee"
    )


def outsee_http_enabled() -> bool:
    """Есть Developer API key — можно звать Bearer без Chrome."""
    return outsee_api_configured()


def _base_url() -> str:
    return (settings.outsee_api_base_url or _DEFAULT_BASE).rstrip("/")


def _headers() -> dict[str, str]:
    key = outsee_api_key()
    if not key:
        raise OutseeHttpAuthError(
            "OUTSEE_API_KEY пуст — добавь ключ из https://outsee.io/profile",
            context={"hint": "Authorization: Bearer <ключ>"},
        )
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def assert_not_nano_banana_pro_on_outsee(slug: str | None) -> None:
    from app.services.media_route import is_nano_banana_pro

    if is_nano_banana_pro(slug):
        raise NanoBananaProOutseeBannedError(
            NANO_BANANA_PRO_OUTSEE_BAN,
            context={"model": slug, "provider": "outsee", "banned": True},
        )


def studio_id_to_outsee_image_slug(studio_id: str | None) -> str:
    from app.services.media_route import is_nano_banana_pro

    default = settings.outsee_default_image_model or "gpt-image-2"
    if is_nano_banana_pro(default):
        default = "gpt-image-2"
    if not studio_id:
        return default
    assert_not_nano_banana_pro_on_outsee(studio_id)
    s = studio_id.strip()
    if s in OUTSEE_WIRED_IMAGE_MODELS:
        return s
    mapping = {
        "gpt_image_2": "gpt-image-2",
        "gpt-image-2": "gpt-image-2",
        "nano_banana_2": "nano-banana-2",
        "nano-banana-2": "nano-banana-2",
        "gpt_image_1_5": "gpt-image-2",
        "nano_banana_2_lite": "nano-banana-2",
        "nano-banana-2-lite": "nano-banana-2",
        "nano_banana_fast": "nano-banana-2",
        "nano-banana-fast": "nano-banana-2",
        "nano_banana": "nano-banana-2",
        "nano-banana": "nano-banana-2",
    }
    mapped = mapping.get(s, s.replace("_", "-"))
    assert_not_nano_banana_pro_on_outsee(mapped)
    if mapped in OUTSEE_WIRED_IMAGE_MODELS:
        return mapped
    return default


def studio_id_to_outsee_video_slug(studio_id: str | None) -> str:
    default = settings.outsee_default_video_model or "veo-3-1-lite"
    if not studio_id:
        return default
    s = studio_id.strip()
    if s in OUTSEE_WIRED_VIDEO_MODELS:
        return s
    mapping = {
        "veo_3_1_lite": "veo-3-1-lite",
        "veo-3-1-lite": "veo-3-1-lite",
        "veo_3_1_fast": "veo-3-1-lite",
        "veo-3-1-fast": "veo-3-1-lite",
        "veo_3_fast": "veo-3-1-lite",
        "veo-3-fast": "veo-3-1-lite",
        "veo3.1-fast": "veo-3-1-lite",
        "veo3.1-lite": "veo-3-1-lite",
        # Kling 2.6 fallback идёт через kie.ai (app/bots/kie_kling.py), не Outsee API.
        # Если slug Kling попал сюда по ошибке — не притворяемся Veo молча:
        # падаем в default Veo primary (логирует caller).
    }
    return mapping.get(s, default)


def _raise_api(resp: httpx.Response, *, where: str) -> None:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {"raw": resp.text[:400]}
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        code = err.get("code") or "error"
        msg = err.get("message") or resp.text[:300]
    else:
        code = f"http_{resp.status_code}"
        msg = str(err or data)[:300]
    if resp.status_code in (401, 403) or code in {"unauthorized", "forbidden"}:
        raise OutseeHttpAuthError(
            f"Outsee API auth ({where}): {msg}",
            context={"status": resp.status_code, "code": code},
        )
    raise OutseeApiError(
        f"Outsee API {where}: {msg}",
        context={"status": resp.status_code, "code": code, "body": data},
    )


def _strip_banned_outsee_image_models(data: dict[str, Any]) -> dict[str, Any]:
    from app.services.media_route import is_nano_banana_pro

    def keep(item: Any) -> bool:
        if isinstance(item, dict):
            slug = str(item.get("id") or item.get("slug") or item.get("model") or "")
            return not is_nano_banana_pro(slug)
        if isinstance(item, str):
            return not is_nano_banana_pro(item)
        return True

    out = dict(data)
    for key, val in list(out.items()):
        if isinstance(val, list):
            out[key] = [x for x in val if keep(x)]
    return out


async def fetch_models() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{_base_url()}/api/v1/models", headers=_headers())
        if r.status_code >= 400:
            _raise_api(r, where="models")
        data = r.json()
        payload = data if isinstance(data, dict) else {}
        return _strip_banned_outsee_image_models(payload)


async def fetch_balance() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{_base_url()}/api/v1/balance", headers=_headers())
        if r.status_code >= 400:
            _raise_api(r, where="balance")
        data = r.json()
        return data if isinstance(data, dict) else {}


_CONCURRENCY_MARKERS = (
    "лимит одновремен",
    "одновременных генерац",
    "дождитесь завершения текущих",
    "too many concurrent",
    "concurrent generation",
    "concurrency limit",
)
_CONCURRENCY_MAX_WAITS = 3
_CONCURRENCY_BACKOFF_S = (15.0, 30.0, 45.0)


def _is_concurrency_api_error(err: BaseException) -> bool:
    if not isinstance(err, OutseeApiError):
        return False
    code = str((err.context or {}).get("code") or "").lower()
    if code in {"concurrency_limit", "rate_limit", "too_many_requests"}:
        return True
    blob = str(err).lower()
    return any(m in blob for m in _CONCURRENCY_MARKERS)


def _concurrency_backoff_s(wait_n: int) -> float:
    idx = max(0, min(wait_n - 1, len(_CONCURRENCY_BACKOFF_S) - 1))
    return _CONCURRENCY_BACKOFF_S[idx]


async def _post_generate(path: str, body: dict[str, Any]) -> dict[str, Any]:
    waits = 0
    last_err: BaseException | None = None
    while True:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                r = await client.post(
                    f"{_base_url()}{path}", headers=_headers(), json=body
                )
                if r.status_code >= 400:
                    _raise_api(r, where=path)
                data = r.json()
                if not isinstance(data, dict) or data.get("id") is None:
                    raise OutseeApiError(
                        "Outsee generate: нет id",
                        context={"path": path, "body": data},
                    )
                logger.info(
                    "outsee_api.submitted path={} id={} status={}",
                    path,
                    data.get("id"),
                    data.get("status"),
                )
                return data
        except OutseeApiError as e:
            if not _is_concurrency_api_error(e):
                raise
            last_err = e
            waits += 1
            if waits > _CONCURRENCY_MAX_WAITS:
                raise
            delay = _concurrency_backoff_s(waits)
            logger.warning(
                "outsee_api: concurrency_limit {} — жду {:.0f}с (wait {}/{})",
                path,
                delay,
                waits,
                _CONCURRENCY_MAX_WAITS,
            )
            await asyncio.sleep(delay)
            continue
        except (httpx.HTTPError, OSError) as e:
            raise OutseeApiError(
                f"Outsee API network {path}: {e}",
                context={"path": path, "network": True},
            ) from e
    raise last_err or OutseeApiError("Outsee generate: concurrency exhausted")


async def _poll_generation(
    gen_id: int | str, *, timeout: float
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    last: dict[str, Any] = {}
    url = f"{_base_url()}/api/v1/generations/{gen_id}"
    transient_5xx_streak = 0
    max_transient_5xx = 6
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    r = await client.get(url, headers=_headers())
                except (httpx.HTTPError, OSError) as e:
                    logger.warning(
                        "outsee_api.poll network id={} err={} — retry",
                        gen_id,
                        e,
                    )
                    await asyncio.sleep(_POLL_INTERVAL_S)
                    continue
                if r.status_code in {500, 502, 503, 504}:
                    transient_5xx_streak += 1
                    if transient_5xx_streak <= max_transient_5xx:
                        logger.warning(
                            "outsee_api.poll transient HTTP {} id={} (streak {}/{}) — retry in {}s",
                            r.status_code,
                            gen_id,
                            transient_5xx_streak,
                            max_transient_5xx,
                            _POLL_INTERVAL_S * 1.5,
                        )
                        await asyncio.sleep(_POLL_INTERVAL_S * 1.5)
                        continue
                else:
                    transient_5xx_streak = 0

                if r.status_code >= 400:
                    _raise_api(r, where=f"generations/{gen_id}")
                data = r.json()
                if not isinstance(data, dict):
                    await asyncio.sleep(_POLL_INTERVAL_S)
                    continue
                last = data
                status = (data.get("status") or "").lower()
                if status in {"queued", "processing", "pending", "running"}:
                    await asyncio.sleep(_POLL_INTERVAL_S)
                    continue
                if status in {"failed", "error", "rejected", "cancelled"}:
                    err = data.get("error") or status
                    code = ""
                    if isinstance(err, dict):
                        code = str(err.get("code") or "")
                    raise OutseeApiError(
                        f"Outsee generation failed: {err}",
                        context={
                            "id": gen_id,
                            "status": data,
                            "code": code.lower() if code else "",
                        },
                    )
                return data
    except OutseeApiError:
        raise
    except (httpx.HTTPError, OSError) as e:
        raise OutseeApiError(
            f"Outsee API network poll id={gen_id}: {e}",
            context={"id": gen_id, "network": True},
        ) from e
    raise OutseeApiError(
        f"Outsee: таймаут ожидания id={gen_id} ({timeout:.0f}s)",
        context={"last": last},
    )


async def _download(url: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(
                timeout=180.0, follow_redirects=True
            ) as client:
                r = await client.get(url)
                if r.status_code >= 400 or len(r.content) < 64:
                    raise OutseeApiError(
                        f"Outsee download HTTP {r.status_code}",
                        context={"url": url[:160], "bytes": len(r.content)},
                    )
                out_path.write_bytes(r.content)
            return out_path
        except (httpx.HTTPError, OSError, OutseeApiError) as e:
            last_err = e
            logger.warning(
                "outsee_api.download retry {}/3 url={} err={}",
                attempt,
                url[:120],
                e,
            )
            await asyncio.sleep(1.5 * attempt)
    raise OutseeApiError(
        f"Outsee download failed after retries: {last_err}",
        context={"url": url[:160]},
    ) from last_err


def _assert_video_not_mush(path: Path, *, duration_sec: int) -> None:
    """Проверить, что клип скачался. Порог битрейта не ставим:

    clay/lite 720p 8с часто 0.7–2.0 MB — отбраковка «мыла» гнала на Kling 401
    и блокировала очередь.
    """
    del duration_sec
    if not path.is_file():
        raise OutseeApiError(f"видео не скачано: {path.name}")
    size = path.stat().st_size
    if size < 1024:
        raise OutseeApiError(
            f"видео пустое или обрезано ({size} bytes)",
            context={"path": str(path), "bytes": size},
        )


def _pick_result_url(status: dict[str, Any]) -> str:
    urls = status.get("result_urls")
    if isinstance(urls, list):
        for u in urls:
            if isinstance(u, str) and u.startswith("http"):
                return u
    u = status.get("result_url")
    if isinstance(u, str) and u.startswith("http"):
        return u
    raise OutseeApiError(
        "Outsee: нет result_url",
        context={"keys": list(status.keys())[:20], "status": status.get("status")},
    )


def _path_to_data_url(path: Path) -> str:
    raw = path.read_bytes()
    mime = "image/png"
    suf = path.suffix.lower()
    if suf in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    elif suf == ".webp":
        mime = "image/webp"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _decode_data_url(url: str) -> tuple[bytes, str] | None:
    m = _DATA_URL_RE.match((url or "").strip())
    if not m:
        return None
    mime = m.group(1).lower().replace("image/jpg", "image/jpeg")
    try:
        raw = base64.b64decode(re.sub(r"\s+", "", m.group(2)), validate=False)
    except Exception:  # noqa: BLE001
        return None
    if len(raw) < 32:
        return None
    return raw, mime


_FRAME_UPLOAD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Бесплатные хосты часто валятся на 4–6 МБ PNG; JPEG ≤ этого — ок.
_FRAME_HOST_SOFT_MAX_BYTES = 1_800_000


def _looks_like_image_bytes(raw: bytes, content_type: str | None = None) -> bool:
    """Отсекаем HTML-лендинги (tmpfiles /dl/ часто отдаёт text/html, не JPEG)."""
    if not raw or len(raw) < 16:
        return False
    head = raw[:32].lstrip()
    if head[:1] == b"<" or head[:9].lower() == b"<!doctype" or head[:5].lower() == b"<html":
        return False
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("text/html") or ctype in {"text/plain", "application/json"}:
        return False
    if raw[:3] == b"\xff\xd8\xff":
        return True  # JPEG
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return True
    if ctype.startswith("image/"):
        return True
    return False


async def _verify_hosted_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    min_bytes: int = 32,
    attempts: int = 3,
) -> bool:
    """GET с ретраями: catbox/litter часто «пустые» первые секунды после upload."""
    last_err = ""
    for i in range(max(1, attempts)):
        try:
            # HEAD часто режет ботов — пробуем GET с Range, потом полный GET.
            r = await client.get(url, headers={"Range": "bytes=0-2047"})
            body = r.content or b""
            if (
                r.status_code in (200, 206)
                and len(body) >= min(min_bytes, 16)
                and _looks_like_image_bytes(body, r.headers.get("content-type"))
            ):
                return True
            if r.status_code in (200, 206) and body and not _looks_like_image_bytes(
                body, r.headers.get("content-type")
            ):
                last_err = (
                    f"HTTP {r.status_code} not-image "
                    f"ctype={r.headers.get('content-type')!r} head={body[:12]!r}"
                )
            else:
                r = await client.get(url)
                body = r.content or b""
                if (
                    r.status_code < 400
                    and len(body) >= min_bytes
                    and _looks_like_image_bytes(body, r.headers.get("content-type"))
                ):
                    return True
                last_err = (
                    f"HTTP {r.status_code} bytes={len(body)} "
                    f"ctype={r.headers.get('content-type')!r}"
                )
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)[:120]
        if i + 1 < attempts:
            await asyncio.sleep(0.7 * (i + 1))
    logger.debug("outsee_api.frame verify miss {}: {}", url[:100], last_err)
    return False


def _jpeg_variant(raw: bytes, *, max_side: int = 2048, quality: int = 85) -> tuple[bytes, str] | None:
    """Сжать кадр в JPEG — litterbox/catbox стабильнее на ~1 МБ, чем на 4+ МБ PNG."""
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    try:
        img = Image.open(BytesIO(raw))
        img = img.convert("RGB")
        w, h = img.size
        scale = min(1.0, float(max_side) / float(max(w, h, 1)))
        if scale < 0.999:
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.LANCZOS,
            )
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) < 64:
            return None
        return out, "image/jpeg"
    except Exception as exc:  # noqa: BLE001
        logger.warning("outsee_api.frame jpeg shrink failed: {}", exc)
        return None


def _upload_payload_variants(raw: bytes, mime: str) -> list[tuple[bytes, str, str]]:
    """Варианты для заливки: оригинал (если не гигант) + JPEG fallback."""
    variants: list[tuple[bytes, str, str]] = []
    if len(raw) <= _FRAME_HOST_SOFT_MAX_BYTES:
        variants.append((raw, mime, "orig"))
    else:
        logger.info(
            "outsee_api.frame: {} bytes > {} — сначала JPEG, потом orig",
            len(raw),
            _FRAME_HOST_SOFT_MAX_BYTES,
        )
    jpg = _jpeg_variant(raw)
    if jpg is not None:
        jraw, jmime = jpg
        variants.append((jraw, jmime, f"jpeg:{len(jraw)}"))
        # Ещё более лёгкий JPEG, если первый всё ещё жирный.
        if len(jraw) > _FRAME_HOST_SOFT_MAX_BYTES:
            jpg2 = _jpeg_variant(raw, max_side=1440, quality=72)
            if jpg2 is not None and len(jpg2[0]) < len(jraw):
                variants.append((jpg2[0], jpg2[1], f"jpeg72:{len(jpg2[0])}"))
    # Оригинал в хвост, если ещё не пробовали (маленький шанс на жирный PNG).
    if not any(v[2] == "orig" for v in variants):
        variants.append((raw, mime, "orig"))
    # Уникальные по (len, mime)
    seen: set[tuple[int, str]] = set()
    out: list[tuple[bytes, str, str]] = []
    for item in variants:
        key = (len(item[0]), item[1])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


async def _accept_hosted_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    host: str,
    raw_len: int,
) -> str:
    """Проверяем, что URL реально отдаёт байты — иначе Outsee тоже не скачает.

    Soft-accept (раньше) залипал на catbox с пустым GET → Outsee:
    «Не удалось скачать изображение по ссылке».
    """
    ok = await _verify_hosted_image(client, url, min_bytes=min(32, max(16, raw_len // 1000)))
    if ok:
        return url
    raise OutseeApiError(
        f"{host}: verify miss после upload ({url[:100]})",
        context={"host": host, "url": url[:160]},
    )


def _host_name_from_url(url: str) -> str | None:
    low = (url or "").lower()
    if "storage.yandexcloud.net" in low or "yandexcloud.net" in low:
        return "yandex"
    if "litter.catbox" in low or "litterbox" in low:
        return "litterbox"
    if "catbox.moe" in low:
        return "catbox"
    if "tmpfiles.org" in low:
        return "tmpfiles"
    if "0x0.st" in low:
        return "0x0"
    if "uguu.se" in low:
        return "uguu"
    return None


async def _host_via_yandex(
    client: httpx.AsyncClient, raw: bytes, mime: str, filename: str
) -> str:
    from app.bots.yandex_storage import upload_public_bytes, yandex_storage_configured

    if not yandex_storage_configured():
        raise OutseeApiError("yandex storage: не настроен")
    url = await upload_public_bytes(client, raw, mime, filename)
    return await _accept_hosted_url(client, url, host="yandex", raw_len=len(raw))


async def _host_via_uguu(client: httpx.AsyncClient, raw: bytes, mime: str, filename: str) -> str:
    r = await client.post(
        "https://uguu.se/upload.php",
        files={"files[]": (filename, raw, mime)},
    )
    body_txt = (r.text or "")[:200]
    if r.status_code >= 400:
        raise OutseeApiError(f"uguu HTTP {r.status_code}: {body_txt or '(empty body)'}")
    try:
        payload = r.json()
        url = str((((payload or {}).get("files") or [{}])[0]).get("url") or "").strip()
    except Exception as exc:  # noqa: BLE001
        raise OutseeApiError(f"uguu bad JSON: {body_txt or '(empty body)'}") from exc
    if not url.startswith("http"):
        raise OutseeApiError(f"uguu no url: {body_txt or '(empty body)'}")
    return await _accept_hosted_url(client, url, host="uguu", raw_len=len(raw))


async def _host_via_litterbox(client: httpx.AsyncClient, raw: bytes, mime: str, filename: str) -> str:
    url = "https://litterbox.catbox.moe/resources/internals/api.php"
    for attempt in range(2):
        try:
            r = await client.post(
                url,
                data={"reqtype": "fileupload", "time": "24h"},
                files={"fileToUpload": (filename, raw, mime)},
            )
            text = (r.text or "").strip()
            if r.status_code < 400 and text.startswith("http"):
                return await _accept_hosted_url(client, text, host="litterbox", raw_len=len(raw))
            if attempt == 0 and r.status_code in {500, 502, 503, 504}:
                await asyncio.sleep(1.0)
                continue
            raise OutseeApiError(
                f"litterbox HTTP {r.status_code}: {text[:160] or '(empty body)'}"
            )
        except OutseeApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            raise OutseeApiError(f"litterbox connection fail: {exc}") from exc
    raise OutseeApiError("litterbox: exhausted retries")


async def _host_via_catbox(client: httpx.AsyncClient, raw: bytes, mime: str, filename: str) -> str:
    url = "https://catbox.moe/user/api.php"
    for attempt in range(2):
        try:
            r = await client.post(
                url,
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (filename, raw, mime)},
            )
            text = (r.text or "").strip()
            if r.status_code < 400 and text.startswith("http"):
                return await _accept_hosted_url(client, text, host="catbox", raw_len=len(raw))
            if attempt == 0 and r.status_code in {412, 429, 500, 502, 503, 504}:
                await asyncio.sleep(1.0)
                continue
            raise OutseeApiError(f"catbox HTTP {r.status_code}: {text[:160] or '(empty body)'}")
        except OutseeApiError:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            raise OutseeApiError(f"catbox connection fail: {exc}") from exc
    raise OutseeApiError("catbox: exhausted retries")


async def _host_via_0x0(client: httpx.AsyncClient, raw: bytes, mime: str, filename: str) -> str:
    r = await client.post(
        "https://0x0.st",
        files={"file": (filename, raw, mime)},
    )
    text = (r.text or "").strip()
    if r.status_code >= 400 or not text.startswith("http"):
        raise OutseeApiError(f"0x0 HTTP {r.status_code}: {text[:160] or '(empty body)'}")
    return await _accept_hosted_url(client, text.split()[0], host="0x0", raw_len=len(raw))


async def _host_via_tmpfiles(client: httpx.AsyncClient, raw: bytes, mime: str, filename: str) -> str:
    r = await client.post(
        "https://tmpfiles.org/api/v1/upload",
        files={"file": (filename, raw, mime)},
    )
    if r.status_code >= 400:
        raise OutseeApiError(f"tmpfiles HTTP {r.status_code}: {(r.text or '')[:160]}")
    try:
        payload = r.json()
        page = str((((payload or {}).get("data") or {}).get("url") or "")).strip()
    except Exception as exc:  # noqa: BLE001
        raise OutseeApiError(f"tmpfiles bad JSON: {(r.text or '')[:160]}") from exc
    if not page.startswith("http"):
        raise OutseeApiError(f"tmpfiles no url: {(r.text or '')[:160]}")
    # https://tmpfiles.org/123/name.png → direct https://tmpfiles.org/dl/123/name.png
    direct = page.replace("://tmpfiles.org/", "://tmpfiles.org/dl/", 1)
    return await _accept_hosted_url(client, direct, host="tmpfiles", raw_len=len(raw))


async def ensure_public_image_url(
    url: str | None,
    *,
    skip_hosts: frozenset[str] | set[str] | None = None,
    force_rehost: bool = False,
) -> str | None:
    """Outsee image_url принимает только http(s); data: молча игнорит.

    data: → публичный URL только через Yandex Object Storage.
    http(s) → как есть (кроме localhost), либо force_rehost=True → скачать и
    залить в Yandex. Публичные хосты (litterbox/catbox/uguu) отключены.
    ``yandex`` в skip_hosts игнорируется: новый PUT = новый URL.
    """
    if not url or not str(url).strip():
        return None
    u = str(url).strip()
    skip = {str(x).lower() for x in (skip_hosts or ())}
    skip.discard("yandex")  # новый PUT = новый URL, yandex не банить
    if u.startswith(("http://", "https://")) and not force_rehost:
        # Outsee не скачает localhost / 127.0.0.1 — молча получится чужое видео
        low = u.lower()
        if "://127." in low or "://localhost" in low or "://[::1]" in low:
            raise OutseeApiError(
                "стартовый кадр: localhost URL недоступен Outsee — нужен data: или публичный https",
                context={"url": u[:120]},
            )
        return u
    raw: bytes
    mime: str
    if u.startswith(("http://", "https://")) and force_rehost:
        async with httpx.AsyncClient(
            timeout=90.0,
            follow_redirects=True,
            headers={"User-Agent": _FRAME_UPLOAD_UA},
        ) as client:
            r = await client.get(u)
            if r.status_code >= 400 or len(r.content or b"") < 64:
                raise OutseeApiError(
                    f"force_rehost: не скачал исходник HTTP {r.status_code}",
                    context={"url": u[:160]},
                )
            raw = r.content
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            mime = ctype if ctype.startswith("image/") else "image/jpeg"
            known = _host_name_from_url(u)
            if known and known != "yandex":
                skip.add(known)
    else:
        decoded = _decode_data_url(u)
        if not decoded:
            logger.warning("outsee_api.frame: не data/http URL, пропускаю")
            return None
        raw, mime = decoded
    errors: list[str] = []
    from app.bots.yandex_storage import yandex_storage_configured

    hosts: list[tuple[str, Any]] = []
    if yandex_storage_configured():
        hosts.append(("yandex", _host_via_yandex))
        # Резервные хосты на случай временного сбоя S3
        hosts.extend([
            ("uguu", _host_via_uguu),
            ("litterbox", _host_via_litterbox),
            ("catbox", _host_via_catbox),
            ("0x0", _host_via_0x0),
        ])
        logger.info("outsee_api.frame: upload host=yandex (с fallback на uguu/litterbox/catbox/0x0, {} bytes)", len(raw))
    else:
        logger.info("outsee_api.frame: Yandex S3 не настроен, использую fallback (uguu/litterbox/catbox/0x0, {} bytes)", len(raw))
        hosts.extend([
            ("uguu", _host_via_uguu),
            ("litterbox", _host_via_litterbox),
            ("catbox", _host_via_catbox),
            ("0x0", _host_via_0x0),
        ])

    variants = _upload_payload_variants(raw, mime)
    async with httpx.AsyncClient(
        timeout=90.0,
        follow_redirects=True,
        headers={"User-Agent": _FRAME_UPLOAD_UA},
    ) as client:
        for payload, pmime, label in variants:
            ext = {
                "image/png": "png",
                "image/jpeg": "jpg",
                "image/webp": "webp",
            }.get(pmime, "jpg")
            filename = f"frame.{ext}"
            for name, upload in hosts:
                if name in skip:
                    continue
                try:
                    hosted = await upload(client, payload, pmime, filename)
                    logger.info(
                        "outsee_api.frame hosted via {} variant={} {} bytes → {}",
                        name,
                        label,
                        len(payload),
                        hosted[:120],
                    )
                    return hosted
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc).strip() or repr(exc)
                    msg = msg[:180]
                    errors.append(f"{name}/{label}: {msg}")
                    logger.warning(
                        "outsee_api.frame host {} variant={} failed: {}",
                        name,
                        label,
                        msg,
                    )
    raise OutseeApiError(
        "frame upload failed (проверь доступность хостов/Yandex S3): "
        + " | ".join(errors)[:500],
        context={
            "mime": mime,
            "bytes": len(raw),
            "variants": [v[2] for v in variants],
            "yandex_configured": yandex_storage_configured(),
        },
    )


def _is_outsee_image_fetch_error(err: BaseException) -> bool:
    """Outsee не смог скачать наш image_url (хост/сеть) — нужна смена хоста."""
    blob = str(getattr(err, "reason", None) or err).lower()
    return any(
        m in blob
        for m in (
            "не удалось скачать изображение",
            "скачать изображение по ссылке",
            "по ссылке находится не изображение",
            "не изображение",
            "failed to download the image",
            "failed to download image",
            "could not download image",
            "unable to download image",
            "not an image",
            "is not an image",
        )
    )


async def probe_has_audio(path: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return bool((stdout or b"").strip())


async def postprocess_veo_mp4(
    path: Path,
    *,
    duration: int | None,
    generate_audio: bool | None,
) -> Path:
    """Outsee Veo всегда отдаёт ~8с + AAC. Подрезаем/глушим локально под UI."""
    from app.services.media_probe import probe_duration

    want_dur = int(duration) if duration is not None else None
    # None / False → mute. True only when Create explicitly asks for audio.
    want_mute = generate_audio is not True
    if not want_mute and want_dur not in {4, 6}:
        return path

    actual = 0.0
    try:
        actual = await probe_duration(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("veo postprocess: probe failed {} — {}", path.name, e)

    trim_to: float | None = None
    if want_dur in {4, 6} and actual >= float(want_dur) + 0.35:
        trim_to = float(want_dur)

    has_a = False
    if want_mute:
        try:
            has_a = await probe_has_audio(path)
        except Exception:  # noqa: BLE001
            has_a = True

    if trim_to is None and not (want_mute and has_a):
        return path

    out = path.with_name(f"{path.stem}_pp{path.suffix}")
    cmd: list[str] = ["ffmpeg", "-y", "-i", str(path)]
    if trim_to is not None:
        cmd.extend(["-t", f"{trim_to:.3f}"])
    if want_mute and has_a:
        cmd.append("-an")
    else:
        cmd.extend(["-c:a", "copy"])
    cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(out)])
    # If only mute (no trim), stream-copy video is faster:
    if trim_to is None and want_mute and has_a:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-c:v",
            "copy",
            "-an",
            str(out),
        ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 64:
        logger.warning(
            "veo postprocess ffmpeg failed rc={} err={}",
            proc.returncode,
            (stderr or b"")[:400].decode(errors="ignore"),
        )
        out.unlink(missing_ok=True)
        return path
    path.write_bytes(out.read_bytes())
    out.unlink(missing_ok=True)
    logger.info(
        "veo postprocess ok path={} trim={} mute={}",
        path.name,
        trim_to,
        want_mute and has_a,
    )
    return path


def _normalize_refs(refs: list[Any] | None) -> list[str] | None:
    if not refs:
        return None
    out: list[str] = []
    for item in refs:
        if isinstance(item, Path):
            if item.is_file():
                out.append(_path_to_data_url(item))
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out[:9] or None


async def generate_image(
    prompt: str,
    out_path: Path,
    *,
    model_slug: str | None = None,
    aspect_ratio: str = "9:16",
    resolution: str | None = "2K",
    detail_level: str | None = None,
    reference_images: list[Any] | None = None,
    prompt_id_prefix: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    gen_id: str | None = None,
    project_id: int | None = None,
    **_kwargs: Any,
) -> GenerationResult:
    """Картинка через POST /api/v1/images/generate.

    Рефы → публичные URL в поле ``image_urls`` (не ``reference_images``:
    каталог models врёт max_reference_images, generate это поле игнорит).
    """
    assert_not_nano_banana_pro_on_outsee(model_slug)
    if prompt_id_prefix:
        prompt = prepend_gen_id(prompt, prompt_id_prefix)
    model = studio_id_to_outsee_image_slug(model_slug)
    assert_not_nano_banana_pro_on_outsee(model)
    body: dict[str, Any] = {
        "prompt": prompt,
        "model": model,
        "aspect_ratio": (aspect_ratio or "9:16").replace("_", ":"),
    }
    if resolution:
        # Outsee /api/v1/images/generate строго требует "2K"
        body["resolution"] = "2K"
    if detail_level:
        dl = str(detail_level).strip().lower()
        mapping = {
            "low": "low",
            "medium": "medium",
            "high": "medium",
            "низкое": "low",
            "среднее": "medium",
            "высокое": "medium",
        }
        body["detail_level"] = mapping.get(dl, dl)
    refs = _normalize_refs(reference_images)
    skip_hosts: set[str] = set()
    submitted: dict[str, Any] | None = None
    last_submit_err: BaseException | None = None
    for host_try in range(1, 4):
        if refs:
            hosted: list[str] = []
            for u in refs:
                pub = await ensure_public_image_url(u, skip_hosts=skip_hosts)
                if pub:
                    hosted.append(pub)
            if not hosted:
                raise OutseeApiError(
                    "outsee_api.image: рефы не удалось залить "
                    "(Yandex Object Storage) — генерация без рефа запрещена",
                    context={
                        "raw_refs": len(refs),
                        "model": model,
                        "project_id": project_id,
                        "skip_hosts": sorted(skip_hosts),
                    },
                )
            # Живой probe 2026-08-03: field `reference_images` Outsee МОЛЧА
            # игнорит (результат без identity). Рабочее поле — `image_urls`
            # (как video: `image_url`, не first_frame_url).
            body["image_urls"] = hosted
            logger.info(
                "outsee_api.image refs ready model={} n={} image_urls={}",
                model,
                len(hosted),
                [u[:80] for u in hosted],
            )

        logger.info(
            "outsee_api.image model={} aspect={} res={} refs={} project={} try={}/3",
            model,
            body.get("aspect_ratio"),
            body.get("resolution"),
            len(body.get("image_urls") or []),
            project_id,
            host_try,
        )
        try:
            submitted = await _post_generate("/api/v1/images/generate", body)
            break
        except Exception as exc:  # noqa: BLE001
            last_submit_err = exc
            if (
                not _is_outsee_image_fetch_error(exc)
                or not refs
                or host_try >= 3
            ):
                raise
            for u in body.get("image_urls") or []:
                bad = _host_name_from_url(str(u))
                # yandex не баним — следующий try = новый объект в бакете.
                if bad and bad != "yandex":
                    skip_hosts.add(bad)
            logger.warning(
                "outsee_api.image: Outsee отверг image_urls (skip={}) try {}/3",
                sorted(skip_hosts),
                host_try,
                3,
            )
    if submitted is None:
        raise last_submit_err or OutseeApiError("image generate: no submit")
    task_id = submitted["id"]
    done = await _poll_generation(task_id, timeout=timeout)
    url = _pick_result_url(done)
    suf = Path(url.split("?")[0]).suffix.lower()
    if suf in {".png", ".jpg", ".jpeg", ".webp"} and out_path.suffix.lower() != suf:
        out_path = out_path.with_suffix(suf if suf != ".jpeg" else ".jpg")
    await _download(url, out_path)
    return GenerationResult(
        file_path=out_path,
        raw_url=url,
        gen_id=str(gen_id or task_id),
    )


async def generate_video(
    prompt: str,
    out_path: Path,
    *,
    model_slug: str | None = None,
    aspect_ratio: str = "9:16",
    resolution: str | None = "720p",
    duration: int | float | None = None,
    generate_audio: bool | None = False,
    first_frame_url: str | None = None,
    last_frame_url: str | None = None,
    reference_image: Path | str | None = None,
    last_frame_image: Path | str | None = None,
    prompt_id_prefix: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_S,
    gen_id: str | None = None,
    project_id: int | None = None,
    **_kwargs: Any,
) -> GenerationResult:
    """Видео через POST /api/v1/videos/generate.

    Veo 3.1 Lite (Outsee): aspect 16:9|9:16, resolution 720p,
    duration_sec 4|6|8, generate_audio, first_frame_url, last_frame_url.
    """
    if prompt_id_prefix:
        prompt = prepend_gen_id(prompt, prompt_id_prefix)
    from app.services.video_prompt_sanitize import ensure_silent_video_prompt

    # Звук всегда off: + явный silent в промте (Veo иначе сам клеит речь).
    prompt = ensure_silent_video_prompt(prompt)
    model = studio_id_to_outsee_video_slug(model_slug)
    aspect = (aspect_ratio or "9:16").replace("_", ":")
    if aspect not in {"16:9", "9:16"}:
        aspect = "16:9"
    body: dict[str, Any] = {
        "prompt": prompt,
        "model": model,
        "aspect_ratio": aspect,
    }

    # Veo Outsee: только 720p (1080p → invalid_request)
    res = (resolution or "720p").strip().lower()
    if model == "veo-3-1-lite":
        body["resolution"] = "720p"
    else:
        body["resolution"] = res if res in {"720p", "1080p"} else "720p"

    # Veo: каталог Outsee фиксирует duration_sec=8; 4/6 шлём и потом режем ffmpeg.
    dur = int(duration or 8)
    if model == "veo-3-1-lite":
        if dur not in {4, 6, 8}:
            dur = 8
    body["duration_sec"] = dur

    # Пайплайн / Studio: звук ВСЕГДА выкл. Veo всё равно может вшить AAC —
    # тогда режем локально; на API шлём только false.
    if generate_audio:
        logger.warning(
            "outsee_api.video: generate_audio=True проигнорирован — всегда silent"
        )
    body["generate_audio"] = False

    want_start = bool(first_frame_url) or reference_image is not None
    frame = first_frame_url
    if not frame and reference_image is not None:
        if isinstance(reference_image, str):
            s = reference_image.strip()
            if s.startswith(("http://", "https://", "data:")):
                frame = s
            else:
                p = Path(s)
                if not p.is_file():
                    raise OutseeApiError(
                        f"стартовый кадр не найден на диске: {s[:200]}",
                        context={"project_id": project_id},
                    )
                frame = _path_to_data_url(p)
        elif isinstance(reference_image, Path):
            if not reference_image.is_file():
                raise OutseeApiError(
                    f"стартовый кадр не найден на диске: {reference_image}",
                    context={"project_id": project_id},
                )
            frame = _path_to_data_url(reference_image)
        else:
            raise OutseeApiError(
                f"стартовый кадр: неподдерживаемый тип {type(reference_image).__name__}",
                context={"project_id": project_id},
            )
    frame_source = frame  # data: или исходный http — для rehost при fetch-fail Outsee
    frame = await ensure_public_image_url(frame)

    want_end = bool(last_frame_url) or last_frame_image is not None
    last = last_frame_url
    if not last and last_frame_image is not None:
        if isinstance(last_frame_image, str):
            s = last_frame_image.strip()
            if s.startswith(("http://", "https://", "data:")):
                last = s
            else:
                p = Path(s)
                if not p.is_file():
                    raise OutseeApiError(
                        f"конечный кадр не найден на диске: {s[:200]}",
                        context={"project_id": project_id},
                    )
                last = _path_to_data_url(p)
        elif isinstance(last_frame_image, Path):
            if not last_frame_image.is_file():
                raise OutseeApiError(
                    f"конечный кадр не найден на диске: {last_frame_image}",
                    context={"project_id": project_id},
                )
            last = _path_to_data_url(last_frame_image)
        else:
            raise OutseeApiError(
                f"конечный кадр: неподдерживаемый тип {type(last_frame_image).__name__}",
                context={"project_id": project_id},
            )
    last_source = last
    last = await ensure_public_image_url(last)
    if want_start and not frame:
        raise OutseeApiError(
            "стартовый кадр передан, но image_url не получен (upload/host)",
            context={"project_id": project_id},
        )
    if want_end and not last:
        raise OutseeApiError(
            "конечный кадр передан, но URL не получен (upload/host)",
            context={"project_id": project_id},
        )
    if frame and last:
        # Один image_url = только старт. Два кадра — как картинки: image_urls.
        body["image_urls"] = [frame, last]
    elif frame:
        body["image_url"] = frame

    skip_hosts: set[str] = set()
    submitted: dict[str, Any] | None = None
    last_submit_err: BaseException | None = None
    for host_try in range(1, 4):
        urls = list(body.get("image_urls") or [])
        logger.info(
            "outsee_api.video model={} aspect={} res={} dur={} audio={} "
            "image_url={} image_urls={} project={} host_try={}",
            model,
            body.get("aspect_ratio"),
            body.get("resolution"),
            body.get("duration_sec"),
            body.get("generate_audio"),
            bool(body.get("image_url")),
            len(urls),
            project_id,
            host_try,
        )
        try:
            submitted = await _post_generate("/api/v1/videos/generate", body)
            break
        except Exception as exc:  # noqa: BLE001
            last_submit_err = exc
            if (
                not _is_outsee_image_fetch_error(exc)
                or not (frame_source or last_source)
                or host_try >= 3
            ):
                raise
            hosted = list(body.get("image_urls") or [])
            bad = _host_name_from_url(
                str(body.get("image_url") or (hosted[0] if hosted else "") or "")
            )
            # yandex не баним: следующий try зальёт новый объект в бакет.
            if bad and bad != "yandex":
                skip_hosts.add(bad)
            logger.warning(
                "outsee_api.video: Outsee не скачал кадр (host={}) — "
                "rehost skip={} try {}/3",
                bad or "?",
                sorted(skip_hosts),
                host_try,
                3,
            )
            changed = False
            new_start = body.get("image_url")
            new_end = None
            pair = list(body.get("image_urls") or [])
            if pair:
                new_start, new_end = pair[0], pair[1] if len(pair) > 1 else None
            if frame_source:
                rehosted = await ensure_public_image_url(
                    frame_source,
                    skip_hosts=skip_hosts,
                    force_rehost=bool(
                        str(frame_source).startswith(("http://", "https://"))
                    ),
                )
                if rehosted and rehosted != new_start:
                    new_start = rehosted
                    changed = True
            if last_source:
                rehosted_last = await ensure_public_image_url(
                    last_source,
                    skip_hosts=skip_hosts,
                    force_rehost=bool(
                        str(last_source).startswith(("http://", "https://"))
                    ),
                )
                if rehosted_last and rehosted_last != new_end:
                    new_end = rehosted_last
                    changed = True
            if not changed:
                raise
            if new_start and new_end:
                body.pop("image_url", None)
                body["image_urls"] = [new_start, new_end]
            elif new_start:
                body.pop("image_urls", None)
                body["image_url"] = new_start
    if submitted is None:
        raise last_submit_err or OutseeApiError("video generate: no submit")
    task_id = submitted["id"]
    done = await _poll_generation(task_id, timeout=timeout)
    url = _pick_result_url(done)
    suf = Path(url.split("?")[0]).suffix.lower()
    if suf in {".mp4", ".webm"} and out_path.suffix.lower() != suf:
        out_path = out_path.with_suffix(suf)
    await _download(url, out_path)
    # Strip AAC always — generate_audio на API форсится False.
    await postprocess_veo_mp4(
        out_path,
        duration=dur if model == "veo-3-1-lite" else None,
        generate_audio=False,
    )
    _assert_video_not_mush(out_path, duration_sec=dur)
    return GenerationResult(
        file_path=out_path,
        raw_url=url,
        gen_id=str(gen_id or task_id),
    )


async def generate_image_http(
    page: Any = None,
    **kwargs: Any,
) -> GenerationResult:
    """Совместимость с OutseeBot._try_http_image(page, …) — page игнорируется."""
    del page
    prompt = kwargs.pop("prompt")
    out_path = kwargs.pop("out_path")
    return await generate_image(prompt, out_path, **kwargs)


async def generate_video_http(
    page: Any = None,
    **kwargs: Any,
) -> GenerationResult:
    """Совместимость с OutseeBot._try_http_video(page, …) — page игнорируется."""
    del page
    prompt = kwargs.pop("prompt")
    out_path = kwargs.pop("out_path")
    return await generate_video(prompt, out_path, **kwargs)


async def generate_music_http(
    page: Any = None,
    *args: Any,
    **kwargs: Any,
) -> GenerationResult:
    del page, args, kwargs
    raise OutseeApiError(
        "Outsee Developer API не поддерживает audio — "
        "используй пайплайн ElevenLabs/Suno или CDP UI",
        context={"hint": "нет /api/v1/audio"},
    )

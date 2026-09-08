"""Хук для автоматической записи активности API-ключей в API Tracker.

Работает через SQLite WAL в фоновом потоке:
- 0 мс задержки для основного пайплайна
- Безопасное подавление ошибок (fail-safe)
- Автоматический расчёт стоимости
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Путь к локальной базе API Tracker
_TRACKER_DB = Path(__file__).resolve().parent.parent.parent / "api-tracker" / "tracker.db"

# Быстрый расчет тарифов
_RATES: dict[str, tuple[float, float]] = {
    # model: (input_usd_per_m, output_usd_per_m)
    "gemini-3.7-flash": (0.147622, 0.738108),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.295243, 1.476216),
    "gemini-3-flash": (0.098414, 0.590486),
    "gemini-3.1-pro": (0.393658, 2.361946),
    "gpt-5.6-sol": (1.20, 4.80),
    "gpt-5.6-terra": (1.50, 6.00),
    "gpt-5.6-luna": (2.00, 8.00),
    "deepseek-v4-flash": (0.291896, 0.875687),
    "deepseek-v4-pro": (0.55, 2.19),
}


_MEDIA_RATES: dict[str, float] = {
    "flux-2-pro": 0.05,
    "seedream-5-pro": 0.045,
    "z-image": 0.02,
    "qwen3-image": 0.035,
    "gpt-image-2": 0.04,
    "kling": 0.08,
    "seedance": 0.06,
    "veo": 0.15,
    "sora": 0.15,
    "wan": 0.05,
    "hailuo": 0.09,
    "suno": 0.05,
    "elevenlabs": 0.01,
    "sound-generation": 0.01,
}


def _calc_cost(
    model: str,
    p_tok: int,
    c_tok: int,
    call_type: str,
    duration: float,
    media_count: int = 1,
    cost_usd: float | None = None,
) -> float:
    if cost_usd is not None and cost_usd >= 0:
        return round(cost_usd, 6)
    m = (model or "").lower().replace("_", "-").replace(" ", "-")
    for k, (in_r, out_r) in _RATES.items():
        if k in m:
            return round((p_tok / 1e6) * in_r + (c_tok / 1e6) * out_r, 6)
    for k, rate in _MEDIA_RATES.items():
        if k in m:
            return round(rate * max(media_count, 1), 6)
    if call_type == "video":
        return round(max(duration, 5.0) * 0.02, 6)
    if call_type == "image":
        return round(0.04 * max(media_count, 1), 6)
    if call_type == "audio":
        return 0.03
    return round((p_tok / 1e6) * 0.50 + (c_tok / 1e6) * 2.00, 6)


def _get_identity() -> tuple[str, str]:
    """Определить имя пользователя и ПК из сохранённого профиля или системы."""
    import getpass
    import os
    import socket
    try:
        app_data = os.environ.get("APPDATA")
        candidates = []
        if app_data:
            candidates.append(Path(app_data) / "API-Tracker" / "user_profile.json")
        candidates.append(_TRACKER_DB.parent / "user_profile.json")
        for p in candidates:
            if p.is_file():
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict) and d.get("user_name"):
                    return str(d["user_name"]).strip(), str(d.get("device_name", "")).strip()
    except Exception:
        pass
    u = os.environ.get("USERNAME") or os.environ.get("USER") or getpass.getuser() or "Пользователь"
    h = socket.gethostname() or "Desktop"
    return u, h


def _push_to_supabase(payload: dict[str, Any]) -> bool:
    """Асинхронная отправка записи в Supabase."""
    import os
    import httpx
    url = os.environ.get("SUPABASE_URL", "").strip() or "https://jubhhajwknvhlntmwpoj.supabase.co"
    key = os.environ.get("SUPABASE_KEY", "").strip() or os.environ.get("SUPABASE_ANON_KEY", "").strip() or "sb_publishable_qxcebL4M8lXRj0s2qF4ZLA_VkHhp3a-"
    if not url or not key:
        return False
    endpoint = f"{url.rstrip('/')}/rest/v1/api_calls"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    try:
        with httpx.Client(timeout=8.0) as client:
            r = client.post(endpoint, headers=headers, json=payload)
            return r.status_code in (200, 201)
    except Exception:
        return False


def record_api_call(
    *,
    provider: str,
    model: str,
    call_type: str = "text",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    media_count: int = 0,
    duration_sec: float = 0.0,
    cost_usd: float | None = None,
    status_code: int = 200,
    error_message: str = "",
    project_source: str = "",
    metadata: dict[str, Any] | None = None,
    timestamp: str | None = None,
) -> None:
    """Асинхронная запись вызова API в базу трекера и облако Supabase."""
    def _write():
        try:
            _TRACKER_DB.parent.mkdir(parents=True, exist_ok=True)
            cost = _calc_cost(
                model,
                prompt_tokens,
                completion_tokens,
                call_type,
                duration_sec,
                media_count=media_count or 1,
                cost_usd=cost_usd,
            )
            total_tok = prompt_tokens + completion_tokens
            ts = timestamp or datetime.now(timezone.utc).isoformat()
            meta_dict = metadata or {}
            meta_json = json.dumps(meta_dict, ensure_ascii=False)

            clean_prov = provider or "unknown"
            p_lower = clean_prov.lower()
            if p_lower == "kie":
                clean_prov = "Kie.ai"
            elif p_lower == "outsee":
                clean_prov = "Outsee"
            elif p_lower == "google":
                clean_prov = "Google"
            elif p_lower == "openai":
                clean_prov = "OpenAI"
            elif "eleven" in p_lower:
                clean_prov = "ElevenLabs"

            u_name, d_name = _get_identity()

            # 1. Отправка в облако Supabase
            cloud_payload = {
                "timestamp": ts,
                "user_name": u_name,
                "device_name": d_name,
                "provider": clean_prov,
                "key_alias": "",
                "model": model,
                "call_type": call_type,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
                "total_tokens": total_tok,
                "media_count": media_count,
                "duration_sec": round(duration_sec, 3),
                "cost_usd": cost,
                "status_code": status_code,
                "error_message": error_message,
                "project_source": project_source,
                "metadata_json": meta_dict,
            }
            is_synced = _push_to_supabase(cloud_payload)

            # 2. Локальная запись в SQLite
            with sqlite3.connect(str(_TRACKER_DB), timeout=5.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS api_calls (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        user_name TEXT NOT NULL DEFAULT 'unknown',
                        device_name TEXT DEFAULT '',
                        provider TEXT NOT NULL,
                        key_alias TEXT DEFAULT '',
                        model TEXT NOT NULL,
                        call_type TEXT DEFAULT 'text',
                        prompt_tokens INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        cached_tokens INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0,
                        media_count INTEGER DEFAULT 0,
                        duration_sec REAL DEFAULT 0.0,
                        cost_usd REAL DEFAULT 0.0,
                        status_code INTEGER DEFAULT 200,
                        error_message TEXT DEFAULT '',
                        project_source TEXT DEFAULT '',
                        metadata_json TEXT DEFAULT '{}',
                        synced INTEGER DEFAULT 1
                    );
                """)
                # Автомиграция для старых баз
                cols = [c[1] for c in conn.execute("PRAGMA table_info(api_calls);").fetchall()]
                if "user_name" not in cols:
                    conn.execute("ALTER TABLE api_calls ADD COLUMN user_name TEXT DEFAULT 'unknown';")
                if "device_name" not in cols:
                    conn.execute("ALTER TABLE api_calls ADD COLUMN device_name TEXT DEFAULT '';")
                if "synced" not in cols:
                    conn.execute("ALTER TABLE api_calls ADD COLUMN synced INTEGER DEFAULT 1;")

                conn.execute(
                    """
                    INSERT INTO api_calls (
                        timestamp, user_name, device_name, provider, key_alias, model, call_type,
                        prompt_tokens, completion_tokens, cached_tokens, total_tokens,
                        media_count, duration_sec, cost_usd, status_code,
                        error_message, project_source, metadata_json, synced
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts,
                        u_name,
                        d_name,
                        clean_prov,
                        "",
                        model,
                        call_type,
                        prompt_tokens,
                        completion_tokens,
                        cached_tokens,
                        total_tok,
                        media_count,
                        round(duration_sec, 3),
                        cost,
                        status_code,
                        error_message,
                        project_source,
                        meta_json,
                        1 if is_synced else 0,
                    ),
                )
        except Exception:
            pass

    threading.Thread(target=_write, daemon=True).start()


class ApiEmergencyBlockedError(RuntimeError):
    """Вызов API отклонён: активирован аварийный рубильник."""


_KILLSWITCH_CACHE: dict[str, Any] = {
    "is_blocked": False,
    "updated_by": "",
    "updated_at": "",
    "reason": "",
    "expires_at": 0.0,
}
_KILLSWITCH_LOCK = threading.Lock()


def _fetch_cloud_killswitch() -> tuple[bool, dict[str, Any]]:
    """Получить статус рубильника из облачной базы данных."""
    url = os.environ.get("SUPABASE_URL", "").strip() or "https://jubhhajwknvhlntmwpoj.supabase.co"
    key = os.environ.get("SUPABASE_KEY", "").strip() or os.environ.get("SUPABASE_ANON_KEY", "").strip() or "sb_publishable_qxcebL4M8lXRj0s2qF4ZLA_VkHhp3a-"
    if not (url and key):
        return False, {}
    try:
        import httpx
        endpoint = f"{url.rstrip('/')}/rest/v1/api_calls?call_type=eq.system&provider=eq.SYSTEM&order=timestamp.desc&limit=1"
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        with httpx.Client(timeout=4.0) as client:
            resp = client.get(endpoint, headers=headers)
            if resp.status_code == 200:
                rows = resp.json()
                if rows and isinstance(rows, list):
                    last = rows[0]
                    return True, {
                        "is_blocked": (last.get("model") == "KILLSWITCH_ACTIVATED"),
                        "updated_by": str(last.get("user_name") or ""),
                        "updated_at": str(last.get("timestamp") or ""),
                        "reason": str(last.get("error_message") or ""),
                    }
    except Exception:
        pass
    return False, {}


def is_killswitch_active(force_refresh: bool = False) -> tuple[bool, dict[str, Any]]:
    """Проверить статус аварийного рубильника (с кэшированием на 5 секунд)."""
    import time
    global _KILLSWITCH_CACHE
    now = time.time()
    if not force_refresh and now < _KILLSWITCH_CACHE.get("expires_at", 0):
        return bool(_KILLSWITCH_CACHE.get("is_blocked")), dict(_KILLSWITCH_CACHE)

    st = {"is_blocked": False, "updated_by": "", "updated_at": "", "reason": ""}
    got_cloud, cloud_st = _fetch_cloud_killswitch()
    if got_cloud:
        st = cloud_st

    # 2. Локальный SQLite fallback
    if not got_cloud and _TRACKER_DB.is_file():
        try:
            with sqlite3.connect(str(_TRACKER_DB), timeout=2.0) as conn:
                row = conn.execute(
                    "SELECT model, user_name, timestamp, error_message FROM api_calls WHERE call_type = 'system' AND provider = 'SYSTEM' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    st = {
                        "is_blocked": (row[0] == "KILLSWITCH_ACTIVATED"),
                        "updated_by": str(row[1] or ""),
                        "updated_at": str(row[2] or ""),
                        "reason": str(row[3] or ""),
                    }
        except Exception:
            pass

    with _KILLSWITCH_LOCK:
        _KILLSWITCH_CACHE = {
            **st,
            "expires_at": now + 5.0,
        }
    return bool(st["is_blocked"]), st


def check_api_allowed(provider: str = "", model: str = "") -> None:
    """Проверяет, разрешены ли сейчас вызовы API.
    
    Если включён аварийный рубильник, фиксирует отклоненный вызов (статус 403)
    и выбрасывает ApiEmergencyBlockedError.
    """
    blocked, info = is_killswitch_active()
    if blocked:
        author = info.get("updated_by") or "администратором"
        err_text = f"Вызовы API временно заблокированы ({author})."
        try:
            record_api_call(
                provider=provider or "system",
                model=model or "unknown",
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
                duration_sec=0.0,
                status_code=403,
                error_message=f"Отклонено аварийным рубильником ({author})",
                project_source="killswitch_guard",
            )
        except Exception:
            pass
        raise ApiEmergencyBlockedError(err_text)


def toggle_killswitch(
    action: str,  # "block" | "unblock"
    user_name: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Переключить аварийный рубильник (заблокировать или возобновить)."""
    import time
    global _KILLSWITCH_CACHE

    is_block = (action.lower() == "block")
    model_action = "KILLSWITCH_ACTIVATED" if is_block else "KILLSWITCH_DEACTIVATED"
    u_name = user_name.strip() if user_name else _get_identity()[0]
    d_name = _get_identity()[1]
    ts = datetime.now(timezone.utc).isoformat()
    desc = "Аварийный рубильник: API заморожены" if is_block else "Аварийный рубильник: работа API возобновлена"
    if reason:
        desc += f" ({reason})"

    payload = {
        "timestamp": ts,
        "user_name": u_name,
        "device_name": d_name,
        "provider": "SYSTEM",
        "key_alias": "",
        "model": model_action,
        "call_type": "system",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "media_count": 0,
        "duration_sec": 0.0,
        "cost_usd": 0.0,
        "status_code": 200,
        "error_message": desc,
        "project_source": "killswitch",
        "metadata_json": {"action": model_action, "reason": reason},
    }

    # 1. Запись в облако
    is_synced = _push_to_supabase(payload)

    # 2. Запись в локальный SQLite
    try:
        if _TRACKER_DB.is_file():
            with sqlite3.connect(str(_TRACKER_DB), timeout=4.0) as conn:
                conn.execute(
                    """
                    INSERT INTO api_calls (
                        timestamp, user_name, device_name, provider, key_alias, model, call_type,
                        prompt_tokens, completion_tokens, cached_tokens, total_tokens,
                        media_count, duration_sec, cost_usd, status_code,
                        error_message, project_source, metadata_json, synced
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts, u_name, d_name, "SYSTEM", "", model_action, "system",
                        0, 0, 0, 0, 0, 0.0, 0.0, 200, desc, "killswitch", "{}", 1 if is_synced else 0,
                    ),
                )
    except Exception:
        pass

    # 3. Мгновенно обновляем кэш
    with _KILLSWITCH_LOCK:
        _KILLSWITCH_CACHE = {
            "is_blocked": is_block,
            "updated_by": u_name,
            "updated_at": ts,
            "reason": desc,
            "expires_at": time.time() + 5.0,
        }

    return {
        "is_blocked": is_block,
        "updated_by": u_name,
        "updated_at": ts,
        "reason": desc,
        "synced": is_synced,
    }


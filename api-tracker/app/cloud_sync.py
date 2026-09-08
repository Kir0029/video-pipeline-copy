"""Модуль синхронизации данных API Tracker с облачной базой Supabase."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from app.config import get_supabase_key, get_supabase_url


def _get_headers() -> dict[str, str]:
    key = get_supabase_key()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def push_call_to_supabase(call_data: dict[str, Any]) -> bool:
    """Отправить запись вызова в облако Supabase (синхронно или из треда)."""
    url = get_supabase_url()
    if not url or not get_supabase_key():
        return False

    endpoint = f"{url.rstrip('/')}/rest/v1/api_calls"
    payload = {
        "timestamp": call_data.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "user_name": call_data.get("user_name") or "unknown",
        "device_name": call_data.get("device_name") or "",
        "provider": call_data.get("provider") or "other",
        "key_alias": call_data.get("key_alias") or "",
        "model": call_data.get("model") or "unknown",
        "call_type": call_data.get("call_type") or "text",
        "prompt_tokens": int(call_data.get("prompt_tokens") or 0),
        "completion_tokens": int(call_data.get("completion_tokens") or 0),
        "cached_tokens": int(call_data.get("cached_tokens") or 0),
        "total_tokens": int(call_data.get("total_tokens") or 0),
        "media_count": int(call_data.get("media_count") or 0),
        "duration_sec": float(call_data.get("duration_sec") or 0.0),
        "cost_usd": float(call_data.get("cost_usd") or 0.0),
        "status_code": int(call_data.get("status_code") or 200),
        "error_message": str(call_data.get("error_message") or ""),
        "project_source": str(call_data.get("project_source") or ""),
        "metadata_json": call_data.get("metadata") or call_data.get("metadata_json") or {},
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(endpoint, headers=_get_headers(), json=payload)
            if resp.status_code in (200, 201):
                return True
            logger.warning("Supabase push failed: HTTP {} {}", resp.status_code, resp.text[:200])
            return False
    except Exception as exc:
        logger.debug("Supabase push network error: {}", exc)
        return False


def normalize_user_name(name: str | None) -> str:
    """Нормализация имени пользователя: объединение админ/тест-алиасов в kir00."""
    n = (name or "").strip()
    if not n:
        return "kir00"
    if n.lower() in ("kir (admin)", "admin", "kir", "administrator", "администратор", "владелец", "менеджер"):
        return "kir00"
    return n


def get_cloud_distinct_users() -> list[str]:
    """Получить список всех уникальных пользователей из Supabase."""
    url = get_supabase_url()
    if not url or not get_supabase_key():
        return []

    endpoint = f"{url.rstrip('/')}/rest/v1/api_calls?call_type=neq.system&select=user_name&order=user_name.asc"
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(endpoint, headers=_get_headers())
            if resp.status_code == 200:
                rows = resp.json()
                users = sorted({normalize_user_name(r.get("user_name")) for r in rows if r.get("user_name")})
                return [u for u in users if u and u not in ("unknown", "Владелец", "Менеджер")]
    except Exception as exc:
        logger.debug("get_cloud_distinct_users error: {}", exc)
    return []


def get_cloud_logs(
    *,
    limit: int = 100,
    offset: int = 0,
    user_name: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    call_type: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Получить логи из облака Supabase с фильтрацией и общим количеством."""
    url = get_supabase_url()
    if not url or not get_supabase_key():
        return [], 0

    params: list[str] = [
        "select=*",
        "order=timestamp.desc",
        f"limit={limit}",
        f"offset={offset}",
    ]

    if user_name and user_name != "all":
        norm = normalize_user_name(user_name)
        if norm == "kir00":
            params.append("user_name=in.(kir00,Kir (Admin),Admin)")
        else:
            params.append(f"user_name=eq.{user_name}")
    if provider:
        params.append(f"provider=ilike.*{provider}*")
    if model:
        params.append(f"model=ilike.*{model}*")
    if call_type:
        params.append(f"call_type=eq.{call_type}")
    if status == "ok":
        params.append("status_code=gte.200&status_code=lt.300")
    elif status == "blocked":
        params.append("status_code=eq.403")
    elif status == "error":
        params.append("or=(status_code.gte.400,status_code.eq.0)")
    if date_from:
        params.append(f"timestamp=gte.{date_from}")
    if date_to:
        params.append(f"timestamp=lte.{date_to}")
    if search:
        s = f"*{search}*"
        params.append(f"or=(model.ilike.{s},provider.ilike.{s},user_name.ilike.{s},project_source.ilike.{s})")

    query_str = "&".join(params)
    endpoint = f"{url.rstrip('/')}/rest/v1/api_calls?{query_str}"
    headers = _get_headers()
    headers["Prefer"] = "count=exact"

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(endpoint, headers=headers)
            if resp.status_code == 200:
                rows = resp.json()
                for r in rows:
                    r["user_name"] = normalize_user_name(r.get("user_name"))
                total = len(rows)
                cr = resp.headers.get("content-range", "")
                if "/" in cr:
                    try:
                        total = int(cr.split("/")[-1])
                    except ValueError:
                        pass
                return rows, total
    except Exception as exc:
        logger.warning("get_cloud_logs error: {}", exc)
    return [], 0


def get_cloud_stats(
    *,
    user_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    """Сводные метрики из облака Supabase для дашборда."""
    url = get_supabase_url()
    if not url or not get_supabase_key():
        return {}

    # Запрашиваем поля, нужные для расчёта аналитики (исключая системные события)
    params: list[str] = [
        "select=cost_usd,total_tokens,prompt_tokens,completion_tokens,duration_sec,status_code,provider,model,user_name,timestamp,call_type",
        "call_type=neq.system",
        "order=timestamp.desc",
        "limit=5000",
    ]
    if user_name and user_name != "all":
        norm = normalize_user_name(user_name)
        if norm == "kir00":
            params.append("user_name=in.(kir00,Kir (Admin),Admin)")
        else:
            params.append(f"user_name=eq.{user_name}")
    if date_from:
        params.append(f"timestamp=gte.{date_from}")
    if date_to:
        params.append(f"timestamp=lte.{date_to}")

    query_str = "&".join(params)
    endpoint = f"{url.rstrip('/')}/rest/v1/api_calls?{query_str}"

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(endpoint, headers=_get_headers())
            if resp.status_code != 200:
                return {}
            rows = resp.json()
    except Exception as exc:
        logger.warning("get_cloud_stats error: {}", exc)
        return {}

    total_calls = len(rows)
    total_cost = sum(float(r.get("cost_usd") or 0.0) for r in rows)
    total_tokens = sum(int(r.get("total_tokens") or 0) for r in rows)
    total_errors = sum(1 for r in rows if (int(r.get("status_code") or 200) >= 400 or int(r.get("status_code") or 200) == 0))
    avg_latency = (sum(float(r.get("duration_sec") or 0.0) for r in rows) / total_calls) if total_calls > 0 else 0.0

    # Группировка по моделям
    by_model_map: dict[str, dict[str, Any]] = {}
    # Группировка по пользователям
    by_user_map: dict[str, dict[str, Any]] = {}
    # Группировка по дням
    by_day_map: dict[str, dict[str, Any]] = {}

    for r in rows:
        if str(r.get("call_type") or "") == "system" or str(r.get("provider") or "") == "SYSTEM":
            continue
        m = r.get("model") or "unknown"
        p = r.get("provider") or "unknown"
        u = normalize_user_name(r.get("user_name"))
        if u in ("Владелец", "Менеджер", "unknown"):
            continue
        c = float(r.get("cost_usd") or 0.0)
        tok = int(r.get("total_tokens") or 0)
        dur = float(r.get("duration_sec") or 0.0)
        ts_day = str(r.get("timestamp") or "")[:10]

        # Model stats
        if m not in by_model_map:
            by_model_map[m] = {"model": m, "provider": p, "calls": 0, "cost": 0.0, "tokens": 0, "total_dur": 0.0}
        by_model_map[m]["calls"] += 1
        by_model_map[m]["cost"] += c
        by_model_map[m]["tokens"] += tok
        by_model_map[m]["total_dur"] += dur

        # User stats
        if u not in by_user_map:
            by_user_map[u] = {"user_name": u, "calls": 0, "cost": 0.0, "tokens": 0}
        by_user_map[u]["calls"] += 1
        by_user_map[u]["cost"] += c
        by_user_map[u]["tokens"] += tok

        # Day stats
        if ts_day:
            if ts_day not in by_day_map:
                by_day_map[ts_day] = {"date": ts_day, "cost": 0.0, "calls": 0, "tokens": 0}
            by_day_map[ts_day]["cost"] += c
            by_day_map[ts_day]["calls"] += 1
            by_day_map[ts_day]["tokens"] += tok

    by_model = []
    for m, d in by_model_map.items():
        by_model.append({
            "model": d["model"],
            "provider": d["provider"],
            "calls": d["calls"],
            "cost": round(d["cost"], 4),
            "tokens": d["tokens"],
            "avg_latency": round(d["total_dur"] / d["calls"], 2) if d["calls"] > 0 else 0.0,
        })
    by_model.sort(key=lambda x: x["cost"], reverse=True)

    by_user = []
    for u, d in by_user_map.items():
        by_user.append({
            "user_name": d["user_name"],
            "calls": d["calls"],
            "cost": round(d["cost"], 4),
            "tokens": d["tokens"],
        })
    by_user.sort(key=lambda x: x["cost"], reverse=True)

    daily_chart = sorted(by_day_map.values(), key=lambda x: x["date"])

    return {
        "total_calls": total_calls,
        "total_cost": round(total_cost, 4),
        "total_tokens": total_tokens,
        "total_errors": total_errors,
        "avg_latency": round(avg_latency, 2),
        "summary": {
            "total_calls": total_calls,
            "total_cost": round(total_cost, 4),
            "total_tokens": total_tokens,
            "total_errors": total_errors,
            "avg_latency": round(avg_latency, 2),
        },
        "models": by_model[:20],
        "by_model": by_model[:20],
        "daily": daily_chart,
        "daily_chart": daily_chart,
        "by_user": by_user,
    }


def get_cloud_killswitch_state() -> dict[str, Any]:
    """Получить текущий статус аварийного рубильника из облака."""
    url = get_supabase_url()
    if not url or not get_supabase_key():
        return {"is_blocked": False, "updated_by": "", "updated_at": "", "reason": ""}

    endpoint = f"{url.rstrip('/')}/rest/v1/api_calls?call_type=eq.system&provider=eq.SYSTEM&order=timestamp.desc&limit=1"
    try:
        with httpx.Client(timeout=6.0) as client:
            resp = client.get(endpoint, headers=_get_headers())
            if resp.status_code == 200:
                rows = resp.json()
                if rows and isinstance(rows, list):
                    last = rows[0]
                    is_blocked = (last.get("model") == "KILLSWITCH_ACTIVATED")
                    return {
                        "is_blocked": is_blocked,
                        "updated_by": normalize_user_name(last.get("user_name")),
                        "updated_at": last.get("timestamp") or "",
                        "reason": str(last.get("error_message") or ""),
                    }
    except Exception as exc:
        logger.debug("get_cloud_killswitch_state error: {}", exc)
    return {"is_blocked": False, "updated_by": "", "updated_at": "", "reason": ""}


def push_audit_log(
    action: str,  # "KILLSWITCH_ACTIVATED" | "KILLSWITCH_DEACTIVATED"
    user_name: str,
    device_name: str = "",
    reason: str = "",
) -> bool:
    """Записать событие аудита аварийного рубильника в облако."""
    desc = "Аварийный рубильник: API заморожены" if action == "KILLSWITCH_ACTIVATED" else "Аварийный рубильник: работа API возобновлена"
    if reason:
        desc += f" ({reason})"

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_name": user_name or "Пользователь",
        "device_name": device_name or "",
        "provider": "SYSTEM",
        "model": action,
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
        "metadata_json": {"action": action, "reason": reason},
    }
    return push_call_to_supabase(payload)


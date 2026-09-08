"""База данных SQLite для хранения активности и расходов API-ключей.

Использует WAL (Write-Ahead Logging) для максимальной скорости записи
без блокировок и конфликтов.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def get_default_db_path() -> Path:
    """Определить надежный путь к tracker.db для PyInstaller .exe и для обычного скрипта."""
    env_path = os.environ.get("TRACKER_DB_PATH")
    if env_path:
        return Path(env_path).resolve()

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # Если запущено из dist/API-Tracker/API-Tracker.exe:
        # parent.parent -> api-tracker/tracker.db
        candidates = [
            exe_dir.parent.parent / "tracker.db",
            exe_dir.parent / "tracker.db",
            exe_dir / "tracker.db",
        ]
        for c in candidates:
            if c.is_file():
                return c
        if (exe_dir.parent.parent / "app").is_dir() or (exe_dir.parent.parent / "main.py").is_file():
            return exe_dir.parent.parent / "tracker.db"
        return exe_dir / "tracker.db"

    return Path(__file__).resolve().parent.parent / "tracker.db"


DEFAULT_DB_PATH = get_default_db_path()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Создать таблицы и необходимые индексы."""
    with get_connection(db_path) as conn:
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
        # Автомиграция существующих баз данных
        cols = [c["name"] for c in conn.execute("PRAGMA table_info(api_calls);").fetchall()]
        if "user_name" not in cols:
            conn.execute("ALTER TABLE api_calls ADD COLUMN user_name TEXT DEFAULT 'unknown';")
        if "device_name" not in cols:
            conn.execute("ALTER TABLE api_calls ADD COLUMN device_name TEXT DEFAULT '';")
        if "synced" not in cols:
            conn.execute("ALTER TABLE api_calls ADD COLUMN synced INTEGER DEFAULT 1;")

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_calls_ts
            ON api_calls(timestamp DESC);
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_calls_user
            ON api_calls(user_name);
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_calls_provider_model
            ON api_calls(provider, model);
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS custom_pricing (
                model TEXT PRIMARY KEY,
                pricing_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)


def insert_call(
    *,
    provider: str,
    model: str,
    cost_usd: float,
    user_name: str = "unknown",
    device_name: str = "",
    synced: int = 1,
    key_alias: str = "",
    call_type: str = "text",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    media_count: int = 0,
    duration_sec: float = 0.0,
    status_code: int = 200,
    error_message: str = "",
    project_source: str = "",
    metadata: dict[str, Any] | None = None,
    timestamp: str | None = None,
    db_path: Path | str | None = None,
) -> int:
    """Записать вызов API в базу данных."""
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    total_tok = prompt_tokens + completion_tokens
    meta_str = json.dumps(metadata or {}, ensure_ascii=False)

    with get_connection(db_path) as conn:
        cur = conn.execute(
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
                user_name,
                device_name,
                provider,
                key_alias,
                model,
                call_type,
                prompt_tokens,
                completion_tokens,
                cached_tokens,
                total_tok,
                media_count,
                round(duration_sec, 3),
                round(cost_usd, 6),
                status_code,
                error_message,
                project_source,
                meta_str,
                synced,
            ),
        )
        return cur.lastrowid or 0


def get_logs(
    *,
    limit: int = 100,
    offset: int = 0,
    user_name: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    call_type: str | None = None,
    status: str | None = None,  # "ok" / "error"
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    db_path: Path | str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Получить список логов с фильтрами и общее число совпадений."""
    query = ["SELECT * FROM api_calls WHERE 1=1"]
    count_query = ["SELECT COUNT(*) as cnt FROM api_calls WHERE 1=1"]
    params: list[Any] = []

    if user_name and user_name != "all":
        query.append("AND user_name = ?")
        count_query.append("AND user_name = ?")
        params.append(user_name)

    if provider:
        query.append("AND LOWER(provider) = LOWER(?)")
        count_query.append("AND LOWER(provider) = LOWER(?)")
        params.append(provider)

    if model:
        query.append("AND LOWER(model) LIKE LOWER(?)")
        count_query.append("AND LOWER(model) LIKE LOWER(?)")
        params.append(f"%{model}%")

    if call_type:
        query.append("AND LOWER(call_type) = LOWER(?)")
        count_query.append("AND LOWER(call_type) = LOWER(?)")
        params.append(call_type)

    if status == "ok":
        query.append("AND status_code >= 200 AND status_code < 300")
        count_query.append("AND status_code >= 200 AND status_code < 300")
    elif status == "blocked":
        query.append("AND status_code = 403")
        count_query.append("AND status_code = 403")
    elif status == "error":
        query.append("AND (status_code >= 400 OR status_code = 0)")
        count_query.append("AND (status_code >= 400 OR status_code = 0)")

    if date_from:
        query.append("AND timestamp >= ?")
        count_query.append("AND timestamp >= ?")
        params.append(date_from)

    if date_to:
        query.append("AND timestamp <= ?")
        count_query.append("AND timestamp <= ?")
        params.append(date_to)

    if search:
        s = f"%{search}%"
        query.append("AND (model LIKE ? OR provider LIKE ? OR user_name LIKE ? OR error_message LIKE ? OR project_source LIKE ?)")
        count_query.append("AND (model LIKE ? OR provider LIKE ? OR user_name LIKE ? OR error_message LIKE ? OR project_source LIKE ?)")
        params.extend([s, s, s, s, s])

    # Подсчёт общего числа записей
    with get_connection(db_path) as conn:
        total_count = conn.execute(" ".join(count_query), params).fetchone()["cnt"]

        query.append("ORDER BY id DESC LIMIT ? OFFSET ?")
        rows = conn.execute(" ".join(query), params + [limit, offset]).fetchall()

    return [dict(r) for r in rows], total_count


def get_stats(
    *,
    user_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Сводные метрики и данные для графиков дашборда."""
    where = ["WHERE 1=1 AND call_type != 'system' AND user_name NOT IN ('Владелец', 'Менеджер')"]
    params: list[Any] = []

    if user_name and user_name != "all":
        where.append("AND user_name = ?")
        params.append(user_name)
    if date_from:
        where.append("AND timestamp >= ?")
        params.append(date_from)
    if date_to:
        where.append("AND timestamp <= ?")
        params.append(date_to)

    where_clause = " ".join(where)

    with get_connection(db_path) as conn:
        # 1. Общие метрики
        summary = conn.execute(
            f"""
            SELECT
                COUNT(*) as total_calls,
                COALESCE(SUM(cost_usd), 0.0) as total_cost,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(SUM(CASE WHEN status_code >= 400 OR status_code = 0 THEN 1 ELSE 0 END), 0) as total_errors,
                COALESCE(AVG(duration_sec), 0.0) as avg_latency
            FROM api_calls
            {where_clause}
            """,
            params,
        ).fetchone()

        # 2. Топ моделей по расходам
        models = conn.execute(
            f"""
            SELECT
                model,
                MAX(provider) as provider,
                COUNT(*) as calls_count,
                COALESCE(SUM(cost_usd), 0.0) as cost_usd,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(AVG(duration_sec), 0.0) as avg_latency
            FROM api_calls
            {where_clause}
            GROUP BY model
            ORDER BY cost_usd DESC
            LIMIT 10
            """,
            params,
        ).fetchall()

        # 3. Расходы по дням / часам (для Area Chart)
        day_count_row = conn.execute(
            f"""
            SELECT COUNT(DISTINCT SUBSTR(timestamp, 1, 10)) as day_cnt
            FROM api_calls
            {where_clause}
            """,
            params,
        ).fetchone()
        distinct_days = day_count_row["day_cnt"] if day_count_row else 0

        # Если данные в пределах 1 дня (например, сегодня), группируем по часам (МСК, UTC+3)
        if distinct_days <= 1:
            group_expr = "strftime('%H:00', timestamp, '+3 hours')"
            time_unit = "hour"
        else:
            group_expr = "SUBSTR(timestamp, 1, 10)"
            time_unit = "day"

        daily = conn.execute(
            f"""
            SELECT
                {group_expr} as day,
                COUNT(*) as calls_count,
                COALESCE(SUM(cost_usd), 0.0) as cost_usd,
                COALESCE(SUM(total_tokens), 0) as total_tokens
            FROM api_calls
            {where_clause}
            GROUP BY {group_expr}
            ORDER BY day ASC
            LIMIT 48
            """,
            params,
        ).fetchall()

        # 4. Расходы по провайдерам (Donut Chart)
        providers = conn.execute(
            f"""
            SELECT
                provider,
                COUNT(*) as calls_count,
                COALESCE(SUM(cost_usd), 0.0) as cost_usd
            FROM api_calls
            {where_clause}
            GROUP BY provider
            ORDER BY cost_usd DESC
            """,
            params,
        ).fetchall()

    total_cost = float(summary["total_cost"])
    models_list = []
    for m in models:
        c = float(m["cost_usd"])
        pct = round((c / total_cost * 100.0), 1) if total_cost > 0 else 0.0
        models_list.append({
            "model": m["model"],
            "provider": m["provider"],
            "calls_count": m["calls_count"],
            "cost_usd": round(c, 4),
            "total_tokens": m["total_tokens"],
            "avg_latency": round(float(m["avg_latency"]), 2),
            "percentage": pct,
        })

    return {
        "total_cost": round(total_cost, 4),
        "total_calls": summary["total_calls"],
        "total_tokens": summary["total_tokens"],
        "total_errors": summary["total_errors"],
        "avg_latency": round(float(summary["avg_latency"]), 2),
        "time_unit": time_unit,
        "models": models_list,
        "daily": [
            {
                "day": d["day"],
                "calls_count": d["calls_count"],
                "cost_usd": round(float(d["cost_usd"]), 4),
                "total_tokens": d["total_tokens"],
            }
            for d in daily
        ],
        "providers": [
            {
                "provider": p["provider"],
                "calls_count": p["calls_count"],
                "cost_usd": round(float(p["cost_usd"]), 4),
            }
            for p in providers
        ],
    }


def get_unsynced_calls(limit: int = 100, db_path: Path | str | None = None) -> list[dict[str, Any]]:
    """Получить локальные записи, которые еще не были отправлены в Supabase."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM api_calls WHERE synced = 0 ORDER BY id ASC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def mark_calls_synced(call_ids: list[int], db_path: Path | str | None = None) -> None:
    """Пометить записи как успешно отправленные в Supabase."""
    if not call_ids:
        return
    with get_connection(db_path) as conn:
        placeholders = ",".join("?" for _ in call_ids)
        conn.execute(f"UPDATE api_calls SET synced = 1 WHERE id IN ({placeholders})", call_ids)


def get_local_distinct_users(db_path: Path | str | None = None) -> list[str]:
    """Список уникальных пользователей из локальной базы."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT user_name FROM api_calls WHERE user_name != '' AND user_name != 'unknown' AND call_type != 'system' AND user_name NOT IN ('Владелец', 'Менеджер') ORDER BY user_name ASC"
        ).fetchall()
        return [r["user_name"] for r in rows]


def get_local_killswitch_state(db_path: Path | str | None = None) -> dict[str, Any]:
    """Получить статус рубильника из локальной базы данных."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM api_calls WHERE call_type = 'system' AND provider = 'SYSTEM' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row:
            r = dict(row)
            return {
                "is_blocked": r.get("model") == "KILLSWITCH_ACTIVATED",
                "updated_by": r.get("user_name") or "",
                "updated_at": r.get("timestamp") or "",
                "reason": r.get("error_message") or "",
            }
    return {"is_blocked": False, "updated_by": "", "updated_at": "", "reason": ""}



"""Клиентская библиотека для интеграции API Tracker в любые проекты.

Работает в двух режимах:
1. Прямая запись в локальный SQLite tracker.db (быстро, надежно, работает даже если UI закрыт).
2. Опциональная отправка HTTP POST на http://localhost:8900/api/log (если запущен сервер).
Никогда не выбрасывает исключений и не ломает основной пайплайн!
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

# Путь к базе данных по умолчанию
TRACKER_DB_PATH = Path(__file__).resolve().parent / "tracker.db"


def track_api_call(
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
    key_alias: str = "",
    metadata: dict[str, Any] | None = None,
    db_path: Path | str | None = None,
) -> None:
    """Записать вызов API асинхронно в фоновом потоке.
    
    Не блокирует основной поток генерации и не вызывает ошибок при сбоях.
    """
    def _worker():
        try:
            from app.db import init_db, insert_call
            from app.pricing import calculate_cost

            target_db = Path(db_path or TRACKER_DB_PATH)
            init_db(target_db)

            cost = cost_usd
            if cost is None:
                cost = calculate_cost(
                    model,
                    call_type=call_type,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_tokens=cached_tokens,
                    media_count=media_count,
                    duration_sec=duration_sec,
                    chars=int((metadata or {}).get("chars", 0)),
                )

            insert_call(
                provider=provider,
                model=model,
                key_alias=key_alias,
                call_type=call_type,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                media_count=media_count,
                duration_sec=duration_sec,
                cost_usd=cost,
                status_code=status_code,
                error_message=error_message,
                project_source=project_source,
                metadata=metadata or {},
                db_path=target_db,
            )
        except Exception:
            # Безопасное подавление ошибок, чтобы никогда не ронять вызывающий процесс
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

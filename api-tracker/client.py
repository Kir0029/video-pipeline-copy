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
    user_name: str | None = None,
    device_name: str | None = None,
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
    Автоматически сохраняет локально в SQLite и отправляет в облако Supabase.
    """
    def _worker():
        try:
            from app.cloud_sync import push_call_to_supabase
            from app.db import get_unsynced_calls, init_db, insert_call, mark_calls_synced
            from app.identity import get_user_identity
            from app.pricing import calculate_cost

            target_db = Path(db_path or TRACKER_DB_PATH)
            init_db(target_db)

            # Определение пользователя
            u_name, d_name, _ = get_user_identity()
            active_user = (user_name or "").strip() or u_name
            active_device = (device_name or "").strip() or d_name

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

            call_payload = {
                "user_name": active_user,
                "device_name": active_device,
                "provider": provider,
                "model": model,
                "key_alias": key_alias,
                "call_type": call_type,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
                "media_count": media_count,
                "duration_sec": duration_sec,
                "cost_usd": cost,
                "status_code": status_code,
                "error_message": error_message,
                "project_source": project_source,
                "metadata": metadata or {},
            }

            # 1. Мгновенная запись в локальный SQLite (0.5 мс, Local-First)
            call_id = insert_call(
                provider=provider,
                model=model,
                user_name=active_user,
                device_name=active_device,
                synced=0,
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

            # 2. Асинхронная отправка в облако Supabase
            is_synced = False
            try:
                is_synced = push_call_to_supabase(call_payload)
            except Exception:
                pass

            if is_synced:
                mark_calls_synced([call_id], db_path=target_db)

            # 3. Если сеть работает, пробуем дослать накопившиеся несинхронизированные записи
            if is_synced:
                pending = get_unsynced_calls(limit=20, db_path=target_db)
                synced_ids = []
                for p_call in pending:
                    if p_call["id"] == call_id:
                        continue
                    if push_call_to_supabase(p_call):
                        synced_ids.append(p_call["id"])
                    else:
                        break
                if synced_ids:
                    mark_calls_synced(synced_ids, db_path=target_db)

        except Exception:
            # Безопасное подавление ошибок, чтобы никогда не ронять вызывающий процесс
            pass

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

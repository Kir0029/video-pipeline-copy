"""Тесты аварийного рубильника (Kill Switch) в пайплайне."""

from __future__ import annotations

import pytest

from app.services.api_tracker_hook import (
    ApiEmergencyBlockedError,
    check_api_allowed,
    is_killswitch_active,
    toggle_killswitch,
)


def test_killswitch_lifecycle(monkeypatch: pytest.MonkeyPatch):
    """Проверка полного цикла аварийного рубильника: нормальный режим -> блокировка -> разблокировка."""
    # Состояние для имитации облака
    cloud_state = {"is_blocked": False, "updated_by": "Админ", "updated_at": "2026-09-08T12:00:00Z", "reason": ""}

    def mock_push(payload):
        is_blk = (payload.get("model") == "KILLSWITCH_ACTIVATED")
        cloud_state["is_blocked"] = is_blk
        cloud_state["updated_by"] = payload.get("user_name", "")
        cloud_state["reason"] = payload.get("error_message", "")
        return True

    def mock_fetch():
        return True, dict(cloud_state)

    monkeypatch.setattr("app.services.api_tracker_hook._push_to_supabase", mock_push)
    monkeypatch.setattr("app.services.api_tracker_hook._fetch_cloud_killswitch", mock_fetch)

    # 1. Принудительно разблокируем перед тестом
    toggle_killswitch("unblock", user_name="Админ", reason="Инициализация теста")
    blocked, info = is_killswitch_active(force_refresh=True)
    assert not blocked

    # 2. pre-flight проверка проходит без исключений
    check_api_allowed(provider="openai", model="gpt-4o")

    # 3. Активируем аварийный рубильник (блокировка)
    res_block = toggle_killswitch("block", user_name="Владелец", reason="Перерасход бюджета")
    assert res_block["is_blocked"] is True
    assert res_block["updated_by"] == "Владелец"

    blocked, info = is_killswitch_active(force_refresh=True)
    assert blocked is True
    assert info["updated_by"] == "Владелец"

    # 4. pre-flight проверка теперь должна выбрасывать ApiEmergencyBlockedError
    with pytest.raises(ApiEmergencyBlockedError) as exc_info:
        check_api_allowed(provider="kling", model="kling-v1-standard")
    assert "Владелец" in str(exc_info.value) or "заблокированы" in str(exc_info.value)

    # 5. Деактивируем аварийный рубильник (возобновление работы)
    res_unblock = toggle_killswitch("unblock", user_name="Владелец", reason="Бюджет пополнен")
    assert res_unblock["is_blocked"] is False

    blocked, info = is_killswitch_active(force_refresh=True)
    assert not blocked

    # 6. pre-flight снова выполняется без ошибок
    check_api_allowed(provider="elevenlabs", model="eleven_multilingual_v2")

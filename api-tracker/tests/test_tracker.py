"""Комплексные тесты для системы API Tracker."""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

# Добавляем пути
TRACKER_DIR = Path(__file__).resolve().parent.parent
if str(TRACKER_DIR) not in sys.path:
    sys.path.insert(0, str(TRACKER_DIR))

from app.db import get_logs, get_stats, init_db, insert_call
from app.pricing import calculate_cost, normalize_model_name
from app.server import app
from client import track_api_call


# ─── 1. ТЕСТЫ РАСЧЁТА СТОИМОСТИ (PRICING) ──────────────────────────────

def test_pricing_gemini_37_flash():
    """Проверка тарифа Gemini 3.7 Flash."""
    # 1_000_000 in = $0.147622, 1_000_000 out = $0.738108
    cost = calculate_cost(
        "gemini-3.7-flash",
        prompt_tokens=1000,
        completion_tokens=2000,
    )
    expected = (1000 / 1e6) * 0.147622 + (2000 / 1e6) * 0.738108
    assert cost == round(expected, 6)
    assert cost > 0


def test_pricing_gemini_38_flash():
    """Проверка тарифа Gemini 3.8 Flash (официальный $0.75 / $3.75)."""
    cost = calculate_cost(
        "gemini-3.8-flash",
        prompt_tokens=10000,
        completion_tokens=5000,
    )
    expected = (10000 / 1e6) * 0.75 + (5000 / 1e6) * 3.75
    assert cost == round(expected, 6)


def test_pricing_gpt_sol():
    """Проверка тарифа GPT 5.6 Sol ($1.20 / $4.80)."""
    cost = calculate_cost(
        "gpt-5.6-sol",
        prompt_tokens=5000,
        completion_tokens=10000,
    )
    expected = (5000 / 1e6) * 1.20 + (10000 / 1e6) * 4.80
    assert cost == round(expected, 6)


def test_pricing_video_and_image():
    """Проверка расчёта медиа: видео (Kling) и изображений (Flux)."""
    # Видео по секундам (5 сек Kling 3.0 по $0.024/сек)
    v_cost = calculate_cost("kling-3-0", call_type="video", duration_sec=5.0)
    assert v_cost == round(5.0 * 0.024, 6)

    # Картинка Flux 2 Pro (2 шт по $0.05)
    img_cost = calculate_cost("flux-2-pro", call_type="image", media_count=2)
    assert img_cost == 0.10


def test_pricing_audio_tts():
    """Проверка озвучки ElevenLabs (1000 символов = $0.30)."""
    audio_cost = calculate_cost("elevenlabs", call_type="audio", chars=500)
    assert audio_cost == 0.15


def test_pricing_unknown_model_fallback():
    """Неизвестная модель не роняет расчет, а использует разумный дефолт."""
    cost = calculate_cost("unknown-model-xyz", prompt_tokens=1000, completion_tokens=1000)
    assert cost > 0


# ─── 2. ТЕСТЫ БАЗЫ ДАННЫХ (SQLITE WAL) ──────────────────────────────────

def test_db_init_and_insert(tmp_path: Path):
    db_file = tmp_path / "test_tracker.db"
    init_db(db_file)

    row_id = insert_call(
        provider="vibecode",
        model="gemini-3.7-flash",
        cost_usd=0.0025,
        prompt_tokens=500,
        completion_tokens=800,
        duration_sec=1.35,
        status_code=200,
        db_path=db_file,
    )
    assert row_id == 1

    logs, total = get_logs(db_path=db_file)
    assert total == 1
    assert logs[0]["model"] == "gemini-3.7-flash"
    assert logs[0]["cost_usd"] == 0.0025
    assert logs[0]["total_tokens"] == 1300


def test_db_fast_concurrent_writes(tmp_path: Path):
    """Проверка скорости записи 100 записей без блокировок."""
    db_file = tmp_path / "test_tracker.db"
    init_db(db_file)

    t0 = time.monotonic()
    for i in range(100):
        insert_call(
            provider="vibecode" if i % 2 == 0 else "kie",
            model="gpt-5.6-sol",
            cost_usd=0.001 * (i + 1),
            prompt_tokens=100,
            completion_tokens=200,
            status_code=200 if i != 13 else 429,
            error_message="rate limit" if i == 13 else "",
            db_path=db_file,
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0  # Должно быть существенно быстрее 1 секунды

    logs, total = get_logs(db_path=db_file)
    assert total == 100

    # Проверка фильтрации по ошибкам
    err_logs, err_total = get_logs(status="error", db_path=db_file)
    assert err_total == 1
    assert err_logs[0]["status_code"] == 429


def test_db_get_stats_aggregation(tmp_path: Path):
    db_file = tmp_path / "test_tracker.db"
    init_db(db_file)

    insert_call(provider="google", model="gemini-3.7-flash", cost_usd=0.10, prompt_tokens=1000, completion_tokens=1000, duration_sec=1.0, db_path=db_file)
    insert_call(provider="openai", model="gpt-5.6-sol", cost_usd=0.20, prompt_tokens=2000, completion_tokens=2000, duration_sec=2.0, db_path=db_file)
    insert_call(provider="google", model="gemini-3.7-flash", cost_usd=0.05, prompt_tokens=500, completion_tokens=500, duration_sec=0.5, db_path=db_file)

    stats = get_stats(db_path=db_file)
    assert stats["total_calls"] == 3
    assert stats["total_cost"] == 0.35
    assert stats["total_tokens"] == 7000
    assert stats["total_errors"] == 0
    assert stats["avg_latency"] == round((1.0 + 2.0 + 0.5) / 3, 2)
    assert len(stats["models"]) == 2
    assert stats["models"][0]["model"] == "gpt-5.6-sol"  # Наибольший расход (0.20)
    assert stats["models"][1]["model"] == "gemini-3.7-flash"


# ─── 3. ТЕСТЫ FASTAPI ЭНДПОИНТОВ ────────────────────────────────────────

def test_api_endpoints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_file = tmp_path / "server_test.db"
    monkeypatch.setattr("app.db.DEFAULT_DB_PATH", db_file)
    init_db(db_file)

    client = TestClient(app)

    # 1. POST /api/log
    res = client.post("/api/log", json={
        "provider": "vibecode",
        "model": "gemini-3.7-flash",
        "call_type": "text",
        "prompt_tokens": 500,
        "completion_tokens": 1000,
        "duration_sec": 0.85,
        "status_code": 200,
        "project_source": "chat",
    })
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["cost_usd"] > 0

    # 2. GET /api/stats
    res = client.get("/api/stats")
    assert res.status_code == 200
    s_data = res.json()
    assert s_data["total_calls"] == 1
    assert s_data["total_cost"] > 0

    # 3. GET /api/logs
    res = client.get("/api/logs")
    assert res.status_code == 200
    l_data = res.json()
    assert l_data["total"] == 1
    assert l_data["items"][0]["model"] == "gemini-3.7-flash"

    # 4. GET /api/pricing
    res = client.get("/api/pricing")
    assert res.status_code == 200
    assert "gemini-3.7-flash" in res.json()

    # 5. GET /api/export/csv
    res = client.get("/api/export/csv")
    assert res.status_code == 200
    assert "gemini-3.7-flash" in res.text

    # 6. GET /api/export/excel
    res = client.get("/api/export/excel")
    assert res.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(res.content))
    ws = wb.active
    assert ws.title == "Активность API"
    assert ws.cell(row=2, column=6).value == "gemini-3.7-flash"


# ─── 4. ТЕСТ КЛИЕНТА (CLIENT TRACK_API_CALL) ───────────────────────────

def test_client_track_call_thread(tmp_path: Path):
    db_file = tmp_path / "client_test.db"
    init_db(db_file)

    track_api_call(
        provider="google",
        model="gemini-3.7-flash",
        prompt_tokens=1200,
        completion_tokens=800,
        duration_sec=1.1,
        db_path=db_file,
    )

    # Даем фоновому потоку завершить запись
    time.sleep(0.15)

    logs, total = get_logs(db_path=db_file)
    assert total == 1
    assert logs[0]["model"] == "gemini-3.7-flash"
    assert logs[0]["total_tokens"] == 2000


# ─── 5. ТЕСТЫ ИДЕНТИФИКАЦИИ И ОБЛАЧНОЙ ФИЛЬТРАЦИИ ───────────────────────

def test_user_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from app.identity import get_user_identity, load_user_profile, save_user_profile
    monkeypatch.setattr("app.identity.get_appdata_dir", lambda: tmp_path)
    monkeypatch.setattr("app.identity.BASE_DIR", tmp_path)

    # До сохранения профиля
    u, d, is_cfg = get_user_identity()
    assert len(u) > 0
    assert not is_cfg

    # Сохраняем имя
    res = save_user_profile("Алина (Тестировщик)")
    assert res["user_name"] == "Алина (Тестировщик)"
    assert res["is_configured"] is True

    # Проверяем повторное чтение
    u2, d2, is_cfg2 = get_user_identity()
    assert u2 == "Алина (Тестировщик)"
    assert is_cfg2 is True


def test_db_user_filter(tmp_path: Path):
    db_file = tmp_path / "user_filter_test.db"
    init_db(db_file)

    insert_call(provider="OpenAI", model="gpt-4o", cost_usd=0.05, user_name="Алина", db_path=db_file)
    insert_call(provider="Google", model="gemini-3.7-flash", cost_usd=0.01, user_name="Лера", db_path=db_file)
    insert_call(provider="Kie.ai", model="Flux 2 Pro", cost_usd=0.025, user_name="Алина", db_path=db_file)

    # 1. Логи по Алине
    logs_alina, cnt_alina = get_logs(user_name="Алина", db_path=db_file)
    assert cnt_alina == 2
    assert all(l["user_name"] == "Алина" for l in logs_alina)

    # 2. Логи по Лере
    logs_lera, cnt_lera = get_logs(user_name="Лера", db_path=db_file)
    assert cnt_lera == 1
    assert logs_lera[0]["model"] == "gemini-3.7-flash"

    # 3. Статистика по пользователю
    stats_alina = get_stats(user_name="Алина", db_path=db_file)
    assert stats_alina["total_calls"] == 2
    assert stats_alina["total_cost"] == 0.075


def test_api_profile_and_users(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    db_file = tmp_path / "server_user_test.db"
    monkeypatch.setattr("app.db.DEFAULT_DB_PATH", db_file)
    monkeypatch.setattr("app.identity.get_appdata_dir", lambda: tmp_path)
    monkeypatch.setattr("app.identity.BASE_DIR", tmp_path)
    init_db(db_file)

    insert_call(provider="OpenAI", model="gpt-4o", cost_usd=0.01, user_name="Владелец", db_path=db_file)

    client = TestClient(app)

    # Сохранение имени через API
    post_res = client.post("/api/user-profile", json={"user_name": "Тестировщик-1"})
    assert post_res.status_code == 200
    assert post_res.json()["user_name"] == "Тестировщик-1"

    # Чтение профиля
    get_res = client.get("/api/user-profile")
    assert get_res.status_code == 200
    assert get_res.json()["user_name"] == "Тестировщик-1"

    # Список пользователей
    users_res = client.get("/api/users")
    assert users_res.status_code == 200
    users = users_res.json()["users"]
    assert "Владелец" in users or len(users) > 0


def test_killswitch_api_toggle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Проверка работы API рубильника: блокировка, разблокировка и журнал аудита."""
    db_file = tmp_path / "killswitch_test.db"
    monkeypatch.setattr("app.db.DEFAULT_DB_PATH", db_file)
    monkeypatch.setattr("app.identity.get_appdata_dir", lambda: tmp_path)
    monkeypatch.setattr("app.identity.BASE_DIR", tmp_path)
    init_db(db_file)

    monkeypatch.setattr("app.cloud_sync.get_cloud_killswitch_state", lambda: {})
    monkeypatch.setattr("app.cloud_sync.push_audit_log", lambda **kw: True)

    client = TestClient(app)

    # 1. По умолчанию API не заблокированы
    res = client.get("/api/system/killswitch")
    assert res.status_code == 200
    assert res.json()["is_blocked"] is False

    # 2. Активируем рубильник (блокировка)
    post_block = client.post(
        "/api/system/killswitch",
        json={"action": "block", "user_name": "Владелец", "reason": "Экстренная остановка"},
    )
    assert post_block.status_code == 200
    assert post_block.json()["is_blocked"] is True
    assert post_block.json()["updated_by"] == "Владелец"

    # 3. Проверяем статус после блокировки
    st_res = client.get("/api/system/killswitch")
    assert st_res.status_code == 200
    assert st_res.json()["is_blocked"] is True

    # 4. Проверяем наличие записи аудита в базе
    logs, cnt = get_logs(call_type="system", db_path=db_file)
    assert cnt == 1
    assert logs[0]["model"] == "KILLSWITCH_ACTIVATED"
    assert logs[0]["provider"] == "SYSTEM"
    assert logs[0]["user_name"] == "Владелец"

    # 5. Деактивируем рубильник (разблокировка)
    post_unblock = client.post(
        "/api/system/killswitch",
        json={"action": "unblock", "user_name": "Менеджер", "reason": "Возобновление работы"},
    )
    assert post_unblock.status_code == 200
    assert post_unblock.json()["is_blocked"] is False
    assert post_unblock.json()["updated_by"] == "Менеджер"

    # 6. Проверяем статус после разблокировки
    st_unblock = client.get("/api/system/killswitch")
    assert st_unblock.status_code == 200
    assert st_unblock.json()["is_blocked"] is False

    logs2, cnt2 = get_logs(call_type="system", db_path=db_file)
    assert cnt2 == 2
    assert logs2[0]["model"] == "KILLSWITCH_DEACTIVATED"



"""Управление идентификацией пользователя (логин, имя устройства, профиль)."""

from __future__ import annotations

import getpass
import json
import os
import platform
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import BASE_DIR, get_appdata_dir

_PROFILE_FILENAME = "user_profile.json"


def _get_profile_paths() -> list[Path]:
    """Список путей для поиска/сохранения user_profile.json."""
    return [
        get_appdata_dir() / _PROFILE_FILENAME,
        BASE_DIR / _PROFILE_FILENAME,
    ]


def detect_fallback_username() -> str:
    """Автоопределение системного имени пользователя Windows/OS."""
    try:
        return os.environ.get("USERNAME") or os.environ.get("USER") or getpass.getuser() or "Пользователь"
    except Exception:
        return "Пользователь"


def detect_device_name() -> str:
    """Имя компьютера / хоста."""
    try:
        host = socket.gethostname()
        system = platform.system()
        return f"{host} ({system})" if host else system
    except Exception:
        return "Desktop"


def load_user_profile() -> dict[str, Any]:
    """Загрузить профиль пользователя из файла."""
    for p in _get_profile_paths():
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("user_name"):
                    return data
            except Exception:
                pass

    # Если профиль ещё не создан
    fallback_name = detect_fallback_username()
    device = detect_device_name()
    return {
        "user_name": fallback_name,
        "device_name": device,
        "is_configured": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def save_user_profile(user_name: str, device_name: str | None = None) -> dict[str, Any]:
    """Сохранить подтверждённое имя пользователя."""
    clean_name = user_name.strip()
    if not clean_name:
        raise ValueError("Имя пользователя не может быть пустым")

    dev = (device_name or "").strip() or detect_device_name()
    profile_data = {
        "user_name": clean_name,
        "device_name": dev,
        "is_configured": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    raw = json.dumps(profile_data, ensure_ascii=False, indent=2)
    for p in _get_profile_paths():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(raw, encoding="utf-8")
        except Exception:
            pass

    return profile_data


def get_user_identity() -> tuple[str, str, bool]:
    """Получить текущие (user_name, device_name, is_configured)."""
    prof = load_user_profile()
    return (
        prof.get("user_name") or detect_fallback_username(),
        prof.get("device_name") or detect_device_name(),
        bool(prof.get("is_configured", False)),
    )

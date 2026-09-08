"""Скрипт сборки автономного .exe файла приложения API Tracker с помощью PyInstaller."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def build():
    print("=== Сборка API Tracker в .exe ===")
    
    # Проверка наличия pyinstaller
    try:
        import PyInstaller
    except ImportError:
        print("Установка PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    static_dir = BASE_DIR / "app" / "static"
    # Разделитель данных для PyInstaller на Windows — точка с запятой ';'
    add_data = f"{static_dir};app/static"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--name",
        "API-Tracker",
        "--add-data",
        add_data,
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols",
        "--hidden-import",
        "uvicorn.protocols.http",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.http.h11_impl",
        "--hidden-import",
        "uvicorn.lifespan.on",
        "--hidden-import",
        "uvicorn.lifespan.off",
        "--hidden-import",
        "httpx",
        "--hidden-import",
        "httpcore",
        str(BASE_DIR / "main.py"),
    ]

    print("Запуск команды:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(BASE_DIR))
    
    dist_path = BASE_DIR / "dist" / "API-Tracker" / "API-Tracker.exe"
    print(f"\n[OK] Сборка успешно завершена!")
    print(f"Исполняемый файл: {dist_path}")


if __name__ == "__main__":
    build()

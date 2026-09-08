"""Главный файл запуска приложения API Tracker (Десктопное окно WebView2)."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

# Безопасные дескрипторы для оконного режима PyInstaller (когда нет консоли)
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# Добавляем корень api-tracker в sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import uvicorn
import uvicorn.lifespan.on
import uvicorn.loops.asyncio
import uvicorn.loops.auto
import uvicorn.protocols.http.auto
import uvicorn.protocols.http.h11_impl

from app.db import init_db
from app.server import app


def find_free_port(start_port: int = 8900) -> int:
    """Найти свободный порт для локального сервера."""
    for p in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start_port


def run_server(port: int, stop_event: threading.Event):
    """Запустить Uvicorn сервер в фоновом потоке."""
    try:
        config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=port,
            loop="asyncio",
            http="h11",
            lifespan="on",
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)

        def _wait_stop():
            stop_event.wait()
            server.should_exit = True

        t = threading.Thread(target=_wait_stop, daemon=True)
        t.start()
        server.run()
    except Exception as e:
        err_file = BASE_DIR / "server_error.log"
        err_file.write_text(f"Server start error: {e}", encoding="utf-8")


def wait_for_server(port: int, timeout: float = 10.0) -> bool:
    """Дождаться готовности HTTP-сервера перед открытием окна WebView."""
    start_t = time.time()
    while time.time() - start_t < timeout:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


def main():
    init_db()
    port = find_free_port(8900)
    stop_event = threading.Event()

    server_thread = threading.Thread(
        target=run_server,
        args=(port, stop_event),
        daemon=True,
    )
    server_thread.start()

    # Дожидаемся готовности сервера, исключая ошибку ERR_CONNECTION_REFUSED в WebView
    if not wait_for_server(port, timeout=10.0):
        print(f"Внимание: сервер на порту {port} инициализируется дольше обычного")
    else:
        time.sleep(0.1)

    url = f"http://127.0.0.1:{port}"
    print(f"API Tracker запущен на {url}")

    # Пытаемся открыть в нативном десктопном окне через pywebview
    has_webview = False
    try:
        import webview

        # Разрешаем скачивание файлов в WebView2
        webview.settings["ALLOW_DOWNLOADS"] = True

        has_webview = True
        window = webview.create_window(
            title="API Tracker & Cost Monitor",
            url=url,
            width=1080,
            height=720,
            min_size=(800, 560),
            background_color="#0a0a0c",
        )
        webview.start(debug=False)
    except Exception as e:
        print(f"Запуск через системный браузер (pywebview: {e})")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()

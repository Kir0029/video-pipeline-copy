@echo off
title API Tracker & Cost Monitor
chcp 65001 >nul
cd /d "%~dp0"

if exist "dist\API-Tracker\API-Tracker.exe" (
    start "" "dist\API-Tracker\API-Tracker.exe"
) else if exist "..\.venv\Scripts\python.exe" (
    start "" "..\.venv\Scripts\python.exe" main.py
) else (
    start "" python main.py
)
exit /b 0

@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Instale primeiro: python -m venv .venv
    echo Depois: .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
".venv\Scripts\python.exe" install_ufo.py
if errorlevel 1 (
    pause
    exit /b 1
)
".venv\Scripts\python.exe" app\main.py
if errorlevel 1 pause

@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" benchmarks\runner.py %*
if errorlevel 1 (
    pause
    exit /b 1
)
".venv\Scripts\python.exe" benchmark_report.py
pause

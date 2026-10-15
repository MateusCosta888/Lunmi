@echo off
setlocal
cd /d "%~dp0"
if /I "%~1"=="--tk" (
    call "%~dp0run-tk.bat"
    exit /b
)
if not exist ".venv\Scripts\python.exe" (
    echo Instale primeiro o ambiente Python conforme README.md.
    exit /b 1
)
".venv\Scripts\python.exe" install_ufo.py
if errorlevel 1 exit /b 1
if not exist "desktop\node_modules\electron\dist\electron.exe" (
    echo Prepare o desktop conforme README.md: npm ci e npm run build na pasta desktop.
    echo Alternativa legada: run.bat --tk
    exit /b 1
)
if not exist "desktop\dist\index.html" (
    echo Execute npm run build na pasta desktop antes de abrir.
    exit /b 1
)
if not exist "desktop\dist-electron\electron\main.js" (
    echo Execute npm run build na pasta desktop antes de abrir.
    exit /b 1
)
"desktop\node_modules\electron\dist\electron.exe" "%~dp0desktop\dist-electron\electron\main.js" %*
exit /b %ERRORLEVEL%

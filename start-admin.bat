@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Python environment is missing. See LOCAL_SETUP.md.
    pause
    exit /b 1
)
set PYTHONUTF8=1
"%~dp0.venv\Scripts\python.exe" "%~dp0admin_start.py"
if errorlevel 1 (
    pause
    exit /b 1
)

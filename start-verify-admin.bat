@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
rem Verification only defaults the frequency to blank; the UI can select 0 or minutes.
if not exist ".venv\Scripts\python.exe" (
    echo Python environment is missing. See LOCAL_SETUP.md.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0admin_start.py" --scan-once
if errorlevel 1 pause

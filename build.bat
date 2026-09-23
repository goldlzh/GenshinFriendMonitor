@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo 原神好友状态监测器 - Windows 打包
if not exist ".venv\Scripts\python.exe" (
    echo 请先按照 README.md 创建 .venv 并安装依赖。
    exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt "pyinstaller>=6.16,<7"
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean build.spec
if errorlevel 1 exit /b 1
echo 完成：dist\GenshinFriendMonitor.exe

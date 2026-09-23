"""仅在用户打开 start-admin.bat 时，通过 Windows UAC 请求管理员启动。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys


def _run_as(executable: str, arguments: str, directory: str) -> int:
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    shell.ShellExecuteW.restype = ctypes.c_size_t
    # pythonw 不创建控制台；SW_SHOWNORMAL 让用户正常看到 Qt 主窗口。
    return shell.ShellExecuteW(None, 'runas', executable, arguments, directory, 1)


def launch_elevated(directory: Path, *, scan_once: bool = False) -> int:
    """Open the UI; scan_once selects its editable blank-frequency default."""
    directory = directory.resolve()
    python = directory / '.venv' / 'Scripts' / 'pythonw.exe'
    script = directory / 'main.py'
    if not python.is_file() or not script.is_file():
        print('缺少虚拟环境或 main.py，请检查 LOCAL_SETUP.md。')
        return 1
    # 使用 Windows 参数引用规则，目录含空格也可以运行。
    arguments = [str(script)] + (['--scan-once'] if scan_once else [])
    result = _run_as(str(python), subprocess.list2cmdline(arguments), str(directory))
    if result <= 32:
        print('未启动扫描器：Windows 管理员确认被取消或启动失败（代码 %d）。' % result)
        print('请重新打开 start-admin.bat，并在 Windows 确认窗口中允许启动。')
        return 1
    print('已请求以管理员权限启动扫描器。')
    return 0


if __name__ == '__main__':
    raise SystemExit(launch_elevated(Path(__file__).resolve().parent, scan_once='--scan-once' in sys.argv))

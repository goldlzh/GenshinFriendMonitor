"""Check the process token and request one administrator relaunch before GUI load."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys

import admin_start


_RETRY_ARGUMENT = '--gfm-elevation-retried'
_FAILURE_TITLE = '需要管理员权限'


def is_administrator() -> bool:
    """Check this process only; never prompt or change the current token."""
    if sys.platform != 'win32':
        return False
    try:
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        shell.IsUserAnAdmin.argtypes = []
        shell.IsUserAnAdmin.restype = wintypes.BOOL
        return bool(shell.IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


def _show_failure(message: str) -> None:
    """Show an error even for pythonw launches that have no console."""
    try:
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                    wintypes.LPCWSTR, wintypes.UINT]
        user.MessageBoxW.restype = ctypes.c_int
        # MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST
        if not user.MessageBoxW(None, message, _FAILURE_TITLE, 0x10 | 0x10000 | 0x40000):
            raise OSError('Unable to display administrator startup error')
    except (OSError, AttributeError):
        if sys.stderr is not None:
            print(f'{_FAILURE_TITLE}：{message}', file=sys.stderr)


def ensure_administrator() -> bool:
    """Return True only when this process may continue into normal GUI startup.

    False always means the caller must exit: either a replacement was requested,
    UAC was cancelled/failed, or the replacement still lacks an elevated token.
    This module deliberately imports neither configuration nor Qt.
    """
    if is_administrator():
        sys.argv[:] = [sys.argv[0]] + [arg for arg in sys.argv[1:] if arg != _RETRY_ARGUMENT]
        return True

    if _RETRY_ARGUMENT in sys.argv[1:]:
        _show_failure('重新启动后仍未获得管理员权限，软件已停止。\n'
                      '请右键软件选择「以管理员身份运行」，并在 Windows 确认窗口中允许启动。')
        return False

    executable = Path(sys.executable).resolve()
    if getattr(sys, 'frozen', False):
        directory = executable.parent
        arguments = list(sys.argv[1:])
    else:
        directory = Path(__file__).resolve().parent.parent
        arguments = [str(directory / 'main.py'), *sys.argv[1:]]
    arguments.append(_RETRY_ARGUMENT)
    try:
        result = admin_start._run_as(str(executable), subprocess.list2cmdline(arguments), str(directory))
    except (OSError, AttributeError) as error:
        _show_failure(f'请求管理员启动失败：{error}\n软件已停止，请重新打开并允许 Windows 管理员确认。')
        return False
    if result <= 32:
        _show_failure(f'Windows 管理员确认被取消或启动失败（代码 {result}）。\n'
                      '软件已停止，请重新打开，并在 Windows 确认窗口中允许启动。')
    return False

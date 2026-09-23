"""鼠标输入及可核验的 Windows SendInput 扫描码按键。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import time
from typing import Optional, Tuple

from app.config import CONFIG
from app.logger import logger

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008


class InputError(RuntimeError):
    """Windows 未接收输入，或输入前置条件不满足。"""


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD),
                ('wParamH', wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    # 必须包含 MOUSEINPUT，保证 INPUT 在 64 位 Windows 上为 40 字节。
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ('data',)
    _fields_ = [('type', wintypes.DWORD), ('data', _INPUTUNION)]


@lru_cache(maxsize=1)
def _user32():
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    api.SendInput.restype = wintypes.UINT
    api.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    api.MapVirtualKeyW.restype = wintypes.UINT
    api.GetAsyncKeyState.argtypes = [ctypes.c_int]
    api.GetAsyncKeyState.restype = wintypes.SHORT
    return api


def _send_key_event(api, scan: int, flags: int, phase: str) -> None:
    event = INPUT(type=1)  # INPUT_KEYBOARD
    event.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    ctypes.set_last_error(0)
    sent = api.SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
    if sent != 1:
        error = ctypes.get_last_error()
        # UIPI 拦截可能没有错误码，不能把 error=0 当作成功。
        raise InputError('键盘%s失败：Windows 未接收输入（SendInput=%s，错误码=%d）。'
                         '请检查扫描器与游戏权限、前台窗口及锁屏状态。' % (phase, sent, error))


def press_key(vk_code: int, *, hwnd: Optional[int] = None) -> bool:
    """发送扫描码按下/抬起；返回 True 仅代表系统接收，还须检查游戏画面。"""
    if not 1 <= vk_code <= 254:
        raise ValueError('无效的虚拟键码')
    if hwnd is not None:
        from app import window
        problem = window.input_permission_error(hwnd)
        if problem:
            raise InputError(problem)
        if not window.is_foreground(hwnd):
            raise InputError('目标游戏未处于前台，未发送按键')
    api = _user32()
    # 不替用户释放真实修饰键，避免把 O 变成 Ctrl+O 等其他快捷键。
    if any(api.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
        raise InputError('检测到 Shift/Ctrl/Alt/Win 正被按住，请松开后重试')
    scan = int(api.MapVirtualKeyW(vk_code, 4))  # MAPVK_VK_TO_VSC_EX
    if not scan:
        raise InputError('无法获得按键 0x%02X 的扫描码' % vk_code)
    flags = KEYEVENTF_SCANCODE
    if scan & 0xFF00 == 0xE000:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan &= 0xFF
    # 与游戏的逐帧输入轮询留出充分重叠，避免只有极短的按键脉冲。
    hold = max(.04, min(1.0, float(CONFIG.key_hold_seconds)))
    _send_key_event(api, scan, flags, '按下')
    try:
        time.sleep(hold)
    finally:
        # 按下成功之后无论等待是否异常，都必须尝试释放。
        try:
            _send_key_event(api, scan, flags | KEYEVENTF_KEYUP, '抬起')
        except InputError:
            logger.warning('首次抬键未被接收，补发一次释放事件')
            _send_key_event(api, scan, flags | KEYEVENTF_KEYUP, '抬起')
    time.sleep(.05)
    logger.debug('SendInput 已接收按键 0x%02X（扫描码 0x%02X）', vk_code, scan)
    return True


def press_esc(*, hwnd: Optional[int] = None) -> bool:
    return press_key(0x1B, hwnd=hwnd)


def click(x: int, y: int) -> None:
    """在屏幕绝对坐标 (x, y) 处单击左键。"""
    import win32api
    import win32con
    win32api.SetCursorPos((x, y))
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    try:
        time.sleep(0.05)
    finally:
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.05)


def move_and_click_relative(rect: Tuple[int, int, int, int], cx: int, cy: int) -> None:
    left, top, _, _ = rect
    click(left + cx, top + cy)


def mouse_event(flags: int, x: int = 0, y: int = 0) -> None:
    """Checked SendInput mouse event; coordinates are normalized when absolute."""
    event = INPUT(type=0)
    event.mi = MOUSEINPUT(dx=x, dy=y, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0)
    ctypes.set_last_error(0)
    sent = _user32().SendInput(1, ctypes.byref(event), ctypes.sizeof(INPUT))
    if sent != 1:
        raise InputError('Windows 未接收鼠标输入（SendInput=%s，错误码=%d）' %
                         (sent, ctypes.get_last_error()))


def move_mouse(x: int, y: int) -> None:
    import win32api
    # Virtual-desktop coordinates work with negative monitor origins and DPI.
    vx, vy = win32api.GetSystemMetrics(76), win32api.GetSystemMetrics(77)
    width, height = win32api.GetSystemMetrics(78), win32api.GetSystemMetrics(79)
    nx = round((x-vx)*65535/max(1, width-1))
    ny = round((y-vy)*65535/max(1, height-1))
    if not 0 <= nx <= 65535 or not 0 <= ny <= 65535:
        raise InputError('拖动坐标超出桌面范围')
    mouse_event(0x0001 | 0x8000 | 0x4000, nx, ny)


def click_client(rect, point, *, hwnd=None, should_stop=None) -> bool:
    """Click a checked point in normalized client coordinates using SendInput."""
    from app import window
    x, y = point
    if not (0 <= x < 1920 and 0 <= y < 1080):
        raise ValueError('点击坐标必须在游戏客户区内')
    def allowed():
        return ((should_stop is None or not should_stop())
                and (hwnd is None or window.is_foreground(hwnd)))
    if not allowed():
        return False
    if hwnd is not None:
        problem = window.input_permission_error(hwnd)
        if problem:
            raise InputError(problem)
        if window.get_window_rect(hwnd) != rect:
            raise InputError('点击前游戏窗口位置发生变化，请重新扫描')
    left, top, right, bottom = rect
    move_mouse(round(left+x*(right-left)/1920), round(top+y*(bottom-top)/1080))
    time.sleep(.03)
    if not allowed():
        return False
    mouse_event(0x0002)
    try:
        time.sleep(.05)
    finally:
        try:
            mouse_event(0x0004)
        except InputError:
            mouse_event(0x0004)
    return allowed()


def move_client(rect, point, *, hwnd=None, should_stop=None) -> bool:
    """Move without clicking, after the same focus/geometry checks as a click."""
    from app import window
    x, y = point
    if not (0 <= x < 1920 and 0 <= y < 1080):
        raise ValueError('移动坐标必须在游戏客户区内')
    def allowed():
        return ((should_stop is None or not should_stop())
                and (hwnd is None or window.is_foreground(hwnd)))
    if not allowed():
        return False
    if hwnd is not None:
        problem = window.input_permission_error(hwnd)
        if problem:
            raise InputError(problem)
        if window.get_window_rect(hwnd) != rect:
            raise InputError('移动鼠标前游戏窗口位置发生变化，请重新扫描')
    left, top, right, bottom = rect
    move_mouse(round(left+x*(right-left)/1920), round(top+y*(bottom-top)/1080))
    return allowed()

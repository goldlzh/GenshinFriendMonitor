"""Windows 窗口定位：按标题关键字查找目标窗口，获取其矩形。"""
from __future__ import annotations

import ctypes
from typing import List, Optional, Tuple

from app.logger import logger

Rect = Tuple[int, int, int, int]  # left, top, right, bottom


def enable_dpi_awareness() -> None:
    """启用 DPI 感知，保证 GetWindowRect 返回物理像素坐标。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def find_windows(keyword: str) -> List[Tuple[int, str, Rect]]:
    """返回标题包含 keyword 的可见主窗口列表 [(hwnd, title, rect)]。

    过滤规则：
      - 不可见 / 最小化窗口
      - owned 子窗口（存在父窗口，如提示框、菜单）
      - toolwindow（悬浮工具窗口）
      - 尺寸过小的窗口（宽 < 400 或 高 < 300）
    结果按窗口面积降序排列，方便用户优先选择真正的主窗口。
    """
    import win32con
    import win32gui

    results: List[Tuple[int, str, Rect]] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.IsIconic(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title or keyword not in title:
                return True
            # 排除 owned 窗口（父窗口存在，如弹窗/提示）
            if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                return True
            # 排除 toolwindow（悬浮工具窗口）
            exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if exstyle & win32con.WS_EX_TOOLWINDOW:
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            w, h = right - left, bottom - top
            if w < 400 or h < 300:
                return True
            results.append((hwnd, title, (left, top, right, bottom)))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(_cb, None)
    # 面积降序：主窗口通常最大
    results.sort(
        key=lambda r: (r[2][2] - r[2][0]) * (r[2][3] - r[2][1]),
        reverse=True,
    )
    return results


def get_window_rect(hwnd: int) -> Optional[Rect]:
    """获取窗口客户区矩形（物理像素）。失败返回 None。"""
    import win32gui
    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        x1, y1 = win32gui.ClientToScreen(hwnd, (left, top))
        x2, y2 = win32gui.ClientToScreen(hwnd, (right, bottom))
        return (x1, y1, x2, y2)
    except Exception as e:
        logger.error("获取窗口矩形失败 hwnd=%s: %s", hwnd, e)
        return None


def bring_to_front(hwnd: int) -> bool:
    """请求激活并核实结果；已经在前台时不再做多余的窗口操作。"""
    import win32con
    import win32gui
    if is_foreground(hwnd):
        return True
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE if win32gui.IsIconic(hwnd) else win32con.SW_SHOW)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as error:
        if not is_foreground(hwnd):
            logger.warning('系统未允许自动激活游戏窗口: %s', error)
    return is_foreground(hwnd)


def find_client_window(rect: Rect, keyword: str) -> Optional[int]:
    """Bind a manual full-client selection only to one matching game window."""
    if not keyword:
        return None
    matches = []
    for hwnd, _, _ in find_windows(keyword):
        client = get_window_rect(hwnd)
        if client and max(abs(a-b) for a,b in zip(client, rect)) <= 4:
            matches.append(hwnd)
    return matches[0] if len(matches) == 1 else None


def is_foreground(hwnd: int) -> bool:
    """全局键盘输入前确认目标仍是前台窗口。"""
    import win32gui
    try:
        return bool(hwnd and win32gui.IsWindow(hwnd) and win32gui.GetForegroundWindow() == hwnd)
    except Exception:
        return False


def point_input_error(hwnd: Optional[int], x=None, y=None) -> Optional[str]:
    """Require the point's root window to be this exact input target.

    With no coordinates, check the actual cursor after movement. GA_ROOT admits
    child controls but deliberately does not follow popup ownership (GA_ROOTOWNER).
    None is reserved for an explicitly selected manual desktop region.
    """
    if hwnd is None:
        return None
    import win32api
    import win32gui
    try:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return '目标游戏窗口已关闭，未发送鼠标输入'
        if x is None and y is None:
            x, y = win32api.GetCursorPos()
        if x is None or y is None:
            return '无法核实鼠标坐标，未发送鼠标输入'
        hit = win32gui.WindowFromPoint((int(x), int(y)))
        if not hit or win32gui.GetAncestor(hit, 2) != hwnd:  # GA_ROOT, not GA_ROOTOWNER
            return '鼠标位置被其他窗口遮挡，未发送鼠标输入；请移开遮挡窗口'
        return None
    except Exception as error:
        logger.warning('无法核实鼠标命中窗口 hwnd=%s: %s', hwnd, error)
        return '无法核实鼠标命中窗口，未发送鼠标输入'


def executable_path(hwnd: int) -> Optional[str]:
    """只读查询选中窗口的程序路径，用于寻找其本机字库。"""
    import win32api
    import win32process
    process = None
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = win32api.OpenProcess(0x1000, False, pid)
        from ctypes import wintypes
        query = ctypes.WinDLL('kernel32', use_last_error=True).QueryFullProcessImageNameW
        query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                          ctypes.POINTER(wintypes.DWORD)]
        query.restype = wintypes.BOOL
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if query(int(process), 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    except Exception:
        return None
    finally:
        if process is not None:
            process.Close()


def process_integrity_level(pid: int) -> Optional[int]:
    """只读查询进程的完整性级别，查询失败返回未知，不冒充已获权限。"""
    import win32api
    import win32con
    import win32security
    process = token = None
    try:
        process = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)
        return sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
    except Exception as error:
        logger.debug('无法查询进程权限 pid=%s: %s', pid, error)
        return None
    finally:
        if token is not None:
            token.Close()
        if process is not None:
            process.Close()


def input_permission_error(hwnd: int) -> Optional[str]:
    """Windows UIPI 不允许较低权限向较高权限窗口注入输入。"""
    import win32api
    import win32process
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return '目标窗口已失效，请重新查找游戏窗口'
    except Exception:
        return '目标窗口已失效，请重新查找游戏窗口'
    source = process_integrity_level(win32api.GetCurrentProcessId())
    target = process_integrity_level(pid)
    if source is not None and target is not None and source < target:
        logger.warning('游戏输入权限不足: scanner=0x%X target=0x%X pid=%d', source, target, pid)
        if target >= 0x3000:
            return '游戏以管理员权限运行，扫描器权限不足。请关闭程序，使用 start-admin.bat（管理员启动）后重试'
        return '目标游戏权限高于扫描器，Windows 会阻止输入。请使用 start-admin.bat（管理员启动）后重试'
    return None

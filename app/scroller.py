"""列表滚动模拟：支持两种方式翻动好友列表。

1. wheel 模式（默认，推荐）：直接向目标窗口发送 WM_MOUSEWHEEL 消息，
   不移动系统光标，规避合成拖动不被识别、以及 SetCursorPos 权限不足的问题。
2. drag 模式（备用）：在列表锚点按住鼠标左键并垂直拖动。部分机器上
   云原神客户端用原始输入追踪物理鼠标，合成绝对跳转不会被识别为滑动。
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

from app.config import CONFIG
from app.logger import logger


def _anchor(rect: Tuple[int, int, int, int]) -> Tuple[int, int]:
    """返回列表拖拽锚点（绝对屏幕坐标）。"""
    left, top, right, bottom = rect
    w = right - left
    h = bottom - top
    cx = left + int(w * CONFIG.drag_anchor_x_ratio)
    cy = top + int(h * CONFIG.drag_anchor_y_ratio)
    return cx, cy


def _move_to(cx: int, cy: int) -> bool:
    """移动系统光标到 (cx, cy)。成功返回 True；权限不足等失败时返回 False（不抛异常）。"""
    import win32api
    try:
        win32api.SetCursorPos((cx, cy))
        return True
    except Exception as e:
        logger.warning("移动光标失败(SetCursorPos 拒绝访问等): %s", e)
        return False


def _post_wheel(hwnd: int, cx: int, cy: int, delta: int) -> bool:
    """直接向目标窗口发送 WM_MOUSEWHEEL（不移动光标，规避权限/焦点问题）。"""
    import win32con
    try:
        import win32gui
        wparam = (delta & 0xFFFF) << 16  # 高 16 位为滚轮增量
        lparam = ((cy & 0xFFFF) << 16) | (cx & 0xFFFF)  # 屏幕坐标
        win32gui.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wparam, lparam)
        return True
    except Exception as e:
        logger.warning("向窗口发送滚轮消息失败: %s", e)
        return False


def scroll_clicks(rect: Tuple[int, int, int, int], clicks: int,
                  hwnd: Optional[int] = None) -> bool:
    """发送滚轮事件并返回是否成功；失败必须传回扫描器。"""
    import win32api
    import win32con
    if not clicks:
        return False
    if CONFIG.wheel_invert:
        clicks = -clicks
    cx, cy = _anchor(rect)
    delta = clicks * 120
    if _move_to(cx, cy):
        try:
            win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
            return True
        except Exception as e:
            logger.warning('合成滚轮事件失败: %s', e)
    return _post_wheel(hwnd, cx, cy, delta) if hwnd is not None else False


def drag(rect: Tuple[int, int, int, int], dy: int,
         should_stop: Optional[Callable[[], bool]] = None) -> bool:
    """平滑拖动；停止、移动失败或异常时释放鼠标按钮。"""
    import win32api
    import win32con
    if CONFIG.drag_invert:
        dy = -dy
    if should_stop is not None and should_stop():
        return False
    cx, cy = _anchor(rect)
    if not _move_to(cx, cy):
        return False
    pressed = False
    try:
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        pressed = True
        steps = max(1, abs(dy) // 12)
        for i in range(1, steps + 1):
            if should_stop is not None and should_stop():
                return False
            if not _move_to(cx, cy + int(dy * i / steps)):
                return False
            time.sleep(0.012)
        return True
    except Exception as e:
        logger.warning('拖动失败: %s', e)
        return False
    finally:
        if pressed:
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def scroll_down(rect: Tuple[int, int, int, int], step: int = None,
                should_stop: Optional[Callable[[], bool]] = None,
                hwnd: Optional[int] = None) -> bool:
    if should_stop is not None and should_stop():
        return False
    step = CONFIG.scroll_step if step is None else step
    if CONFIG.scroll_mode == 'wheel':
        success = scroll_clicks(rect, -abs(step), hwnd=hwnd)
    else:
        pixels = max(60, int((rect[3] - rect[1]) * CONFIG.scroll_overlap_ratio))
        success = drag(rect, -pixels, should_stop=should_stop)
    time.sleep(max(0, CONFIG.scroll_settle_seconds))
    return success


def scroll_to_top(rect: Tuple[int, int, int, int], times: int = 40,
                  should_stop: Optional[Callable[[], bool]] = None,
                  hwnd: Optional[int] = None) -> bool:
    for _ in range(times):
        if should_stop is not None and should_stop():
            return False
        if CONFIG.scroll_mode == 'wheel':
            success = scroll_clicks(rect, 10, hwnd=hwnd)
        else:
            success = drag(rect, CONFIG.drag_pixels, should_stop=should_stop)
        if not success:
            return False
        time.sleep(0.05)
    time.sleep(max(0, CONFIG.scroll_settle_seconds))
    return True


def drag_client(rect, start, end, *, hwnd=None, should_stop=None) -> bool:
    """Drag calibrated 1080p client coordinates; release on every failure path.

    Hold briefly at the destination before release to suppress list inertia.
    Every movement stays inside the selected game window and checks its focus.
    """
    from app import input as game_input, window
    left, top, right, bottom = rect
    def point(p):
        x, y = p
        if not (0 <= x < 1920 and 0 <= y < 1080):
            raise ValueError('拖动坐标必须在游戏客户区内')
        return (round(left+x*(right-left)/1920), round(top+y*(bottom-top)/1080))
    source, target = point(start), point(end)
    def allowed():
        return ((should_stop is None or not should_stop())
                and (hwnd is None or window.is_foreground(hwnd)))
    if not allowed():
        return False
    if hwnd is not None:
        problem = window.input_permission_error(hwnd)
        if problem:
            raise game_input.InputError(problem)
    pressed = False
    try:
        game_input.move_mouse(*source)
        time.sleep(.025)
        if not allowed():
            return False
        game_input.mouse_event(0x0002)
        pressed = True
        time.sleep(.025)
        duration = max(.08, min(1., CONFIG.drag_duration_seconds))
        steps = max(8, round(duration/.012))
        for i in range(1, steps+1):
            if not allowed():
                return False
            # Ease in/out, followed by a stationary hold: no release velocity.
            t = i/steps
            t = t*t*(3-2*t)
            game_input.move_mouse(round(source[0]+(target[0]-source[0])*t),
                                  round(source[1]+(target[1]-source[1])*t))
            time.sleep(duration/steps)
        deadline = time.monotonic()+max(0., min(3., CONFIG.drag_hold_seconds))
        while time.monotonic() < deadline:
            if not allowed():
                return False
            time.sleep(.01)
    finally:
        if pressed:
            try:
                game_input.mouse_event(0x0004)
            except game_input.InputError:
                game_input.mouse_event(0x0004)
    if allowed():
        game_input.move_mouse(*point((1860, 1000)))
    time.sleep(max(0., CONFIG.fast_settle_seconds))
    return True


def drag_six_rows(rect, *, hwnd=None, should_stop=None, distance=None):
    from app.fast_layout import DRAG_X, DRAG_START_Y, ROW_PITCH
    distance = 6*ROW_PITCH if distance is None else float(distance)
    if not 30 <= distance <= 780:
        raise ValueError('滑动距离超出校准范围')
    return drag_client(rect, (DRAG_X, DRAG_START_Y), (DRAG_X, DRAG_START_Y-distance),
                       hwnd=hwnd, should_stop=should_stop)


def drag_scrollbar_top(rect, thumb, *, hwnd=None, should_stop=None):
    from app.fast_layout import TRACK_TOP
    return drag_client(rect, (thumb.x, thumb.center), (thumb.x, TRACK_TOP),
                       hwnd=hwnd, should_stop=should_stop)

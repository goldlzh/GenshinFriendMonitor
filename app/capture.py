"""屏幕截图：截取指定窗口区域，返回 RGB numpy 数组。"""
from __future__ import annotations

from typing import Optional
import threading

import numpy as np

from app.logger import logger

_target = threading.local()
_window_broker = None


def initialize_window_capture(app):
    """Install the window broker on the QApplication thread before starting workers."""
    global _window_broker
    from app.window_capture import WindowCaptureBroker
    if _window_broker is not None:
        _window_broker.close()
    _window_broker = WindowCaptureBroker(app)
    app.aboutToQuit.connect(_window_broker.close)
    return _window_broker


def _capture_window_frame(hwnd, rect):
    if _window_broker is None:
        logger.error('窗口采集尚未初始化 hwnd=%s', hwnd)
        return None
    return _window_broker.capture(hwnd, rect)


def capture_window(hwnd, left, top, right, bottom):
    """Scope this rectangle call to an HWND; None explicitly selects the desktop.

    Keeping capture_region's four-argument boundary also lets offline image
    providers replay the same scan path without requiring a live native window.
    """
    previous = getattr(_target, 'hwnd', None)
    _target.hwnd = hwnd
    try:
        return capture_region(left, top, right, bottom)
    finally:
        _target.hwnd = previous


def capture_region(left: int, top: int, right: int, bottom: int) -> Optional[np.ndarray]:
    """Capture the selected backend's rectangle as owned RGB pixels."""
    hwnd = getattr(_target, 'hwnd', None)
    if hwnd is not None:
        return _capture_window_frame(hwnd, (left, top, right, bottom))
    import mss

    width = max(1, right - left)
    height = max(1, bottom - top)
    try:
        with mss.mss() as sct:
            monitor = {"left": left, "top": top, "width": width, "height": height}
            shot = sct.grab(monitor)
            # BGRA -> RGB
            arr = np.asarray(shot)[:, :, :3][:, :, ::-1]
            return np.ascontiguousarray(arr)
    except Exception as e:
        logger.error("截图失败 (%d,%d,%d,%d): %s", left, top, right, bottom, e)
        return None


def image_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """计算两张图的结构相似度（简单灰度均值差，0~1，越接近 1 越相似）。"""
    if a.shape != b.shape:
        return 0.0
    try:
        ga = a.mean(axis=2, dtype=np.float32)
        gb = b.mean(axis=2, dtype=np.float32)
        diff = np.abs(ga - gb).mean() / 255.0
        return 1.0 - float(diff)
    except Exception:
        return 0.0

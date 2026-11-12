"""屏幕截图：截取指定窗口区域，返回 RGB numpy 数组。"""
from __future__ import annotations

from typing import Optional
import os
import threading

import numpy as np

from app.logger import logger

_target = threading.local()
_window_broker = None


class CaptureError(RuntimeError):
    """The selected client cannot safely provide a complete game frame."""


def experimental_window_capture_enabled():
    return os.environ.get('GFM_EXPERIMENTAL_WINDOW_CAPTURE') == '1'


def initialize_window_capture(app):
    """Desktop is the default; install the experimental broker only on opt-in."""
    global _window_broker
    if _window_broker is not None:
        _window_broker.close()
        _window_broker = None
    if not experimental_window_capture_enabled():
        logger.info('使用屏幕采集；请保持游戏完整位于同一屏幕内且无遮挡；遮挡采集暂为实验功能')
        return None
    from app.window_capture import WindowCaptureBroker
    logger.warning('已显式启用实验性窗口采集，尚未通过完整扫描验收')
    _window_broker = WindowCaptureBroker(app)
    app.aboutToQuit.connect(_window_broker.close)
    return _window_broker


def _capture_window_frame(hwnd, rect):
    if _window_broker is None:
        logger.error('窗口采集尚未初始化 hwnd=%s', hwnd)
        return None
    return _window_broker.capture(hwnd, rect)


def capture_window(hwnd, left, top, right, bottom):
    """Bind a rectangle to its HWND; normal operation uses desktop pixels.

    Keeping capture_region's four-argument boundary also lets offline image
    providers replay the same scan path without requiring a live native window.
    """
    previous = getattr(_target, 'hwnd', None)
    _target.hwnd = hwnd
    try:
        return capture_region(left, top, right, bottom)
    finally:
        _target.hwnd = previous


def _validate_client(hwnd, rect):
    from app.window import capture_rect_error
    problem = capture_rect_error(hwnd, rect)
    if problem:
        logger.error('%s hwnd=%s rect=%s', problem, hwnd, rect)
        raise CaptureError(problem)


def capture_region(left: int, top: int, right: int, bottom: int) -> Optional[np.ndarray]:
    """Capture the selected backend's rectangle as owned RGB pixels."""
    hwnd = getattr(_target, 'hwnd', None)
    if hwnd is not None:
        _validate_client(hwnd, (left, top, right, bottom))
        if experimental_window_capture_enabled():
            result = _capture_window_frame(hwnd, (left, top, right, bottom))
            _validate_client(hwnd, (left, top, right, bottom))
            return result
    import mss

    width = max(1, right - left)
    height = max(1, bottom - top)
    try:
        with mss.mss() as sct:
            monitor = {"left": left, "top": top, "width": width, "height": height}
            shot = sct.grab(monitor)
            # BGRA -> RGB
            arr = np.asarray(shot)[:, :, :3][:, :, ::-1]
            result = np.ascontiguousarray(arr)
        if hwnd is not None:
            _validate_client(hwnd, (left, top, right, bottom))
        return result
    except CaptureError:
        raise
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

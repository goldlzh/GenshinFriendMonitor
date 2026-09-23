"""屏幕截图：截取指定窗口区域，返回 RGB numpy 数组。"""
from __future__ import annotations

from typing import Optional

import numpy as np

from app.logger import logger


def capture_region(left: int, top: int, right: int, bottom: int) -> Optional[np.ndarray]:
    """截取屏幕矩形区域，返回 RGB numpy 数组 (H, W, 3)。"""
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

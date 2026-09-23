"""RapidOCR 引擎封装：延迟加载 + 统一接口。"""
from __future__ import annotations

import threading
from typing import Any, List, Optional, Tuple

import numpy as np

from app.config import CONFIG
from app.logger import logger, ocr_fail_logger

_engine = None
_engine_lock = threading.Lock()
_inference_lock = threading.Lock()


class OcrResult:
    """单个 OCR 识别框。"""

    def __init__(self, text: str, confidence: float, box: Any):
        self.raw_text = text
        self.text = text.strip()
        self.confidence = float(confidence)
        self.box = box  # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]

    @property
    def center(self) -> Tuple[float, float]:
        xs = [p[0] for p in self.box]
        ys = [p[1] for p in self.box]
        return (sum(xs) / 4, sum(ys) / 4)

    @property
    def top(self) -> float:
        return min(p[1] for p in self.box)

    @property
    def bottom(self) -> float:
        return max(p[1] for p in self.box)

    @property
    def left(self) -> float:
        return min(p[0] for p in self.box)

    def __repr__(self) -> str:
        return f"OcrResult({self.text!r}, {self.confidence:.2f})"


def get_engine() -> Any:
    """获取全局 RapidOCR 引擎（首次调用时加载模型）。"""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                logger.info("正在加载 RapidOCR 模型（首次运行需几秒）……")
                from rapidocr import RapidOCR

                # ONNX's default thread pool oversubscribes a running game. Small
                # fixed text crops need only a few CPU threads, not every core.
                _engine = RapidOCR(params={
                    'EngineConfig.onnxruntime.intra_op_num_threads': max(1, int(CONFIG.ocr_threads)),
                    'EngineConfig.onnxruntime.inter_op_num_threads': 1,
                    'Global.use_cls': False,
                    'Det.limit_type': 'max',
                    'Det.limit_side_len': 960,
                    'Rec.rec_batch_num': 12,
                })
                logger.info("RapidOCR 模型加载完成")
    return _engine


def recognize(image: np.ndarray) -> List[OcrResult]:
    """识别一张图片，返回 OcrResult 列表（按从上到下、从左到右排序）。

    可选的放大预处理：游戏里昵称多为艺术字/小字，直接 OCR 容易漏检；
    放大后再识别能显著提升召回率。返回的坐标会换算回原图坐标系。
    """
    from PIL import Image

    engine = get_engine()

    scale = max(1.0, float(CONFIG.ocr_upscale_factor))
    if scale > 1.0:
        import cv2

        h, w = image.shape[:2]
        image = cv2.resize(
            image, (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_CUBIC,
        )

    # rapidocr>=3 返回 RapidOCROutput dataclass：boxes/txts/scores
    with _inference_lock:
        result = engine(Image.fromarray(image))
    items: List[OcrResult] = []
    if result is None:
        return items
    boxes = getattr(result, "boxes", None)
    txts = getattr(result, "txts", None)
    scores = getattr(result, "scores", None)
    if boxes is None or txts is None:
        return items
    if scores is None:
        return items
    for box, text, score in zip(boxes, txts, scores):
        conf = float(score)
        txt = str(text).strip()
        if conf < CONFIG.ocr_confidence:
            ocr_fail_logger.warning("低置信度文本: %r (%.3f)", txt, conf)
            continue
        # box: shape (4,2) → [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
        # 放大后坐标需换算回原图坐标系
        box_list = [[float(p[0]) / scale, float(p[1]) / scale] for p in box]
        items.append(OcrResult(txt, conf, box_list))
    # 按垂直坐标排序（同一行按水平）
    items.sort(key=lambda r: (round(r.top), r.left))
    return items


def recognize_lines(images: List[np.ndarray], *, minimum_width=160) -> List[Tuple[str, float]]:
    """Batch known single-line RGB fields without repeatedly detecting text.

    RapidOCR's lower-level recognizer accepts BGR crops and preserves input
    order even when it sorts widths internally. Never share a mutable engine
    call between the normal scanner and UID worker.
    """
    if not images:
        return []
    from rapidocr.ch_ppocr_rec.typings import TextRecInput
    engine = get_engine()
    valid = [np.ascontiguousarray(image[:, :, ::-1]) for image in images]
    with _inference_lock:
        recognizer = engine.text_rec
        original_shape = recognizer.rec_image_shape
        # Only the empty padding minimum changes; long lines still expand to
        # their full width. Keep the ordinary detector and weak-name retry at
        # their original shape, even after a failed inference.
        recognizer.rec_image_shape = [*original_shape[:2], int(minimum_width)]
        try:
            result = recognizer(TextRecInput(img=valid))
        finally:
            recognizer.rec_image_shape = original_shape
    texts = result.txts or ()
    scores = result.scores if result.scores is not None else ()
    if len(texts) != len(images) or len(scores) != len(images):
        raise RuntimeError('批量 OCR 返回数量与识别框数量不一致')
    # Nickname callers need leading/trailing whitespace to choose an image-only
    # name. Individual non-name fields normalize their own strings as needed.
    return [(str(text), float(score)) for text, score in zip(texts, scores)]

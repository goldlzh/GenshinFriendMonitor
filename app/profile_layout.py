"""UID fields calibrated from the supplied 2560 x 1440 client screenshot.

Inputs are normalized 1920 x 1080 RGB client frames. No list text, signatures,
character levels or the account owner's bottom-right UID enter these crops.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

import cv2
import numpy as np

from app import fast_layout as layout, ocr_engine

UID = (576, 188, 756, 228)
SHOWCASE = (1060, 207, 1190, 244)
NAMECARD = (1340, 207, 1450, 244)
FIELDS = (UID, SHOWCASE, NAMECARD)
MIN_CONFIDENCE = .85
# User's book-only reference matches original pixels (485, 272)-(2079, 1232).
# Remove the (1, 45) client origin, scale by .75 and round outward.
BOOK = (363, 170, 1559, 891)
MENU = (232, 100, 680, 960)


def crop_profile_book(image):
    """Copy the complete calibrated book, including UID, without the outer game.

    Only normalized full frames and our exact archived book size are accepted;
    an unknown image layout must never be silently cropped during migration.
    """
    shape = (BOOK[3]-BOOK[1], BOOK[2]-BOOK[0], 3)
    if image is None or image.dtype != np.uint8:
        raise ValueError('主页截图必须是 RGB uint8 图像')
    if image.shape == shape:
        return image.copy()
    if image.shape != (1080, 1920, 3):
        raise ValueError('主页截图尺寸不符合已校准的客户区或书本区域')
    return layout.crop(image, BOOK).copy()


def crop_profile_capture(encoded, expected_uid):
    """Return cropped archive bytes; validate old full frames against their UID.

    Pure migration helper: no database or filesystem writes. Already cropped
    bytes are returned unchanged, preserving hashes and avoiding JPEG decay.
    Invalid/unknown frames raise instead of replacing the stored evidence.
    """
    if not re.fullmatch(r'[1-9][0-9]{8}', expected_uid or ''):
        raise ValueError('裁剪旧主页前需要有效好友 UID')
    decoded = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise ValueError('旧主页图片无法解码')
    image = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
    if image.shape == (BOOK[3]-BOOK[1], BOOK[2]-BOOK[0], 3):
        return bytes(encoded)
    if image.shape != (1080, 1920, 3) or read(image).uid != expected_uid:
        raise ValueError('旧主页尺寸或 UID 校验未通过，保留原图')
    ok, result = cv2.imencode('.jpg', cv2.cvtColor(crop_profile_book(image), cv2.COLOR_RGB2BGR),
                              [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError('主页书本截图编码失败')
    return result.tobytes()


def read_menu(frame):
    """Detect only the complete avatar menu and return full-frame coordinates."""
    if frame is None or frame.shape[:2] != (1080, 1920):
        raise ValueError('菜单识别需要 1920x1080 游戏客户区画面')
    x, y, _, _ = MENU
    return [ocr_engine.OcrResult(item.text, item.confidence,
             [[px+x, py+y] for px, py in item.box])
            for item in ocr_engine.recognize(layout.crop(frame, MENU))]


def menu_changed(before, current):
    """Avoid expensive text detection while the click has produced no menu.

    This is only a readiness hint. A changed crop still needs one newly
    appearing exact menu label before any profile-button click is allowed.
    """
    old, new = (layout.crop(frame, MENU).astype(np.int16) for frame in (before, current))
    return bool(old.shape == new.shape and old.size and np.abs(old-new).mean() >= 3.)


def menu_frame_stable(recognized, current):
    """An OCR result with no button is usable only while its source stays current."""
    old, new = (layout.crop(frame, MENU).astype(np.int16) for frame in (recognized, current))
    return bool(old.shape == new.shape and old.size and np.abs(old-new).mean() <= 1.5)


def menu_target_stable(recognized, current, target):
    """Confirm the recognized label at the SAME position without another slow OCR.

    Inspect the complete label and nearby button surface. Background animation
    outside the menu must not invalidate a ready button; a vanished/moved label
    must never leave a clickable stale coordinate.
    """
    x1 = max(0, int(min(p[0] for p in target.box))-8)
    x2 = min(1920, int(max(p[0] for p in target.box))+9)
    y1, y2 = max(0, int(target.top)-6), min(1080, int(target.bottom)+7)
    old, new = (layout.crop(frame, (x1, y1, x2, y2)) for frame in (recognized, current))
    if old.shape != new.shape or not old.size:
        return False
    difference = np.abs(old.astype(np.int16)-new.astype(np.int16))
    return bool(difference.mean() <= 6. and (difference.max(axis=2) > 25).mean() <= .025)


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def uid_from_text(text, confidence):
    if not confidence >= MIN_CONFIDENCE:
        return None
    match = re.fullmatch(r'UID[:.·]?(?P<uid>[1-9][0-9]{8})', compact(text), re.I)
    return match['uid'] if match else None


@dataclass(frozen=True)
class ProfileRead:
    visible: bool
    uid: str | None
    fields: list


def read(frame):
    if frame is None or frame.shape[:2] != (1080, 1920):
        raise ValueError('UID 识别需要 1920x1080 游戏客户区画面')
    fields = ocr_engine.recognize_lines([layout.crop(frame, roi) for roi in FIELDS])
    visible = all(compact(text) == expected and score >= MIN_CONFIDENCE
                  for (text, score), expected in zip(fields[1:], ('角色展柜', '名片')))
    uid = uid_from_text(*fields[0]) if visible else None
    return ProfileRead(visible, uid, fields)

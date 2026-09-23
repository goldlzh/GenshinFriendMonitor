"""Read the logged-in account from the HUD, never from a friend's profile."""
from __future__ import annotations

import re
import time
import unicodedata

import cv2

from app import capture, friend_screen, ocr_engine

# Coordinates refer to a normalized 1920 x 1080 game client, excluding its title bar.
ACCOUNT_UID_RECT = (1620, 1036, 1918, 1080)
# The HUD digits occupy their own line at the lower right, independently of
# the friend's profile. Excluding the UID label avoids a clipped/merged colon
# invalidating nine otherwise readable digits. This crop has a small margin on
# every side and is used only after the ordinary labelled read was rejected.
ACCOUNT_UID_DIGITS_RECT = (1733, 1048, 1870, 1079)
MIN_CONFIDENCE = .90


class AccountIdentityError(ValueError):
    pass


def account_uid_from_text(text, confidence):
    if not confidence >= MIN_CONFIDENCE:
        return None
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    match = re.fullmatch(r'UID[:：.·]?([1-9][0-9]{8})', compact, re.I)
    return match[1] if match else None


def _digits_from_text(text, confidence):
    if not confidence >= MIN_CONFIDENCE:
        return None
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    return compact if re.fullmatch(r'[1-9][0-9]{8}', compact) else None


def _full_line_digits(text, confidence):
    """Corroborate the whole HUD line when its UID label alone was misread.

    A maximal digit run cannot silently select the middle nine of ten digits.
    This witness is never accepted on its own: the two numeric crops must agree.
    """
    if not confidence >= MIN_CONFIDENCE:
        return None
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
    match = re.fullmatch(r'(?:[A-Za-z|][A-Za-z0-9|]{1,3}[:.·]?)?([1-9][0-9]{8})', compact)
    if re.search(r'[0-9]{10}', compact):
        return None
    return match[1] if match else None


def read_account_uid(image):
    frame = friend_screen.normalize(image)
    if frame is None:
        return None
    x1, y1, x2, y2 = ACCOUNT_UID_RECT
    full_line = frame[y1:y2, x1:x2]
    values = ocr_engine.recognize_lines([full_line])
    actual = account_uid_from_text(*values[0]) if len(values) == 1 else None
    if actual is not None:
        return actual  # Normal scans still use exactly one OCR crop.
    primary = values[0] if len(values) == 1 else ('', 0.)
    compact = re.sub(r'\s+', '', unicodedata.normalize('NFKC', primary[0]))
    if re.search(r'[0-9]{10}', compact):
        return None  # A narrower field must not turn ten visible digits into nine.
    whole_line_uid = _full_line_digits(*primary)
    x1, y1, x2, y2 = ACCOUNT_UID_DIGITS_RECT
    digits = frame[y1:y2, x1:x2]
    gray = cv2.cvtColor(digits, cv2.COLOR_RGB2GRAY)
    # Do not ask a recognizer to hallucinate a UID from an empty/covered HUD.
    if int(gray.max())-int(gray.min()) < 35:
        return None
    inverse = cv2.cvtColor(255-gray, cv2.COLOR_GRAY2RGB)
    variants = [digits, inverse]
    if whole_line_uid is None:
        # Numeric crops alone might omit a leading/trailing character. Keep a
        # full-width witness when the original line provided no reliable UID.
        variants.append(cv2.cvtColor(255-cv2.cvtColor(full_line, cv2.COLOR_RGB2GRAY),
                                    cv2.COLOR_GRAY2RGB))
    reads = ocr_engine.recognize_lines(variants)
    if len(reads) != len(variants):
        return None
    candidates = [_digits_from_text(*value) for value in reads[:2]]
    if whole_line_uid is None:
        whole_line_uid = _full_line_digits(*reads[2])
    # No majority vote or expected-account hint: both image variants
    # must contain the same complete, confident nine digits.
    return (candidates[0] if len(candidates) == 2 and candidates[0] is not None
            and candidates[0] == candidates[1] == whole_line_uid else None)


def _reject_other_account(actual, expected_uid):
    if actual is not None and expected_uid is not None and actual != expected_uid:
        raise AccountIdentityError('游戏账号 UID %s 与所选账号 %s 不符，已停止；请切换账号选项' %
                                   (actual, expected_uid))


def verify_account_frame(image, expected_uid):
    actual = read_account_uid(image)
    if actual is None:
        raise AccountIdentityError('无法读清右下角账号 UID，已停止；请保持完整游戏画面可见')
    _reject_other_account(actual, expected_uid)
    return actual


def read_current_account(rect, capture_fn=None, *, expected_uid=None, should_continue=None):
    """Require two consecutive fresh HUD reads; this function sends no game input."""
    capture_fn = capture_fn or capture.capture_region
    previous = None
    for attempt in range(4):
        if should_continue is not None and not should_continue():
            raise AccountIdentityError('账号识别已取消')
        if attempt:
            time.sleep(.15)
            if should_continue is not None and not should_continue():
                raise AccountIdentityError('账号识别已取消')
        image = capture_fn(*rect)
        actual = read_account_uid(image)
        # A missing transient frame may be retried; a confidently different
        # owner stops immediately and can never be outvoted by later frames.
        _reject_other_account(actual, expected_uid)
        if actual and actual == previous:
            return actual
        previous = actual
    raise AccountIdentityError('右下角账号 UID 未能连续两帧读到一致结果，请稍后重试')

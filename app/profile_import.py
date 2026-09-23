"""Read an offline profile screenshot for explicit human review, without game input."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

import numpy as np

from app import ocr_engine


@dataclass(frozen=True)
class ProfileDraft:
    uid: str = ''
    nickname: str = ''
    level: int | None = None
    signature: str | None = None
    missing: tuple[str, ...] = ()


def compact(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))


def right(item):
    return max(point[0] for point in item.box)


def height(item):
    return max(1., item.bottom - item.top)


def same_line(first, second):
    return abs(first.center[1] - second.center[1]) <= max(height(first), height(second)) * .5


def parse_profile_fields(items, width):
    """Anchor on the left-hand adventure row, never on showcase levels or HUD UID.

    Coordinates are relative to the supplied image, so both a cropped book and
    a full game screenshot work. A single screenshot is a draft, not live proof
    of online status or membership in the current friend list.
    """
    adventure = [(item, re.fullmatch(r'(?:冒险等阶|冒险等级)([0-9]{1,2})?', compact(item.text)))
                 for item in items if item.confidence >= .85]
    adventure = [(item, match) for item, match in adventure if match]
    if len(adventure) != 1:
        return ProfileDraft(missing=('UID', '昵称', '冒险等级', '签名'))
    anchor, match = adventure[0]
    row_height = height(anchor)
    upper = [item for item in items if item.bottom < anchor.top and item.confidence >= .85]
    uid_candidates = []
    for item in upper:
        text = compact(item.text)
        uid = re.fullmatch(r'UID[:：.·]?([1-9][0-9]{8})', text, re.I)
        if uid and anchor.left <= item.center[0] < width * .72:
            uid_candidates.append((uid[1], item))
        elif re.fullmatch(r'UID[:：.·]?', text, re.I):
            adjacent = [candidate for candidate in upper if same_line(item, candidate)
                        and 0 <= candidate.left-right(item) <= row_height*2
                        and re.fullmatch(r'[1-9][0-9]{8}', compact(candidate.text))]
            if len(adjacent) == 1 and anchor.left <= adjacent[0].center[0] < width*.72:
                uid_candidates.append((compact(adjacent[0].text), adjacent[0]))
    uid, uid_item = uid_candidates[0] if len(uid_candidates) == 1 else ('', None)
    level = int(match[1]) if match[1] else None
    digits = [item for item in items if item is not anchor and item.confidence >= .85
              and same_line(anchor, item) and item.left >= right(anchor)
              and re.fullmatch(r'[0-9]{1,2}', compact(item.text))]
    # Full-width game screenshots place the profile book left of the showcase.
    if uid_item is not None:
        column_right = min(width, uid_item.center[0] * 2 - anchor.left + row_height * 1.5)
    else:
        column_right = min(width, anchor.left + width * .43)
    digits = [item for item in digits if right(item) <= column_right + row_height]
    if level is None and len(digits) == 1:
        level = int(compact(digits[0].text))
    if level is not None and not 1 <= level <= 60:
        level = None

    nickname = ''
    if uid_item is not None:
        names = [item for item in upper if item.top > uid_item.bottom
                 and item.bottom < anchor.top - row_height*.2
                 and anchor.left-row_height*2 <= item.left and right(item) <= column_right
                 and height(item) >= row_height*1.05
                 and not re.fullmatch(r'(?:Lv[.。]?)?[0-9]+', compact(item.text), re.I)]
        if names:
            largest = max(names, key=height)
            peers = [item for item in names if height(item) >= height(largest)*.8]
            if all(same_line(largest, item) for item in peers):
                nickname = ''.join(item.raw_text.strip() for item in sorted(peers, key=lambda item: item.left))

    world = [item for item in items if compact(item.text).startswith('世界等级')
             and item.confidence >= .85 and item.top > anchor.bottom
             and abs(item.left-anchor.left) <= row_height*2]
    signature = None
    if len(world) == 1:
        start = world[0].bottom
        footers = [item.top for item in items if item.top > start
                   and anchor.left-row_height <= item.left < column_right
                   and compact(item.text) in ('成就总数', '好感度已满的角色', '深境螺旋')]
        end = min(footers) if footers else start + row_height*4
        lines = [item for item in items if item.top > start + row_height*.2
                 and item.bottom < end - row_height*.2
                 and anchor.left-row_height <= item.left and right(item) <= column_right]
        if lines and all(item.confidence >= .85 for item in lines):
            signature = '\n'.join(item.raw_text.strip() for item in sorted(lines, key=lambda item: (item.top, item.left)))
    values = (('UID', uid), ('昵称', nickname), ('冒险等级', level), ('签名', signature))
    return ProfileDraft(uid, nickname, level, signature,
                        tuple(label for label, value in values if value in (None, '')))


def recognize_profile(image):
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
            or image.ndim != 3 or image.shape[2] != 3 or min(image.shape[:2]) < 40):
        raise ValueError('请粘贴清晰、完整的好友主页截图')
    return parse_profile_fields(ocr_engine.recognize(image), image.shape[1])

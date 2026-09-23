"""16:9 好友界面：固定标题确认、真实横条边缘分割、逐卡片 OCR。

参考图为用户提供的 2547×1431 游戏画面。坐标统一到 1920×1080，
只在完整游戏客户区内使用；不从任意文字或昵称反推当前界面。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import unicodedata
from typing import Callable, Optional

import cv2
import numpy as np

from app import ocr_engine, parser
from app.config import CONFIG

WIDTH, HEIGHT = 1920, 1080
TITLE_RECT = (105, 10, 480, 82)
COUNT_RECT = (1510, 10, 1802, 82)
LIST_RECT = (136, 112, 1788, 966)
ENTER_RECT = (0, 864, 310, 1080)
HUD_MODE_RECT = (0, 864, 720, 1080)
TEXT_LEFT, TEXT_RIGHT = 266, 840
AVATAR_X = 194
CARD_HEIGHT = 115


def game_window_problem(rect):
    """Explain unsupported client geometry before capture or game input."""
    if rect is None or len(rect) != 4:
        return '无法获取游戏客户区，请重新查找窗口。'
    width, height = rect[2]-rect[0], rect[3]-rect[1]
    if (width < 640 or height < 360 or
            abs(width / max(1, height) - WIDTH / HEIGHT) > .035):
        return ('需要完整的 16:9 游戏客户区（至少 640×360），当前为 %d×%d。'
                '请在游戏中设置为 1280×720、1920×1080 等 16:9 分辨率后重新开始；不含标题栏。' %
                (width, height))
    return ''


@dataclass(frozen=True)
class CardBand:
    top: int
    bottom: int

    @property
    def height(self):
        return self.bottom - self.top

    @property
    def center(self):
        return (self.top + self.bottom) / 2


@dataclass
class FriendPage:
    confirmed: bool = False
    expected_total: Optional[int] = None
    entries: list[parser.FriendEntry] = field(default_factory=list)
    bands: list[CardBand] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    observations: list = field(default_factory=list)
    ocr_seconds: float = 0.
    field_ocr_seconds: float = 0.
    name_refine_seconds: float = 0.

    @property
    def reliable(self):
        return self.confirmed and not self.issues


def normalize(image: np.ndarray) -> Optional[np.ndarray]:
    """等比布局仅用于 16:9；不把窄幅列表裁剪或不同布局强行拉伸。

    少量宽高比误差允许截图取整（参考图本身并非整除 16:9）。
    """
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        return None
    h, w = image.shape[:2]
    if w < 640 or h < 360 or abs(w / h - WIDTH / HEIGHT) > .035:
        return None
    if (w, h) == (WIDTH, HEIGHT):
        return image
    method = cv2.INTER_AREA if w > WIDTH else cv2.INTER_CUBIC
    return cv2.resize(image, (WIDTH, HEIGHT), interpolation=method)


def crop(image, rect):
    x1, y1, x2, y2 = rect
    return np.ascontiguousarray(image[y1:y2, x1:x2])


def _title_matches(items) -> bool:
    # 只接受指定区域内同一行的完整面包屑；OCR 把斜线读成 I 也可识别。
    items = [r for r in items if r.confidence >= CONFIG.ocr_confidence]
    for anchor in items:
        height = max(1, anchor.bottom - anchor.top)
        line = [r for r in items if abs(r.center[1] - anchor.center[1]) <= height * .55]
        text = ''.join(r.text for r in sorted(line, key=lambda r: r.left))
        chinese = ''.join(re.findall(r'[\u4e00-\u9fff]', text))
        if chinese == '好友我的好友':
            return True
    return False


def is_friend_list(image) -> bool:
    if CONFIG.fast_ocr:
        from app.fast_layout import is_friend_list as fast_title
        return fast_title(image)
    frame = normalize(image)
    return frame is not None and _title_matches(ocr_engine.recognize(crop(frame, TITLE_RECT)))


def detect_card_bands(frame: np.ndarray, min_top: int = LIST_RECT[1]) -> list[CardBand]:
    """寻找横向长边，再配对约 115px 高的上下边；每帧重新定位。

    用多列像素的中位变化压制文字、头像、名片图案。只接受有上下边
    的完整卡片，顶部/底部被裁掉的条目留给下一张有重叠的截图。
    不按固定起点逐行切片，也不串联 OCR 文本的相邻 y 距离。
    """
    strip = frame[:, 530:810].astype(np.float32)
    delta = np.abs(np.diff(strip, axis=0)).mean(axis=2)
    strength = np.median(delta, axis=1)
    candidates = np.flatnonzero(strength > 12)
    candidates = [int(y) for y in candidates if min_top <= y < LIST_RECT[3] - 1]
    groups = []
    for y in candidates:
        if groups and y - groups[-1][-1] <= 4:
            groups[-1].append(y)
        else:
            groups.append([y])
    edges = [max(group, key=lambda y: strength[y]) + 1 for group in groups]
    bands = []
    for top in edges:
        if bands and top < bands[-1].bottom + 5:
            continue
        bottoms = [y for y in edges if CARD_HEIGHT * .92 <= y - top <= CARD_HEIGHT * 1.08]
        if not bottoms:
            continue
        bottom = min(bottoms, key=lambda y: abs(y - top - CARD_HEIGHT))
        bands.append(CardBand(top, bottom))
    # 完整相邻卡片的中心不能重叠；高度范围也排除了约 126px 的整行间距。
    return bands


def read_page(image, left_offset=0, top_offset=0,
              should_continue: Optional[Callable[[], bool]] = None,
              *, interface_mode='normal') -> FriendPage:
    if CONFIG.fast_ocr or interface_mode == 'qianxing':
        from app.fast_layout import read_page as fast_page
        return fast_page(image, left_offset, top_offset, should_continue,
                         interface_mode=interface_mode)
    page = FriendPage()
    frame = normalize(image)
    if frame is None:
        page.issues.append('请选择完整的 16:9 游戏画面，当前比例或区域不匹配')
        return page
    header = ocr_engine.recognize(crop(frame, TITLE_RECT))
    page.texts.extend(r.text for r in header)
    if not _title_matches(header):
        page.issues.append('左上角未识别到“好友 / 我的好友”')
        return page
    page.confirmed = True
    count = ocr_engine.recognize(crop(frame, COUNT_RECT))
    page.texts.extend(r.text for r in count)
    page.expected_total = parser.extract_friend_count(count)
    page.bands = detect_card_bands(frame)
    scale_x, scale_y = image.shape[1] / WIDTH, image.shape[0] / HEIGHT
    for band in page.bands:
        if should_continue is not None and not should_continue():
            page.issues.append('识别已停止')
            break
        # 只 OCR 横条左侧的身份/状态字段，签名和操作按钮不进入解析器。
        results = ocr_engine.recognize(crop(frame, (TEXT_LEFT, band.top, TEXT_RIGHT, band.bottom)))
        page.texts.append('卡片 %d-%d: %s' % (band.top, band.bottom, ' | '.join(r.text for r in results)))
        entry = parser.parse_card(results, band.height,
                                  int(left_offset + AVATAR_X * scale_x),
                                  int(top_offset + band.center * scale_y))
        if entry is None:
            page.issues.append('卡片 %d-%d 字段不完整或存在冲突，已跳过' % (band.top, band.bottom))
        else:
            page.entries.append(entry)
    # 同一画面有同名好友时，不凭昵称任选其一更新状态。
    names = [entry.nickname for entry in page.entries]
    ambiguous = {name for name in names if names.count(name) > 1}
    if ambiguous:
        page.entries = [entry for entry in page.entries if entry.nickname not in ambiguous]
        page.issues.append('同一画面存在同名好友，已跳过其状态更新')
    if not page.bands and page.expected_total != 0:
        page.issues.append('未检测到完整的好友横条')
    return page


def is_gameplay_hud(image) -> bool:
    """左下角出现独立 Enter 提示即可；聊天图标不是导航前提。"""
    frame = normalize(image)
    if frame is None:
        return False
    corner = crop(frame, ENTER_RECT)
    return any(item.confidence >= CONFIG.ocr_confidence
               and re.sub(r'\s+', '', item.text).casefold() == 'enter'
               for item in ocr_engine.recognize(corner))


def interface_mode_from_hud(items):
    """Classify the HUD shortcut row, never a friend's nickname or level badge."""
    threshold = max(.85, CONFIG.ocr_confidence)
    width, height = HUD_MODE_RECT[2]-HUD_MODE_RECT[0], HUD_MODE_RECT[3]-HUD_MODE_RECT[1]
    items = [r for r in items if r.left >= -4 and r.top >= -4
             and max(p[0] for p in r.box) <= width+4 and r.bottom <= height+4]
    def compact(item):
        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', item.text)).casefold()
    anchors = [r for r in items if r.confidence >= threshold and compact(r) == 'enter']
    if len(anchors) != 1:
        return None
    anchor = anchors[0]
    height = max(1., anchor.bottom-anchor.top)
    row = sorted([r for r in items if r.left > anchor.left
                  and abs(r.center[1]-anchor.center[1]) <= height*.55], key=lambda r:r.left)
    for index, item in enumerate(row):
        # Also accept OCR splitting one keycap into adjacent Alt, + and N boxes.
        group = [item]
        for other in row[index+1:index+3]:
            if other.left-max(p[0] for p in group[-1].box) > height*.6:
                break
            group.append(other)
        for size in range(1, len(group)+1):
            if (''.join(compact(r) for r in group[:size]) == 'alt+n'
                    and all(r.confidence >= threshold for r in group[:size])):
                return 'qianxing'
    # A partial/uncertain Alt shortcut must not silently become normal mode.
    if any('alt' in compact(r) or compact(r) in ('n', 'a1t+n', 'ait+n') for r in row):
        return None
    return 'normal'


def gameplay_interface_mode(image):
    frame = normalize(image)
    if frame is None:
        return None
    return interface_mode_from_hud(ocr_engine.recognize(crop(frame, HUD_MODE_RECT)))

"""Calibrated six-row layout, batched OCR, and visual scroll measurements.

All coordinates are in the 1920 x 1080 *client area*. Windows title bars are
never part of the live capture. The three supplied native screenshots share
this layout. Text fields belong to a single observed card, not nearby OCR boxes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
import cv2
import numpy as np

from app import nickname_ocr, ocr_engine, parser
from app.config import CONFIG

TITLE = (135, 22, 430, 68)
COUNT = (1550, 22, 1790, 68)
VIEW = (136, 112, 1788, 958)
ROW_PITCH = 124.8
ROW_HEIGHT = 114.8
NAME = (266, 5, 830, 62)  # Keep accents and the left edge of hovered nicknames.
LEVEL = (278, 66, 356, 102)
STATUS = (385, 64, 830, 105)
QIANXING_LEVEL = (315, 65, 379, 103)  # Logical parser slot; OCR uses the detected digits only.
QIANXING_STATUS = (402, 64, 830, 105)  # Keep the leading digit when a hovered card shifts left.
# Signature text starts after its speech bubble. Its baseline is lower than
# the join/offline button text, so this crop excludes those labels and chat.
SIGNATURE = (914, 70, 1690, 105)
DRAG_X = 800
DRAG_START_Y = 925
NEXT_ROW_TOP = 156  # Leave room for drag inertia instead of landing at the clip edge.
SCROLL_X = 1804
TRACK_TOP, TRACK_BOTTOM = 117, 944


@dataclass(frozen=True)
class Scrollbar:
    top: int
    bottom: int
    x: int = SCROLL_X
    contrast: float = 0.

    @property
    def at_top(self):
        return self.top <= TRACK_TOP + 3

    @property
    def at_bottom(self):
        return self.bottom >= TRACK_BOTTOM - 3

    @property
    def center(self):
        return (self.top + self.bottom) / 2

    @property
    def fraction(self):
        travel = TRACK_BOTTOM - TRACK_TOP - (self.bottom - self.top)
        return max(0., min(1., (self.top - TRACK_TOP) / max(1, travel)))


@dataclass
class CardObservation:
    band: object
    entry: object
    name_image: np.ndarray
    avatar_image: np.ndarray
    fields: list = field(default_factory=list)
    issue: str = ''
    name_audit: dict = field(default_factory=dict)
    tag_images: list = field(default_factory=list)
    level_audit: dict = field(default_factory=dict)
    status_audit: dict = field(default_factory=dict)
    signature_image: np.ndarray | None = None


def crop(frame, rect):
    x1, y1, x2, y2 = map(int, rect)
    return np.ascontiguousarray(frame[y1:y2, x1:x2])


def scrollbar(frame) -> Scrollbar | None:
    """Find a narrow neutral thumb brighter than BOTH of its adjacent sides.

    Absolute brightness alone mistakes pale moving scenery for a long thumb.
    Search a bounded horizontal strip so antialiasing / scaling cannot move the
    real thumb outside the three columns used by the old detector.
    """
    if frame is None or frame.shape[:2] != (1080, 1920):
        return None
    y0, y1 = TRACK_TOP-3, TRACK_BOTTOM+3
    x0, x1 = SCROLL_X-9, SCROLL_X+10
    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY).astype(np.float32)
    colors = frame[y0:y1, x0:x1].astype(np.float32)
    left = np.stack([np.median(gray[y0:y1, x-11:x-7], axis=1)
                     for x in range(x0, x1)], axis=1)
    right = np.stack([np.median(gray[y0:y1, x+8:x+12], axis=1)
                      for x in range(x0, x1)], axis=1)
    contrast = gray[y0:y1, x0:x1]-np.maximum(left, right)
    appearance = (colors.min(axis=2) >= 135) & (np.ptp(colors, axis=2) < 50)
    mask = ((contrast >= 20) & appearance).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    candidates = []
    for index, (x, y, width, height, area) in enumerate(stats[1:], 1):
        if height < 20 or not 2 <= width <= 11 or area/(width*height) < .60:
            continue
        candidates.append((index, x, y, width, height))
    # Preserve the strong seed requirement and ambiguity rejection. A bright
    # background may lower contrast only along the tail of a genuine thumb;
    # allow connected weak pixels to complete THAT seed, never seed a new one.
    if len(candidates) != 1:
        return None
    index, x, y, width, height = candidates[0]
    support = ((contrast >= 8) & appearance).astype(np.uint8)
    _, weak_labels, weak_stats, _ = cv2.connectedComponentsWithStats(support)
    region_ids = np.unique(weak_labels[labels == index])
    if len(region_ids) == 1 and region_ids[0] != 0:
        wx, wy, ww, wh, area = weak_stats[region_ids[0]]
        if (2 <= ww <= 11 and area/(ww*wh) >= .60
                and wh <= max(90, 2*height+4)):
            x, y, width, height = wx, wy, ww, wh
    return Scrollbar(int(y+y0), int(y+y0+height),
                     int(round(x+x0+(width-1)/2)),
                     float(np.median(contrast[labels == index])))


def scrollbar_displacement(before, after, total):
    """Bound image matching using a stable thumb length, never two fractions.

    Different apparent thumb heights (occlusion/contrast loss) must not turn a
    large real movement into a tiny fraction delta. A dubious size change drops
    this auxiliary bound; image correspondence still has to prove the movement.
    """
    if before is None or after is None or total is None:
        return None
    old_height, new_height = before.bottom-before.top, after.bottom-after.top
    if abs(old_height-new_height) > max(3, .08*max(old_height, new_height)):
        return None
    travel = TRACK_BOTTOM-TRACK_TOP-(old_height+new_height)/2
    if travel <= 0:
        return None
    extent = max(0., total*ROW_PITCH-(VIEW[3]-VIEW[1]))
    return (after.top-before.top)*extent/travel


def bands(frame, interface_mode='normal'):
    """Fit the calibrated row grid to visible long edges, including hover cards.

    A selected card can grow a few pixels. Several agreeing normal cards anchor
    the phase; we keep exactly one independent slot per row and omit clipped rows.
    """
    from app.friend_screen import CardBand, detect_card_bands
    min_top = 106 if interface_mode == 'qianxing' else VIEW[1]
    detected = detect_card_bands(frame, min_top=min_top) if interface_mode == 'qianxing' else detect_card_bands(frame)
    if len(detected) < 3:
        return detected
    candidates = [float(b.top) for b in detected]
    def cost(phase):
        residuals = [abs((y-phase+ROW_PITCH/2) % ROW_PITCH-ROW_PITCH/2) for y in candidates]
        return sum(sorted(residuals)[:max(3, len(residuals)-1)])
    anchor = min(candidates, key=cost)
    residuals = [(y-anchor+ROW_PITCH/2) % ROW_PITCH-ROW_PITCH/2 for y in candidates]
    anchor += float(np.median([d for d in residuals if abs(d) <= 6]))
    phase = anchor % ROW_PITCH
    result = []
    for i in range(-1, 10):
        top = int(round(phase+i*ROW_PITCH))
        bottom = int(round(phase+i*ROW_PITCH+ROW_HEIGHT))
        if top >= min_top and bottom <= VIEW[3]:
            if top < VIEW[1] and not any(abs(b.top-top) <= 4 and abs(b.bottom-bottom) <= 4 for b in detected):
                continue
            result.append(CardBand(top, bottom))
    return result[:6]


def field_rect(band, field):
    x1, y1, x2, y2 = field
    return (x1, band.top+y1, x2, band.top+y2)


def qianxing_level_region(frame, band):
    """Locate digits after the small Lv dot, before the badge's right ornament.

    White glyph geometry anchors the crop independently of blue/purple/gold
    badge color. The prefix is located, never OCR'd. A proven pair of horizontal
    dashes is explicit unavailable level; missing evidence is not level zero.
    """
    search = (300, band.top+65, 383, band.top+103)
    image = crop(frame, search)
    missing = {'kind': 'unreadable', 'reason': '未确认等级数字专属边界'}
    if image.shape[:2] != (38, 83):
        return missing
    white = ((image.min(axis=2) > 160) & (np.ptp(image, axis=2) < 90)).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(white)
    components = [(int(x+300), int(y+65), int(w), int(h), int(area))
                  for x, y, w, h, area in stats[1:] if area >= 3]
    dots = []
    for x, y, w, h, area in components:
        if not (320 <= x <= 351 and 84 <= y <= 94 and 1 <= w <= 4 and 1 <= h <= 4 and area <= 12):
            continue
        prefix = [c for c in components if x-25 <= c[0] and c[0]+c[2] <= x
                  and 5 <= c[3] <= 15 and c[4] >= 10 and abs(c[1]+c[3]-y-h) <= 3]
        if len(prefix) >= 2:
            dots.append((x, y, w, h))
    candidates = []
    for dx, dy, dw, dh in dots:
        following = [c for c in components if dx+dw < c[0] <= 370
                     and c[0]+c[2] <= 374 and 73 <= c[1] <= 92 and c[4] >= 5]
        digits = sorted(c for c in following if 9 <= c[3] <= 19
                        and 1 <= c[2] <= 15 and c[4] >= 8)
        if digits:
            if (not 1 <= len(digits) <= 3 or digits[0][0]-dx-dw > 5
                    or any(b[0]-a[0]-a[2] > 5 for a, b in zip(digits, digits[1:]))
                    or max(c[1] for c in digits)-min(c[1] for c in digits) > 3
                    or max(c[1]+c[3] for c in digits)-min(c[1]+c[3] for c in digits) > 3):
                continue
            left, right = digits[0][0]-1, max(c[0]+c[2] for c in digits)+1
            top, bottom = min(c[1] for c in digits)-2, max(c[1]+c[3] for c in digits)+2
            candidates.append({'kind': 'digits', 'rect': (left, band.top+top, right, band.top+bottom),
                               'glyph_count': len(digits), 'prefix_dot': (dx, band.top+dy, dw, dh)})
        else:
            # A genuine dash can rasterize to one row of only 5-7 pixels.
            # Its narrow horizontal geometry and the second matching dash
            # supply evidence independently of the stricter digit-area gate.
            dashes = sorted(c for c in following if 5 <= c[2] <= 10 and 1 <= c[3] <= 4
                            and 78 <= c[1] <= 88)
            if (len(dashes) == 2 and 1 <= dashes[0][0]-dx-dw <= 5
                    and 1 <= dashes[1][0]-dashes[0][0]-dashes[0][2] <= 4
                    and abs(dashes[0][1]-dashes[1][1]) <= 1):
                candidates.append({'kind': 'unset', 'prefix_dot': (dx, band.top+dy, dw, dh),
                                   'dash_rects': [(x, band.top+y, w, h) for x, y, w, h, _ in dashes],
                                   'reason': 'Lv 后确认两条短横线，界面未提供等级'})
    return candidates[0] if len(candidates) == 1 else missing


def read_qianxing_levels(frame, page_bands):
    """Strict digits-only OCR with two views and independent glyph-count proof."""
    audits = [qianxing_level_region(frame, band) for band in page_bands]
    outputs = [('--', 1.) if item['kind'] == 'unset' else ('', 0.) for item in audits]
    images, owners, originals = [], [], {}
    for index, item in enumerate(audits):
        if item['kind'] != 'digits':
            continue
        original = crop(frame, item['rect'])
        originals[index] = original
        mask = ((original.min(axis=2) > 160) & (np.ptp(original, axis=2) < 90)).astype(np.uint8)*255
        binary = cv2.cvtColor(255-mask, cv2.COLOR_GRAY2RGB)
        images.extend((cv2.copyMakeBorder(original, 6, 6, 6, 6, cv2.BORDER_REPLICATE),
                       cv2.copyMakeBorder(binary, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=(255, 255, 255))))
        owners.append(index)
    if not images:
        return outputs, audits
    values = ocr_engine.recognize_lines(images)
    if len(values) != len(images):
        return outputs, audits
    extra_owners, extra_images = [], []
    for pair, index in enumerate(owners):
        reads = values[2*pair:2*pair+2]
        audits[index]['reads'] = reads
        if (all(isinstance(text, str) and re.fullmatch(r'[0-9]{1,3}', text)
                and len(text) == audits[index]['glyph_count'] and .85 <= score <= 1.
                for text, score in reads) and max(score for _, score in reads) >= .97
                and reads[0][0] == reads[1][0]):
            outputs[index] = (reads[0][0], min(score for _, score in reads))
        else:
            audits[index]['reason'] = '数字专属裁片的两次识别或独立字数校验不一致'
            if (reads[0][0] == reads[1][0] and re.fullmatch(r'[0-9]{1,3}', reads[0][0])
                    and len(reads[0][0]) == audits[index]['glyph_count']
                    and all(0. <= score <= 1. for _, score in reads)
                    and max(score for _, score in reads) >= .97
                    and min(score for _, score in reads) < .85):
                # Either preprocessing view can lose thin strokes. Confirm the same entire
                # number in another color view, without lowering its score gate
                # or letting majority vote overrule a different digit string.
                extra_owners.append(index)
                enlarged = cv2.resize(originals[index], None, fx=2, fy=2,
                                      interpolation=cv2.INTER_CUBIC)
                extra_images.append(cv2.copyMakeBorder(enlarged, 8, 8, 8, 8,
                                                       cv2.BORDER_REPLICATE))
    if extra_images:
        extra_reads = ocr_engine.recognize_lines(extra_images)
        if len(extra_reads) == len(extra_owners):
            for index, value in zip(extra_owners, extra_reads):
                audits[index]['extra_color_read'] = value
                supporting = max(audits[index]['reads'], key=lambda read: read[1])
                if (value[0] == supporting[0] and re.fullmatch(r'[0-9]{1,3}', value[0])
                        and .97 <= value[1] <= 1.):
                    outputs[index] = (supporting[0], min(supporting[1], value[1]))
                    audits[index].pop('reason', None)
                    audits[index]['method'] = 'rescaled_color_confirmation'
    return outputs, audits


def qianxing_activity_evidence(frame, band, text, confidence):
    """Corroborate a complete custom/editor activity with text AND icon color.

    Orange custom-world activity and green available/active status occur in
    this isolated status row. Gray text, colorful card art alone, an isolated
    icon, or incomplete quotes cannot classify arbitrary unknown text online.
    """
    evidence = {'verified': False}
    if parser.qianxing_activity_name(text) is None or not .90 <= confidence <= 1.:
        return evidence
    text_image = crop(frame, field_rect(band, QIANXING_STATUS))
    icon_image = crop(frame, (381, band.top+67, 405, band.top+98))
    if not text_image.size or not icon_image.size:
        return evidence
    text_hsv = cv2.cvtColor(text_image, cv2.COLOR_RGB2HSV)
    icon_hsv = cv2.cvtColor(icon_image, cv2.COLOR_RGB2HSV)
    for color, low_hue, high_hue in (('orange', 8, 27), ('green', 28, 52)):
        def foreground(hsv):
            return ((hsv[:, :, 0] >= low_hue) & (hsv[:, :, 0] <= high_hue)
                    & (hsv[:, :, 1] >= 95) & (hsv[:, :, 2] >= 105))
        mask = foreground(text_hsv)
        mask[:, :5] = False  # A shared icon edge is not evidence of text color.
        ys, xs = np.where(mask)
        icon_pixels = int(np.count_nonzero(foreground(icon_hsv)))
        _, _, components, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
        text_components = sum(4 <= area and 5 <= height <= 29 and width <= 40
                              for _, _, width, height, area in components[1:])
        if (len(xs) >= 45 and icon_pixels >= 18 and int(xs.max()-xs.min()) >= 25
                and text_components >= 3
                and 8 <= int(ys.max()-ys.min()+1) <= 29
                and int(xs.max()) < text_image.shape[1]-3):
            return {'verified': True, 'color': color, 'text_pixels': len(xs),
                    'icon_pixels': icon_pixels, 'text_components': int(text_components),
                    'raw': (text, confidence)}
    return evidence


def card_field_regions(frame, band, interface_mode='normal'):
    """Return (nickname_rect, tag_rectangles, tags_known) in client pixels.

    Qianxing's cyan label pills follow the nickname, so their position cannot
    be a fixed name-width cutoff. Detect their connected background separately
    and exclude the first pill from the name before any OCR or image matching.
    The caller must select the mode from the game interface, never the level.
    """
    name_rect = field_rect(band, NAME)
    if interface_mode == 'normal':
        return name_rect, [], False
    if interface_mode != 'qianxing':
        raise ValueError('未知好友界面模式')
    search = (NAME[0], band.top+NAME[1], 1440, band.top+NAME[3])
    image = crop(frame, search)
    if not image.size:
        return name_rect, [], False
    hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    cyan = ((hsv[:, :, 0] >= 85) & (hsv[:, :, 0] <= 105)
            & (hsv[:, :, 1] >= 110) & (hsv[:, :, 2] >= 150)).astype(np.uint8)
    _, _, stats, _ = cv2.connectedComponentsWithStats(cyan)
    valid, uncertain, distant_art = [], [], []
    for x, y, width, height, area in stats[1:]:
        if width < 35 or height < 10 or area < 150:
            continue
        rect = (int(search[0]+x), int(search[1]+y),
                int(search[0]+x+width), int(search[1]+y+height))
        pill_shape = (18 <= height <= 40 and width/height >= 1.8
                      and area/(width*height) >= .45)
        if (pill_shape
                and x > 0 and x+width < image.shape[1]
                and y > 0 and y+height < image.shape[0]):
            valid.append(rect)
        elif rect[0] >= NAME[2] and not pill_shape:
            distant_art.append(rect)
        else:
            uncertain.append(rect)
    # Cyan namecard artwork far beyond the nickname is not label evidence.
    # Keep malformed pieces adjoining a real pill uncertain, as they may be
    # part of that label. Clipped pill shapes and weak-color pills below are
    # still uncertain regardless of their horizontal position.
    uncertain.extend(rect for rect in distant_art if any(
        rect[0] <= pill[2]+16 and rect[2] >= pill[0]-16
        and rect[1] < pill[3] and rect[3] > pill[1]
        for pill in valid))
    if not valid:
        # Color shifts may erase the strict cyan seed while the label's pill
        # shape remains visible. Such a region is uncertain, never "no tags";
        # its white letters must still stay out of nickname OCR.
        relaxed = ((hsv[:, :, 0] >= 75) & (hsv[:, :, 0] <= 115)
                   & (hsv[:, :, 1] >= 65) & (hsv[:, :, 2] >= 80)).astype(np.uint8)
        _, _, relaxed_stats, _ = cv2.connectedComponentsWithStats(relaxed)
        for x, y, width, height, area in relaxed_stats[1:]:
            if (18 <= height <= 40 and width >= 40 and width/height >= 1.8
                    and area/(width*height) >= .45 and y > 0
                    and y+height < image.shape[0]):
                uncertain.append((int(search[0]+x), int(search[1]+y),
                                  int(search[0]+x+width), int(search[1]+y+height)))
    valid.sort()
    # Inconclusive cyan regions inside the nickname slot cannot be OCR'd as a
    # confident name/tag. Keep a conservative crop and leave tags unconfirmed.
    boundaries = valid+[rect for rect in uncertain if rect[0] < NAME[2]]
    if boundaries:
        right = max(NAME[0]+1, min(NAME[2], min(rect[0] for rect in boundaries)-5))
        name_rect = (NAME[0], name_rect[1], right, name_rect[3])
    return name_rect, valid, not bool(uncertain)


def nickname_rect(frame, band, interface_mode='normal'):
    """The actual nickname-only rectangle, excluding Qianxing tag pills."""
    return card_field_regions(frame, band, interface_mode)[0]


def nickname_crop(frame, band, interface_mode='normal'):
    """Original RGB nickname pixels for list OCR, UID return checks and UI."""
    return crop(frame, nickname_rect(frame, band, interface_mode))


def read_tags(frame, regions, interface_mode='normal'):
    """Optional label OCR; confirmed absence and unreadable labels differ."""
    if interface_mode != 'qianxing':
        return [None for _ in regions]
    results = ['' if known and not tags else None for _, tags, known in regions]
    owners, images = [], []
    for index, (_, tags, known) in enumerate(regions):
        if known:
            for rect in tags:
                owners.append(index)
                images.append(crop(frame, rect))
    if not images:
        return results
    try:
        values = ocr_engine.recognize_lines(images)
        if len(values) != len(images):
            return results
        for index in set(owners):
            fields = [value for owner, value in zip(owners, values) if owner == index]
            if all(isinstance(text, str) and text.strip() and .90 <= score <= 1.
                   and '\ufffd' not in text and '\ufffc' not in text
                   for text, score in fields):
                results[index] = '/'.join(text.strip() for text, _ in fields)
    except Exception:
        pass
    return results


def prepare_status(image):
    """Trim only blank horizontal margins, keeping light and dark status ink."""
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    contrast = np.abs(cv2.medianBlur(gray, 31).astype(np.int16)-gray)
    _, labels, stats, _ = cv2.connectedComponentsWithStats((contrast > 14).astype(np.uint8))
    ink = [i for i, shape in enumerate(stats) if i and shape[4] >= 4 and shape[3] >= 3]
    _, xs = np.where(np.isin(labels, ink))
    if not len(xs):
        return image
    return np.ascontiguousarray(image[:, max(0, int(xs.min())-6):
                                     min(image.shape[1], int(xs.max())+7)])


def title_matches(text):
    return ''.join(re.findall(r'[\u4e00-\u9fff]', text)) == '好友我的好友'


def is_friend_list(image):
    from app.friend_screen import normalize
    frame = normalize(image)
    if frame is None:
        return False
    text, confidence = ocr_engine.recognize_lines([crop(frame, TITLE)])[0]
    return confidence >= CONFIG.ocr_confidence and title_matches(text)


def read_header(frame):
    """Check title and counter together without re-running every row's OCR."""
    outputs = ocr_engine.recognize_lines([crop(frame, TITLE), crop(frame, COUNT)])
    title, confidence = outputs[0]
    confirmed = confidence >= CONFIG.ocr_confidence and title_matches(title)
    match = re.search(r'(\d{1,3})\s*[/／]\s*(\d{1,3})', outputs[1][0])
    count = (int(match[1]) if match and outputs[1][1] >= CONFIG.ocr_confidence
             and int(match[1]) <= int(match[2]) else None)
    return confirmed, count


def read_signatures(frame, page_bands):
    """Read optional signatures separately; failure cannot invalidate a page."""
    missing = [(None, 0.) for _ in page_bands]
    if not page_bands:
        return missing
    try:
        originals = [crop(frame, field_rect(band, SIGNATURE)) for band in page_bands]
        padded = [cv2.copyMakeBorder(image, 12, 12, 0, 0, cv2.BORDER_REPLICATE)
                  for image in originals]
        values = ocr_engine.recognize_lines(originals+padded)
        # Never accidentally use title/name/status from an incompatible batch.
        if len(values) != 2*len(page_bands):
            return missing
        checked = []
        for value in values:
            if not isinstance(value, (tuple, list)) or len(value) != 2:
                return missing
            text, confidence = value
            if (isinstance(text, str) and isinstance(confidence, (int, float))
                    and text.strip() and .90 <= confidence <= 1.
                    and '\ufffd' not in text and '\ufffc' not in text
                    and not text.rstrip().endswith(('...', '…', '⋯'))):
                checked.append((text.strip(), float(confidence)))
            else:
                checked.append((None, 0.))
        # A wide crop can lose a short signature's final letters with a high
        # model score. Demand agreement after a different vertical padding;
        # this is corroboration, never a substitute for the friend's UID.
        return [(a[0], min(a[1], b[1])) if a[0] is not None and a[0] == b[0]
                else (None, 0.) for a, b in zip(checked[:len(page_bands)], checked[len(page_bands):])]
    except Exception:
        # Signatures supplement already verified identities; a missing optional
        # field must not discard valid level/status/name observations.
        return missing


def _quick_optional_text(value):
    text, confidence = value
    if (isinstance(text, str) and text.strip() and .90 <= confidence <= 1.
            and '\ufffd' not in text and '\ufffc' not in text
            and not text.rstrip().endswith(('...', '…', '⋯'))):
        return text.strip(), confidence
    return None, 0.


def signature_read_pending(image, value):
    """Retain uncertain text; only a visibly blank crop can be empty.

    This small foreground check never guesses text and needs no extra OCR.
    Both light and dark strokes count so unusual symbols remain reviewable.
    """
    if _quick_optional_text(value)[0] is not None:
        return False
    if value[0] and value[0].strip():
        return True
    if image is None or not image.size:
        return True
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    difference = np.abs(cv2.medianBlur(gray, 31).astype(np.int16)-gray)
    _, _, components, _ = cv2.connectedComponentsWithStats((difference > 12).astype(np.uint8))
    return any(area >= 3 and height >= 2 for _, _, _, height, area in components[1:])


def _quick_status(frame, band, value, interface_mode):
    """Reuse the strict status grammar without requiring an optional level."""
    text, confidence = value
    evidence = (qianxing_activity_evidence(frame, band, text, confidence)
                if interface_mode == 'qianxing' else {})
    # A word such as "正在" inside unknown OCR text is not sufficient evidence
    # of a state. Accept complete known labels or corroborated custom activity.
    state_text = text.strip()
    known = (state_text in ('在线', '在线中', '游戏中', '联机', '可邀请', '离线', '离开', '暂离',
                            '深境螺旋', '尘歌壶', '千星奇域', '幻想真境剧诗')
             or re.fullmatch(r'(?:\d+\s*(?:天|小时|分钟|秒)前|今日内|昨天|前天)(?:最后登录|在线)?', state_text)
             or re.fullmatch(r'(?:提瓦特|千星奇域|深境螺旋|尘歌壶|幻想真境剧诗)'
                             r'(?:游玩中|挑战中|探索中|进行中|游戏中|冒险中)', state_text))
    if interface_mode == 'qianxing':
        known = (known or state_text == '空闲中' or evidence.get('verified')
                 or re.fullmatch(r'\d{1,3}\s*[/／]\s*\d{1,3}\s*组队中', state_text))
    if not known:
        evidence.update(quick=True, recognized=False, raw=value)
        return ('unknown', None, None), evidence
    detail_slots = ((QIANXING_LEVEL, QIANXING_STATUS) if interface_mode == 'qianxing'
                    else (LEVEL, STATUS))
    fields = [('核验', 1.), ('1' if interface_mode == 'qianxing' else 'Lv.1', 1.), value]
    items = []
    for (field_text, score), (x1, y1, x2, y2) in zip(fields, (NAME, *detail_slots)):
        box = [[x1-NAME[0], y1], [x2-NAME[0], y1],
               [x2-NAME[0], y2], [x1-NAME[0], y2]]
        items.append(ocr_engine.OcrResult(field_text, score, box))
    parsed = parser.parse_card(items, band.height, 0, 0, interface_mode=interface_mode,
                               qianxing_activity_verified=bool(evidence.get('verified')),
                               qianxing_activity_color=evidence.get('color'))
    evidence.update(quick=True, recognized=parsed is not None, raw=value)
    if parsed is not None:
        evidence.update(activity=parsed.qianxing_activity,
                        activity_verified=parsed.qianxing_activity_verified)
    return ((parsed.status, parsed.last_seen, parsed.location) if parsed is not None
            else ('unknown', None, None)), evidence


def _read_quick_page(image, frame, page, regions, *, left_offset, top_offset,
                     should_continue, interface_mode, started):
    """Read each field once; retain uncertain fields without inventing values."""
    images = [crop(frame, TITLE), crop(frame, COUNT)]
    specifications = []
    def add(rect, prepare=None):
        field = crop(frame, rect)
        images.append(prepare(field) if prepare else field)
        return len(images)-1
    for band, (name_rect, tag_rects, tags_known) in zip(page.bands, regions):
        spec = {'name': add(name_rect, nickname_ocr.prepare),
                'status': add(field_rect(band, QIANXING_STATUS if interface_mode == 'qianxing'
                                         else STATUS), prepare_status),
                'tags_known': tags_known, 'tags': []}
        if interface_mode == 'qianxing':
            audit = qianxing_level_region(frame, band)
            spec['level_audit'] = dict(audit, quick=True)
            spec['level'] = (add(audit['rect'], lambda value: cv2.copyMakeBorder(
                value, 6, 6, 6, 6, cv2.BORDER_REPLICATE)) if audit['kind'] == 'digits' else None)
        else:
            spec['level_audit'] = {'quick': True}
            spec['level'] = add(field_rect(band, LEVEL))
        spec['signature'] = add(field_rect(band, SIGNATURE))
        if interface_mode == 'qianxing' and tags_known:
            spec['tags'] = [add(rect) for rect in tag_rects]
        specifications.append(spec)
    field_started = time.perf_counter()
    outputs = ocr_engine.recognize_lines(images)
    page.field_ocr_seconds = time.perf_counter()-field_started
    if len(outputs) != len(images):
        page.issues.append('单次字段识别返回数量不一致')
        return page
    outputs = [(text, float(score)) if isinstance(text, str)
               and isinstance(score, (int, float)) and not isinstance(score, bool)
               and 0. <= score <= 1. else ('', 0.) for text, score in outputs]
    title, confidence = outputs[0]
    page.texts = [title, outputs[1][0]]
    if confidence < CONFIG.ocr_confidence or not title_matches(title):
        page.issues.append('左上角未识别到“好友 / 我的好友”')
        return page
    page.confirmed = True
    match = re.search(r'(\d{1,3})\s*[/／]\s*(\d{1,3})', outputs[1][0])
    if match and outputs[1][1] >= CONFIG.ocr_confidence and int(match[1]) <= int(match[2]):
        page.expected_total = int(match[1])
    name_images = [crop(frame, rect) for rect, _, _ in regions]
    refine_started = time.perf_counter()
    names, audits = nickname_ocr.refine(name_images, [outputs[s['name']] for s in specifications],
                                       should_continue, retry_ocr=False)
    page.name_refine_seconds = time.perf_counter()-refine_started
    if should_continue is not None and not should_continue():
        page.issues.append('识别已停止')
        return page
    scale_x, scale_y = image.shape[1]/1920, image.shape[0]/1080
    actual_bands = None
    for i, (band, spec, region) in enumerate(zip(page.bands, specifications, regions)):
        name_rect, tag_rects, tags_known = region
        if interface_mode == 'qianxing' and not tags_known and name_rect[2] < NAME[2]:
            names[i] = ('', 0.)
            audits[i] = {'method': 'rejected', 'quick': True, 'image_only': True,
                         'reason': '昵称与标签边界未完全确认，保留昵称裁片待核查'}
        nickname, name_confidence = names[i]
        pending = not bool(nickname)
        pending_evidence = (audits[i].get('image_only') or
                            nickname_ocr.tight_rect(nickname_ocr.ink_mask(name_images[i])) is not None)
        if pending and not pending_evidence:
            if actual_bands is None:
                from app.friend_screen import detect_card_bands
                actual_bands = (detect_card_bands(frame, min_top=106) if interface_mode == 'qianxing'
                                else detect_card_bands(frame))
            pending_evidence = any(abs(real.top-band.top) <= 8 and abs(real.bottom-band.bottom) <= 8
                                   for real in actual_bands)
        level_audit = spec['level_audit']
        level_value = outputs[spec['level']] if spec['level'] is not None else (
            ('--', 1.) if level_audit.get('kind') == 'unset' else ('', 0.))
        text, score = level_value
        level = None
        if interface_mode == 'qianxing':
            level_audit['reads'] = [level_value] if spec['level'] is not None else []
            if (re.fullmatch(r'[0-9]{1,3}', text) and score >= .97
                    and len(text) == level_audit.get('glyph_count')):
                level = int(text)
            elif level_audit.get('kind') != 'unset':
                level_audit['reason'] = '等级单次数字识别不确定，留空'
        else:
            matched = re.fullmatch(r'(?:(?:[Ll][Vv][.。]?|冒险等阶|等级)\s*)?([0-9]{1,2})', text.strip())
            if matched and score >= .90 and 1 <= int(matched[1]) <= 60:
                level = int(matched[1])
            level_audit.update(reads=[level_value], kind='digits' if level is not None else 'unreadable')
        (status, last_seen, location), status_audit = _quick_status(
            frame, band, outputs[spec['status']], interface_mode)
        signature, signature_confidence = _quick_optional_text(outputs[spec['signature']])
        signature_image = crop(frame, field_rect(band, SIGNATURE))
        signature_pending = signature_read_pending(signature_image, outputs[spec['signature']])
        tags = '' if interface_mode == 'qianxing' and tags_known and not tag_rects else None
        if spec['tags']:
            tag_values = [_quick_optional_text(outputs[index]) for index in spec['tags']]
            if all(value[0] is not None for value in tag_values):
                tags = '/'.join(value[0] for value in tag_values)
        fields = [names[i], level_value, outputs[spec['status']]]
        entry = None
        if not pending or pending_evidence:
            entry = parser.FriendEntry(nickname, status, last_seen,
                                       level if interface_mode == 'normal' else None,
                                       int(left_offset+194*scale_x), int(top_offset+band.center*scale_y),
                                       name_confidence, source=' | '.join(text for text, _ in fields),
                                       nickname_pending=pending, signature=signature,
                                       signature_confidence=signature_confidence, location=location,
                                       interface_mode=interface_mode,
                                       qianxing_level=level if interface_mode == 'qianxing' else None,
                                       tags=tags, signature_pending=signature_pending,
                                       qianxing_activity=status_audit.get('activity'),
                                       qianxing_activity_verified=bool(status_audit.get('activity_verified')))
        issue = ('昵称待人工核查，已保留原图' if pending and entry else
                 '无法确认完整卡片中的昵称证据' if entry is None else '')
        page.observations.append(CardObservation(
            band, entry, name_images[i], crop(frame, (158, band.top+10, 232, band.top+105)),
            fields, issue, audits[i], [crop(frame, rect) for rect in tag_rects], level_audit, status_audit,
            signature_image))
        page.texts.append('卡片 %d-%d: %s' % (band.top, band.bottom,
                          ' | '.join('%s(%.3f)' % value for value in fields)))
        if entry is None:
            page.issues.append('卡片 %d-%d %s' % (band.top, band.bottom, issue))
        else:
            page.entries.append(entry)
    if not page.bands and page.expected_total != 0:
        page.issues.append('未检测到完整的好友横条')
    page.ocr_seconds = time.perf_counter()-started
    return page


def read_page(image, left_offset=0, top_offset=0, should_continue=None,
              interface_mode='normal', *, quick=False):
    from app.friend_screen import FriendPage, normalize
    started = time.perf_counter()
    page = FriendPage()
    if interface_mode not in ('normal', 'qianxing'):
        page.issues.append('未知好友界面模式')
        return page
    frame = normalize(image)
    if frame is None:
        page.issues.append('请选择完整的 16:9 游戏客户区')
        return page
    if should_continue is not None and not should_continue():
        page.issues.append('识别已停止')
        return page
    page.bands = bands(frame, interface_mode=interface_mode) if interface_mode == 'qianxing' else bands(frame)
    regions = [card_field_regions(frame, band, interface_mode) for band in page.bands]
    if quick:
        return _read_quick_page(image, frame, page, regions, left_offset=left_offset, top_offset=top_offset,
                                should_continue=should_continue, interface_mode=interface_mode, started=started)
    detail_slots = ((QIANXING_LEVEL, QIANXING_STATUS) if interface_mode == 'qianxing'
                    else (LEVEL, STATUS))
    rectangles = [TITLE, COUNT]
    for band, (name_rect, _, _) in zip(page.bands, regions):
        rectangles.append(name_rect)
        rectangles.extend(field_rect(band, slot) for slot in
                          ((QIANXING_STATUS,) if interface_mode == 'qianxing' else detail_slots))
    images = [crop(frame, r) for r in rectangles]
    stride = 2 if interface_mode == 'qianxing' else 3
    for i in range(len(page.bands)):
        images[2+stride*i] = nickname_ocr.prepare(images[2+stride*i])
        status_index = 1+stride*(i+1)
        images[status_index] = prepare_status(images[status_index])
    field_started = time.perf_counter()
    outputs = ocr_engine.recognize_lines(images)
    page.field_ocr_seconds = time.perf_counter()-field_started
    title, title_conf = outputs[0]
    page.texts = [title, outputs[1][0]]
    if title_conf < CONFIG.ocr_confidence or not title_matches(title):
        page.issues.append('左上角未识别到“好友 / 我的好友”')
        return page
    page.confirmed = True
    match = re.search(r'(\d{1,3})\s*[/／]\s*(\d{1,3})', outputs[1][0])
    if match and outputs[1][1] >= CONFIG.ocr_confidence and int(match[1]) <= int(match[2]):
        page.expected_total = int(match[1])
    level_audits = [{} for _ in page.bands]
    if interface_mode == 'qianxing':
        level_started = time.perf_counter()
        levels, level_audits = read_qianxing_levels(frame, page.bands)
        page.field_ocr_seconds += time.perf_counter()-level_started
        row_outputs = [value for i in range(len(page.bands))
                       for value in (outputs[2+2*i], levels[i], outputs[3+2*i])]
        outputs = list(outputs[:2])+row_outputs
    refine_started = time.perf_counter()
    names, name_audit = nickname_ocr.refine(
        [crop(frame, rect) for rect, _, _ in regions],
        [outputs[2+3*i] for i in range(len(page.bands))], should_continue)
    for i, (rect, _, known) in enumerate(regions):
        if interface_mode == 'qianxing' and not known and rect[2] < NAME[2]:
            # A possibly cut-off pill must not turn its visible letters into a
            # nickname, or make a cropped nickname fragment look confirmed.
            names[i] = ('', 0.)
            name_audit[i] = {'method': 'rejected', 'image_only': True,
                            'reason': '昵称与标签边界未完全确认，保留昵称裁片待核查'}
    page.name_refine_seconds = time.perf_counter()-refine_started
    if should_continue is not None and not should_continue():
        page.issues.append('识别已停止')
        return page
    signature_started = time.perf_counter()
    signatures = read_signatures(frame, page.bands)
    tags = read_tags(frame, regions, interface_mode)
    page.field_ocr_seconds += time.perf_counter()-signature_started
    scale_x, scale_y = image.shape[1]/1920, image.shape[0]/1080
    actual_bands = None
    for i, band in enumerate(page.bands):
        fields = outputs[2+3*i:5+3*i]
        fields[0] = names[i]
        items = []
        for (text, confidence), field in zip(fields, (NAME, *detail_slots)):
            x1, y1, x2, y2 = field
            box = [[x1-NAME[0], y1], [x2-NAME[0], y1],
                   [x2-NAME[0], y2], [x1-NAME[0], y2]]
            items.append(ocr_engine.OcrResult(text, confidence, box))
        name_image = crop(frame, regions[i][0])
        status_audit = (qianxing_activity_evidence(frame, band, *fields[2])
                        if interface_mode == 'qianxing' else {})
        pending_evidence = (name_audit[i].get('image_only') or
                            nickname_ocr.tight_rect(nickname_ocr.ink_mask(name_image)) is not None)
        if name_audit[i].get('method') == 'rejected' and not pending_evidence:
            # A blank nickname may be intentional, but a featureless/covered
            # frame is not a friend. Require this card's real top/bottom edges.
            if actual_bands is None:
                from app.friend_screen import detect_card_bands
                actual_bands = (detect_card_bands(frame, min_top=106) if interface_mode == 'qianxing'
                                else detect_card_bands(frame))
            pending_evidence = any(abs(real.top-band.top) <= 8 and abs(real.bottom-band.bottom) <= 8
                                   for real in actual_bands)
        entry = parser.parse_card(items, band.height,
                                  int(left_offset+194*scale_x),
                                  int(top_offset+band.center*scale_y),
                                  allow_pending_name=(name_audit[i].get('method') == 'rejected'
                                                      and bool(pending_evidence)),
                                  interface_mode=interface_mode,
                                  qianxing_level_absent=level_audits[i].get('kind') == 'unset',
                                  qianxing_activity_verified=bool(status_audit.get('verified')),
                                  qianxing_activity_color=status_audit.get('color'))
        signature_image = crop(frame, field_rect(band, SIGNATURE))
        if entry is not None:
            entry.signature, entry.signature_confidence = signatures[i]
            entry.signature_pending = signature_read_pending(signature_image, signatures[i])
            entry.tags = tags[i]
        issue = ('昵称待人工核查，已保留原图' if entry and entry.nickname_pending else
                 '' if entry else '字段校验失败: ' + repr(fields))
        page.observations.append(CardObservation(
            band, entry, name_image,
            crop(frame, (158, band.top+10, 232, band.top+105)), fields, issue, name_audit[i],
            [crop(frame, rect) for rect in regions[i][1]], level_audits[i], status_audit, signature_image))
        page.texts.append('卡片 %d-%d: %s' % (band.top, band.bottom,
                          ' | '.join('%s(%.3f)' % value for value in fields)))
        if signatures[i][0] is not None:
            page.texts.append('签名: %s(%.3f)' % signatures[i])
        if tags[i]:
            page.texts.append('标签: '+tags[i])
        if entry is None:
            page.issues.append('卡片 %d-%d %s' % (band.top, band.bottom, issue))
        else:
            page.entries.append(entry)
    if not page.bands and page.expected_total != 0:
        page.issues.append('未检测到完整的好友横条')
    page.ocr_seconds = time.perf_counter()-started
    return page


def visual_similarity(a, b):
    """Match at native aspect ratio, allowing only a small local translation.

    Squashing 74x95 avatars to 256x32 magnified a one-pixel horizontal offset
    while erasing vertical precision. The measured hover shift is up to eight
    client pixels; the row itself must still pass the separate geometry check.
    """
    if a is None or b is None or not a.size or not b.size:
        return 0.
    aa = cv2.cvtColor(a, cv2.COLOR_RGB2GRAY)
    bb = cv2.cvtColor(b, cv2.COLOR_RGB2GRAY)
    h, w = aa.shape
    scale = min(1., 640/max(h, w))
    size = (max(1, round(w*scale)), max(1, round(h*scale)))
    aa = cv2.resize(aa, size, interpolation=cv2.INTER_AREA)
    bb = cv2.resize(bb, size, interpolation=cv2.INTER_AREA)
    h, w = aa.shape
    dx, dy = min(max(1, round(8*scale)), (w-1)//4), min(max(1, round(3*scale)), (h-1)//4)
    template = aa[dy:h-dy or h, dx:w-dx or w]
    if not template.size or float(template.std()) < 1.:
        return 0.
    scores = cv2.matchTemplate(bb, template, cv2.TM_CCOEFF_NORMED)
    return max(-1., min(1., float(scores.max())))


def nickname_ink_geometry(image):
    """Small reusable metadata for the existing complete-ink geometry gate."""
    mask = nickname_ocr.ink_mask(image)
    return {'rect': nickname_ocr.tight_rect(mask), 'area': int(np.count_nonzero(mask))}


def compatible_nickname_geometry(a, b):
    """Reject exactly the shapes rejected by nickname_ink_similarity."""
    if a['rect'] is None or b['rect'] is None:
        return False
    (ax, ay, ar, ab), (bx, by, br, bb) = a['rect'], b['rect']
    aw, ah, bw, bh = ar-ax, ab-ay, br-bx, bb-by
    return (min(a['area'], b['area']) >= 16
            and .90 <= aw/bw <= 1.11 and .90 <= ah/bh <= 1.11
            and abs((ax+ar-bx-br)/2) <= 10 and abs((ay+ab-by-bb)/2) <= 4)


def nickname_ink_similarity(a, b):
    """Compare all nickname strokes when card shading changes after a hover.

    Keep both the crop geometry and the full ink span. A fragment or a blank
    region cannot be stretched to stand in for a complete nickname. Preserve
    grayscale edges: binarization loses antialiasing on outlined/color names.
    """
    if not compatible_nickname_geometry(nickname_ink_geometry(a), nickname_ink_geometry(b)):
        return 0.
    foregrounds = []
    for image in (a, b):
        gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        contrast = np.clip(cv2.medianBlur(gray, 31).astype(np.int16)-gray, 0, 255).astype(np.uint8)
        # A subpixel blur models rasterization changes without discarding thin
        # strokes; it is paired with a stricter foreground cutoff in UidPass.
        contrast = cv2.GaussianBlur(contrast, (0, 0), .6)
        foregrounds.append(cv2.cvtColor(contrast, cv2.COLOR_GRAY2RGB))
    # A Qianxing tag follows the nickname when its card is hovered, changing
    # the right crop boundary. Keep the same pixel scale and top-left origin:
    # resizing unequal crops would stretch the complete letters differently.
    height = max(image.shape[0] for image in foregrounds)
    width = max(image.shape[1] for image in foregrounds)
    foregrounds = [cv2.copyMakeBorder(image, 0, height-image.shape[0],
                   0, width-image.shape[1], cv2.BORDER_CONSTANT, value=(0, 0, 0))
                   for image in foregrounds]
    return visual_similarity(*foregrounds)


def same_person(a: CardObservation, b: CardObservation):
    if a.entry and b.entry:
        mode = getattr(a.entry, 'interface_mode', 'normal')
        if mode != getattr(b.entry, 'interface_mode', 'normal'):
            return False
        level_field = 'qianxing_level' if mode == 'qianxing' else 'level'
        if getattr(a.entry, level_field, None) != getattr(b.entry, level_field, None):
            return False
        if mode == 'qianxing':
            if a.entry.tags is not None and b.entry.tags is not None and a.entry.tags != b.entry.tags:
                return False
            if a.tag_images and b.tag_images:
                if (len(a.tag_images) != len(b.tag_images)
                        or any(visual_similarity(x, y) < .90 for x, y in zip(a.tag_images, b.tag_images))):
                    return False
    if (a.entry and b.entry and a.entry.nickname_pending and b.entry.nickname_pending
            and all(nickname_ocr.tight_rect(nickname_ocr.ink_mask(image)) is None
                    for image in (a.name_image, b.name_image))):
        # This only validates an overlapping row during one traversal. Stable
        # cross-scan identity still requires UID; a blank name cannot supply it.
        return visual_similarity(a.avatar_image, b.avatar_image) >= .95
    pending = (a.entry and a.entry.nickname_pending) or (b.entry and b.entry.nickname_pending)
    avatar = visual_similarity(a.avatar_image, b.avatar_image)
    if (visual_similarity(a.name_image, b.name_image) >= .91
            and avatar >= (.85 if pending else .90)):
        return True
    # Moving a complete card out of the viewport's bottom fade changes its
    # background much more than its nickname strokes. This is only evidence
    # for a consecutive overlapping row, never a persistent UID association.
    # Require the complete ink geometry and account for every added/deleted
    # stroke as well as a stronger avatar match before accepting the fallback.
    return (avatar >= .95 and nickname_ink_similarity(a.name_image, b.name_image) >= .97
            and nickname_ocr.whole_nickname_foreground_agrees(a.name_image, b.name_image))


def stationary_view(before, after, interface_mode='normal'):
    """Prove that complete friends stayed put before retrying an input.

    A fixed viewport fade cannot prove zero movement. Require matching names
    and avatars on at least three complete rows and reject a moving thumb.
    """
    old_thumb, new_thumb = scrollbar(before), scrollbar(after)
    if old_thumb and new_thumb and abs(old_thumb.top-new_thumb.top) > 1:
        return False
    old_rows = bands(before, interface_mode=interface_mode)
    new_rows = bands(after, interface_mode=interface_mode)
    if min(len(old_rows), len(new_rows)) < 3:
        return False
    confirmed = 0
    for old in old_rows:
        matches = [row for row in new_rows if abs(old.top-row.top) <= 3
                   and abs(old.bottom-row.bottom) <= 3]
        if len(matches) != 1:
            continue
        new = matches[0]
        old_name = crop(before, nickname_rect(before, old, interface_mode))
        new_name = crop(after, nickname_rect(after, new, interface_mode))
        if (nickname_ink_similarity(old_name, new_name) >= .95
                and visual_similarity(crop(before, (158, old.top+10, 232, old.top+100)),
                                      crop(after, (158, new.top+10, 232, new.top+100))) >= .93):
            confirmed += 1
    return confirmed >= max(3, len(old_rows)-1)


def scroll_displacement(before, after, expected=None, interface_mode='normal'):
    """Measure a downward browse using the shared visible row fragment.

    Returns (positive pixels, correlation). Image correspondence also detects
    no movement or reversed movement without relying on OCR spelling.
    """
    a = cv2.cvtColor(before, cv2.COLOR_RGB2GRAY)
    b = cv2.cvtColor(after, cv2.COLOR_RGB2GRAY)
    signature_planes = None

    def match(rect, search_top=112, *, near=None, unique=False, planes=None):
        left, top, right, bottom = rect
        source, target = (a, b) if planes is None else planes
        template = source[top:bottom, left:right]
        if float(template.std()) < 3:
            return 0., 0.
        # The hovered card grows horizontally; the content is not always at x=0.
        search = target[search_top:958, left-10:right+10]
        scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        distances = top-search_top-np.arange(scores.shape[0])
        if expected is not None:
            # The thumb only bounds the search. It cannot by itself prove a
            # person's identity, but prevents confusing repeated row borders.
            plausible = np.abs(distances-expected) <= 60
            if not plausible.any():
                return 0., 0.
            scores = np.where(plausible[:, None], scores, -1.)
        if near is not None:
            plausible = np.abs(distances-near) <= 3
            if not plausible.any():
                return 0., 0.
            scores = np.where(plausible[:, None], scores, -1.)
        _, score, _, point = cv2.minMaxLoc(scores)
        pixels = float(top-search_top-point[1])
        if unique:
            # Repeated avatars are normal. The nickname must anchor the row;
            # another equally plausible nickname at a different height is not
            # disambiguated by choosing the avatar's global best match.
            other_rows = np.abs(distances-pixels) > 6
            if other_rows.any() and score-float(scores[other_rows].max()) < .06:
                return pixels, 0.
        return pixels, float(score)

    def signature_position(band):
        """A separate line can locate a row when its name and avatar repeat.

        Match visible signature strokes, not the card's smooth background.
        This is local motion evidence only, never a replacement for friend UID.
        """
        nonlocal signature_planes
        rect = (SIGNATURE[0], band.top+SIGNATURE[1], 1360, band.top+SIGNATURE[3])
        mask = nickname_ocr.ink_mask(crop(before, rect))
        ink_rect = nickname_ocr.tight_rect(mask)
        if ink_rect is None or np.count_nonzero(mask) < 40:
            return 0., 0.
        x1, y1, x2, y2 = ink_rect
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        letters = sum(4 <= area and 5 <= height <= 30 and width <= 45
                      for _, _, width, height, area in stats[1:])
        if letters < 3 or x2-x1 < 25 or not 7 <= y2-y1 <= 30:
            return 0., 0.
        if signature_planes is None:
            signature_planes = []
            for gray in (a, b):
                plane = np.zeros_like(gray)
                strip = gray[:, SIGNATURE[0]-10:1370]
                foreground = np.clip(cv2.medianBlur(strip, 31).astype(np.int16)-strip, 0, 255).astype(np.uint8)
                plane[:, SIGNATURE[0]-10:1370] = cv2.GaussianBlur(foreground, (0, 0), .6)
                signature_planes.append(plane)
        return match(rect, unique=True, planes=signature_planes)

    primary = match((160, 878, 835, 945), unique=expected is None)
    if primary[1] >= .85 and abs(primary[0]) >= 30 and expected is not None:
        return primary
    # A ~770px move clips the top of the fixed 878px template at the header,
    # even though all six next cards are complete. Match only its visible lower
    # part, constrained by a measured thumb (the +/-60px interval excludes the
    # adjacent 125px row). Require two crops to agree at the normal .85 cutoff;
    # short fragments without this independent displacement bound are unsafe.
    if expected is not None and expected > 700:
        fragments = [match((160, top, 835, 945)) for top in (898, 904, 910)]
        reliable = [value for value in fragments if value[1] >= .85]
        if len(reliable) >= 2 and max(v[0] for v in reliable)-min(v[0] for v in reliable) <= 2:
            return max(reliable, key=lambda value: value[1])
    # The bottom fade is fixed to the viewport, not to the scrolling card. A
    # fragment touching that fade changes appearance after moving upwards,
    # even while several complete shared cards remain clearly visible. Confirm
    # a nickname AND its avatar at the same displacement instead of lowering
    # the threshold on that faded strip or trusting repeated borders/statuses.
    candidates = []
    for band in bands(before, interface_mode=interface_mode)[-3:]:
        name = match(nickname_rect(before, band, interface_mode), unique=True)
        witness = 1.
        if name[1] < .85:
            signature = signature_position(band)
            if signature[1] < .90:
                continue
            # A signature from another row cannot supply a missing nickname.
            # Both the entire name and avatar must confirm its position.
            name = match(nickname_rect(before, band, interface_mode), near=signature[0])
            if name[1] < .85 or abs(name[0]-signature[0]) > 3:
                continue
            witness = signature[1]
        avatar = match((158, band.top+10, 232, band.top+100), near=name[0])
        if min(name[1], avatar[1]) >= .85 and abs(name[0]-avatar[0]) <= 3:
            candidates.append(((name[0]+avatar[0])/2, min(name[1], avatar[1], witness)))
    if candidates:
        best = max(candidates, key=lambda value: value[1])
        if all(abs(value[0]-best[0]) <= 3 for value in candidates):
            return best
        return 0., 0.  # Conflicting complete friends cannot be overruled by a background strip.
    if primary[1] >= .85 and abs(primary[0]) >= 30:
        return primary
    # Partially visible rows also have repeated avatars. Locate their text
    # first and check the portrait at THAT position, using two agreeing crops.
    # These fragments prove motion only; clipped rows are never archived here.
    fragments = []
    for top in (906, 916):
        text = match((275, top, 830, 944), search_top=98, unique=True)
        if text[1] < .85:
            continue
        avatar = match((150, top, 265, 944), search_top=98, near=text[0])
        if min(avatar[1], text[1]) >= .85 and abs(avatar[0]-text[0]) <= 3:
            fragments.append(((avatar[0]+text[0])/2, min(avatar[1], text[1])))
    if len(fragments) == 2 and abs(fragments[0][0]-fragments[1][0]) <= 2:
        return max(fragments, key=lambda value: value[1])
    return primary

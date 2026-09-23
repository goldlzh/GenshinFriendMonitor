"""Retry weak nickname fields, with optional glyphs from the local game font.

The glyph cache is derived locally; neither the game font nor a friend-name
dictionary is shipped. Matching must explain the complete ink region. A high
OCR score for one surviving symbol is not enough to accept a truncated name.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import itertools
import json
import os
from pathlib import Path
import threading
import unicodedata

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app import ocr_engine
from app.config import CONFIG
from app.logger import logger
from app.parser import nickname_requires_image

_font_path = None
_lock = threading.Lock()
_cache_version = 'nickname-glyph-v2'


def configure_game_font(hwnd=None):
    """Find the font beside the selected game's executable, without UI input."""
    global _font_path
    path = Path(CONFIG.nickname_font_path) if CONFIG.nickname_font_path else None
    if path is None and hwnd is not None:
        from app.window import executable_path
        exe = executable_path(hwnd)
        if exe:
            root = Path(exe)
            path = root.parent/(root.stem+'_Data')/'StreamingAssets/MiHoYoSDKRes/HttpServerResources/font/zh-cn.ttf'
    _font_path = path if path is not None and path.is_file() else None


def ink_mask(image):
    """Dark/blue nickname strokes against the card, including detached accents."""
    if image is None or not image.size:
        return np.zeros((0, 0), np.uint8)
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    mask = ((cv2.medianBlur(gray, 31).astype(np.int16)-gray) > 25).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    good = [i for i, s in enumerate(stats) if i and s[4] >= 3 and s[3] >= 2]
    return np.isin(labels, good).astype(np.uint8)*255


def tight_rect(mask, padding=0):
    ys, xs = np.where(mask > 32)
    if not len(xs):
        return None
    return (max(0, int(xs.min())-padding), max(0, int(ys.min())-padding),
            min(mask.shape[1], int(xs.max())+1+padding),
            min(mask.shape[0], int(ys.max())+1+padding))


def prepare(image):
    """Remove long blank tails before CTC recognition, retaining every stroke."""
    rect = tight_rect(ink_mask(image), padding=5)
    if rect is None:
        return image
    x1, y1, x2, y2 = rect
    return cv2.copyMakeBorder(image[y1:y2, x1:x2], 4, 4, 4, 4,
                              cv2.BORDER_CONSTANT, value=(235, 235, 235))


def _feature(mask):
    rect = tight_rect(mask)
    if rect is None:
        return None
    x1, y1, x2, y2 = rect
    patch = mask[y1:y2, x1:x2]
    vector = cv2.resize(patch, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32).ravel()/255
    vector /= max(1e-8, float(np.linalg.norm(vector)))
    return vector, np.array(patch.shape, np.float32)


def _whole_masks_agree(a, b):
    """Account for every visible stroke, allowing one pixel of rasterization."""
    masks = [a, b]
    if any(not np.any(mask) for mask in masks):
        return False
    height, width = (max(mask.shape[axis] for mask in masks) for axis in (0, 1))
    masks = [cv2.copyMakeBorder(mask, 0, height-mask.shape[0], 0, width-mask.shape[1],
                               cv2.BORDER_CONSTANT, value=0) for mask in masks]
    search = cv2.copyMakeBorder(masks[1], 4, 4, 10, 10, cv2.BORDER_CONSTANT, value=0)
    _, _, _, point = cv2.minMaxLoc(cv2.matchTemplate(search, masks[0], cv2.TM_CCORR_NORMED))
    aligned = search[point[1]:point[1]+height, point[0]:point[0]+width]
    for original, other in ((masks[0], aligned), (aligned, masks[0])):
        unexplained = ((original > 0) &
                       (cv2.dilate(other, np.ones((3, 3), np.uint8)) == 0)).astype(np.uint8)
        _, _, stats, _ = cv2.connectedComponentsWithStats(unexplained)
        if any(stat[cv2.CC_STAT_AREA] >= 4 for stat in stats[1:]):
            return False
    return True


def whole_nickname_foreground_agrees(a, b):
    """Reject added/deleted strokes hidden by a high whole-line correlation."""
    return _whole_masks_agree(ink_mask(a), ink_mask(b))


class FontMatcher:
    def __init__(self, path, cache_dir):
        from fontTools.ttLib import TTFont
        font_dir = Path(os.environ.get('WINDIR', 'C:/Windows'))/'Fonts'
        paths = [Path(path)]+[p for p in (font_dir/'arial.ttf', font_dir/'Nirmala.ttc') if p.is_file()]
        self.fonts = [ImageFont.truetype(str(p), 48) for p in paths]
        self.font = self.fonts[0]
        self.font_lookup, unique = {}, {}
        for index, font_path in enumerate(paths):
            with TTFont(font_path, fontNumber=0) as ttf:
                cmap = ttf.getBestCmap()
            for code, glyph in sorted(cmap.items()):
                if code in self.font_lookup or unicodedata.category(chr(code))[0] in 'CZ':
                    continue
                self.font_lookup[code] = index
                unique.setdefault((index, glyph), code)
        key = hashlib.sha256(b''.join(p.read_bytes() for p in paths)+_cache_version.encode()).hexdigest()[:20]
        cache = Path(cache_dir)/('glyphs-'+key+'.npz')
        try:
            with np.load(cache, allow_pickle=False) as data:
                self.codes, self.vectors, self.sizes = data['codes'], data['vectors'], data['sizes']
        except (OSError, ValueError, KeyError):
            logger.info('建立本机昵称字形缓存（首次使用）')
            # Keep one code point per identical font glyph. Prefer ordinary
            # ASCII/unified forms; existing OCR may preserve an equivalent form.
            codes, vectors, sizes = [], [], []
            for code in unique.values():
                feature = _feature(self.render(chr(code)))
                if feature is None:
                    continue
                vector, size = feature
                codes.append(code); vectors.append(vector); sizes.append(size)
            self.codes = np.array(codes, dtype=np.int32)
            self.vectors = np.array(vectors, dtype=np.float32)
            self.sizes = np.array(sizes, dtype=np.float32)
            cache.parent.mkdir(parents=True, exist_ok=True)
            # Opening the temporary path explicitly avoids np.savez appending a
            # suffix and keeps another instance from observing a partial archive.
            import uuid
            temp = cache.with_suffix('.'+uuid.uuid4().hex+'.tmp')
            try:
                with temp.open('wb') as f:
                    np.savez_compressed(f, codes=self.codes, vectors=self.vectors, sizes=self.sizes)
                temp.replace(cache)
            finally:
                temp.unlink(missing_ok=True)

    def render(self, text):
        lookup = getattr(self, 'font_lookup', None)
        if lookup is None or all(lookup.get(ord(c)) == 0 for c in text):
            mask = self.font.getmask(text, mode='L')
            return np.asarray(mask, dtype=np.uint8).reshape(mask.size[1], mask.size[0])
        if any(ord(c) not in lookup for c in text):
            return np.zeros((0, 0), np.uint8)
        fonts = [self.fonts[lookup[ord(c)]] for c in text]
        # Same baseline, each font's actual advance. This covers characters
        # absent from the game CJK font, such as a heart, without dropping them.
        image = Image.new('L', (int(sum(f.getlength(c) for f, c in zip(fonts, text)))+96, 128))
        draw = ImageDraw.Draw(image)
        x = 32.
        for char, font in zip(text, fonts):
            draw.text((x, 80), char, font=font, fill=255, anchor='ls')
            x += font.getlength(char)
        return np.array(image)

    def line_score(self, mask, text):
        rendered = self.render(text)
        observed_rect, rendered_rect = tight_rect(mask), tight_rect(rendered)
        if observed_rect is None or rendered_rect is None:
            return 0.
        x1, y1, x2, y2 = observed_rect
        a = mask[y1:y2, x1:x2]
        x1, y1, x2, y2 = rendered_rect
        b = rendered[y1:y2, x1:x2]
        # Nicknames are about 32px/em in the normalized 1080p client. Do not
        # stretch a lone '^' or '1' to explain a multi-character source region.
        ratios = np.array(a.shape)/(np.array(b.shape)*(32/48))
        if np.any(ratios < .72) or np.any(ratios > 1.28):
            return 0.
        # Preserve the line's width: a 32x32 feature squeezes a long nickname
        # into only a few columns per glyph. A half-pixel blur reconciles the
        # screenshot's thresholded antialiasing with the rendered font without
        # allowing any character or detached mark to disappear.
        b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
        a = cv2.GaussianBlur(a.astype(np.float32)/255, (3, 3), .5)
        b = cv2.GaussianBlur(b.astype(np.float32)/255, (3, 3), .5)
        similarity = float(np.sum(a*b)/max(1e-8, np.linalg.norm(a)*np.linalg.norm(b)))
        return similarity-.12*float(np.abs(np.log(ratios)).sum())

    def recognize(self, mask, hint='', *, audit=None):
        """Dynamic segmentation: a split radical cannot become a second friend."""
        rect = tight_rect(mask)
        if rect is None:
            return None
        x1, y1, x2, y2 = rect
        ink = mask[y1:y2, x1:x2]
        edges = np.flatnonzero(np.diff(np.r_[False, np.any(ink, axis=0), False]))
        runs = list(zip(edges[::2], edges[1::2]))
        # At client resolution two neighboring glyphs can share one column of
        # antialiasing. Add optional cuts at thin valleys only in runs wider
        # than a single glyph; the dynamic search can still join these pieces.
        projection = np.count_nonzero(ink, axis=0)
        split_runs = []
        bridge_cuts = []
        for left, right in runs:
            cuts = [left]
            if right-left > 43:
                valleys = [x for x in range(left+8, right-7)
                           if projection[x] <= 2
                           and projection[x] <= min(projection[x-1], projection[x+1])]
                previous_valley = left
                for cut in valleys:
                    if cut-previous_valley >= 8:
                        # A thin pixel column may belong to either neighbor.
                        # Keep cuts on BOTH sides of that column. Choosing only
                        # its left edge can put the heart's last pixel into the
                        # following ideograph, especially at native 1080p.
                        cuts.extend((int(cut), int(cut)+1))
                        bridge_cuts.extend((int(cut), int(cut)+1))
                        previous_valley = cut
            cuts.append(right)
            split_runs.extend(zip(cuts, cuts[1:]))
        runs = split_runs
        if bridge_cuts and audit is not None:
            audit['segmentation'] = {'ink_rect': list(rect), 'candidate_cuts': bridge_cuts}
        if not runs or len(runs) > 60:
            return None
        choices, ambiguous = {}, {}
        for start in range(len(runs)):
            for stop in range(start+1, min(len(runs), start+5)+1):
                left, right = runs[start][0], runs[stop-1][1]
                if right-left > 43:
                    break
                v, size = _feature(ink[:, left:right])
                ratios = size[None, :]/(self.sizes*(32/48))
                plausible = np.all((ratios >= .70) & (ratios <= 1.30), axis=1)
                indices = np.flatnonzero(plausible)
                if not len(indices):
                    continue
                scores = self.vectors[indices]@v-.12*np.abs(np.log(ratios[indices])).sum(axis=1)
                order = np.argsort(scores)[-6:][::-1]
                best = int(indices[order[0]])
                score = float(scores[order[0]])
                if score < .90:
                    continue
                # Different code points can have the exact same visible form.
                alternatives = [float(scores[j]) for j in order[1:]
                                if float(self.vectors[best]@self.vectors[indices[j]]) < .995]
                if alternatives and score-max(alternatives) < .035:
                    # A crop loses baseline/spacing context (e.g. Greek case).
                    # Retain a bounded set of locally plausible alternatives;
                    # do not accept one until the whole line distinguishes it.
                    selected = [best]
                    options = [(chr(int(self.codes[best])), score)]
                    for j in order[1:]:
                        candidate = int(indices[j])
                        value = float(scores[j])
                        if value < .90 or score-value >= .035:
                            continue
                        if any(float(self.vectors[k]@self.vectors[candidate]) >= .995
                               for k in selected):
                            continue
                        selected.append(candidate)
                        options.append((chr(int(self.codes[candidate])), value))
                    ambiguous[start, stop] = options
                    continue
                text = chr(int(self.codes[best]))
                choices[start, stop] = text, score
        paths = {0: ('', [], 0.)}
        for start in range(len(runs)):
            if start not in paths:
                continue
            text, scores, merit = paths[start]
            for stop in range(start+1, min(len(runs), start+5)+1):
                match = choices.get((start, stop))
                if match is None:
                    continue
                char, score = match
                value = merit+(score-.90)*(runs[stop-1][1]-runs[start][0])
                if stop not in paths or value > paths[stop][2]:
                    paths[stop] = text+char, scores+[score], value
        result = paths.get(len(runs))
        if result is None or not result[0] or len(result[0]) > 30 or self.line_score(mask, result[0]) < .88:
            result = self._resolve_ambiguous(mask, runs, choices, ambiguous, audit=audit)
            if result is None:
                return None
        elif audit is not None:
            audit.update(mode='per_glyph', text=result[0],
                         glyph_score=min(result[1]), line_score=self.line_score(mask, result[0]))
        text, scores, _ = result
        # Retain OCR's code points when rendering is identical (e.g. width
        # variants), but never preserve a visually different mistaken ideograph.
        if len(hint) == len(text):
            for i, (old, new) in enumerate(zip(hint, text)):
                a, b = _feature(self.render(old)), _feature(self.render(new))
                if a is not None and b is not None and float(a[0]@b[0]) >= .998:
                    text = text[:i]+old+text[i+1:]
        return text, min(scores)


    def _resolve_ambiguous(self, mask, runs, certain, ambiguous, *, audit=None):
        """Rank complete spellings; never prune away a possible rival to win."""
        if not ambiguous:
            return None
        choices = {edge: [value] for edge, value in certain.items()}
        choices.update(ambiguous)
        paths = {0: {'': ([], 0.)}}
        for start in range(len(runs)):
            for text, (scores, merit) in paths.get(start, {}).items():
                for stop in range(start+1, min(len(runs), start+5)+1):
                    for char, score in choices.get((start, stop), []):
                        candidate = text+char
                        if len(candidate) > 30:
                            continue
                        value = merit+(score-.90)*(runs[stop-1][1]-runs[start][0])
                        target = paths.setdefault(stop, {})
                        if candidate not in target or value > target[candidate][1]:
                            target[candidate] = (scores+[score], value)
                        if len(target) > 64:
                            if audit is not None:
                                audit.update(mode='whole_line_disambiguation', accepted=False,
                                             reason='完整字形候选过多，未裁掉其他候选后强行确认')
                            return None
        ranked = sorted([(self.line_score(mask, text), text, scores, merit)
                         for text, (scores, merit) in paths.get(len(runs), {}).items()], reverse=True)
        if audit is not None:
            audit.update(mode='whole_line_disambiguation', accepted=False,
                         ranked_candidates=[{'text': text, 'glyph_score': round(float(score), 6)}
                                            for score, text, _, _ in ranked])
        if not ranked:
            return None
        score, text, scores, merit = ranked[0]
        margin = score-ranked[1][0] if len(ranked) > 1 else None
        if score < .93 or (margin is not None and margin < .06):
            if audit is not None:
                audit['reason'] = '整行字形不足以消除单字歧义'
            return None
        if audit is not None:
            audit.update(accepted=True, glyph_score=float(score), margin=margin)
        return text, scores, merit


@lru_cache(maxsize=2)
def _load_matcher(path, cache_dir):
    return FontMatcher(path, cache_dir)


def get_matcher():
    path = Path(CONFIG.nickname_font_path) if CONFIG.nickname_font_path else _font_path
    if path is None or not path.is_file():
        return None
    try:
        with _lock:
            return _load_matcher(str(path), str(Path(CONFIG.db_path).parent/'ocr-font-cache'))
    except Exception:
        logger.exception('本机字形辅助不可用，保留 OCR 校验结果')
        return None


def add_correction(image, text, source, note):
    """Save a locally reviewed text/image pair; never learn from OCR alone."""
    if not text.strip() or len(text) > 30 or tight_rect(ink_mask(image)) is None:
        raise ValueError('校对需要非空昵称及原始昵称截图')
    directory = Path(CONFIG.db_path).parent/'nickname-corrections'
    directory.mkdir(parents=True, exist_ok=True)
    ok, data = cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    if not ok:
        raise ValueError('无法保存昵称校对截图')
    key = hashlib.sha256(data.tobytes()).hexdigest()[:20]
    (directory/(key+'.png')).write_bytes(data.tobytes())
    record = {'text': text, 'image': key+'.png', 'source': str(source), 'note': note}
    (directory/(key+'.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return record


@lru_cache(maxsize=4)
def _read_corrections(directory, revision):
    result = []
    for path in Path(directory).glob('*.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            image_path = path.parent/Path(record['image']).name
            bgr = cv2.imdecode(np.frombuffer(image_path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
            if bgr is None or not record['text'].strip():
                continue
            image = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            mask = ink_mask(image)
            feature = _feature(mask)
            if feature is not None:
                result.append((record['text'], feature, path.name, image, mask))
        except (OSError, ValueError, KeyError):
            logger.warning('跳过损坏的昵称校对文件: %s', path)
    return result


def reviewed_name(mask, image=None):
    directory = Path(CONFIG.db_path).parent/'nickname-corrections'
    if not directory.is_dir():
        return None
    revision = tuple(sorted((p.name, p.stat().st_mtime_ns) for p in directory.iterdir()))
    feature = _feature(mask)
    if feature is None:
        return None
    vector, size = feature
    matches = []
    for text, (reference, reference_size), source, reference_image, reference_mask in _read_corrections(str(directory), revision):
        shape_error = abs(float(np.log((size[1]/size[0])/(reference_size[1]/reference_size[0]))))
        if shape_error > .04 or not _whole_masks_agree(mask, reference_mask):
            continue
        score = min(1., float(vector@reference))
        if score >= .975:
            matches.append((text, score, source))
        elif image is not None:
            # Tight-box rescaling magnifies a one-pixel height change. Compare
            # the complete original strokes without stretching that box. This
            # only restores reviewed text; the workflow still proves UID/owner.
            from app.fast_layout import nickname_ink_similarity
            score = nickname_ink_similarity(image, reference_image)
            if score >= .985:
                matches.append((text, score, source))
    if len({m[0] for m in matches}) == 1:
        return max(matches, key=lambda m: m[1])
    return None


def punctuation_candidate(matcher, mask, candidates, *, original=None, audit=None):
    """Resolve bounded punctuation variants, supported by the complete image.

    Two OCR reads must agree on every other character and mark position. Strong
    whole-line geometry can corroborate weaker OCR, but never add missing marks
    or substitute letters/digits. Ordinary bracket validation stays compatible.
    """
    detail = audit if audit is not None else {}
    def reject(reason):
        detail.update(accepted=False, reason=reason)
        return None
    if matcher is None:
        return reject('未找到可用的本机字库')
    if len(candidates) < 2:
        return reject('不足两次独立复读')
    low, high = min(s for _, s in candidates), max(s for _, s in candidates)
    three_read_consensus = False
    if low < .85:
        reads = ([original]+list(candidates)) if original is not None else list(candidates)
        supporting = [score for _, score in reads if score >= .85]
        # A weaker third read must not veto two strong, exactly agreeing reads.
        # This route requires all three complete strings to agree, including
        # punctuation, and the stronger whole-image checks below still apply.
        if len(reads) < 3 or len({text for text, _ in reads}) != 1 or len(supporting) < 2:
            return reject('没有两次达到 0.85 且文字一致的识别结果')
        low, high = min(supporting), max(supporting)
        three_read_consensus = True
        detail['three_read_consensus'] = {'reads': reads, 'supporting_reads': len(supporting)}
    groups = ('(（', ')）', '.。．')
    variants = {char: group for group in groups for char in group}
    table = str.maketrans({char: group[0] for group in groups for char in group})
    texts = {text.translate(table) for text, _ in candidates}
    if len(texts) != 1:
        return reject('候选的正文、标点数量或位置不一致')
    text = next(iter(texts))
    if not 1 <= sum(char in variants for char in text) <= 4:
        return reject('没有可校验的标点，或标点组合超过限额')
    ranked = sorted(((matcher.line_score(mask, candidate), candidate)
                     for candidate in map(''.join, itertools.product(
                         *(variants.get(char, char) for char in text)))), reverse=True)
    detail['ranked_variants'] = [{'text': t, 'glyph_score': round(float(s), 6)}
                                 for s, t in ranked]
    score, text = ranked[0]
    margin = score-ranked[1][0]
    # Existing strong OCR can use the original whole-image limits. When OCR
    # fluctuates below them, require stronger image evidence and separation.
    ocr_supported = (not three_read_consensus and (low >= .90 or high >= .95)
                     and score >= .88 and margin >= .035)
    image_supported = score >= .93 and margin >= .06
    if not (ocr_supported or image_supported):
        return reject('完整字形分数或与次选的差距不足')
    rule = ('three_reads_and_whole_glyph' if three_read_consensus else
            'strong_whole_glyph' if image_supported and not ocr_supported else 'ocr_and_glyph')
    detail.update(accepted=True, rule=rule, glyph_score=float(score), margin=float(margin))
    return text, min(score, low)


def has_large_ink_gap(mask):
    """Only flag an unusually empty internal span, not normal glyph spacing.

    OCR may silently erase spaces. A gap exceeding three quarters of the full
    ink height (and at least 20 client pixels) is evidence to retain the image;
    smaller invisible spaces cannot be inferred reliably from a bitmap.
    """
    rect = tight_rect(mask)
    if rect is None:
        return False
    x1, y1, x2, y2 = rect
    columns = np.any(mask[y1:y2, x1:x2] > 32, axis=0)
    edges = np.flatnonzero(np.diff(np.r_[False, ~columns, False]))
    return any(right-left > max(20, .75*(y2-y1))
               for left, right in zip(edges[::2], edges[1::2]))


def refine(images, outputs, should_continue=None, *, retry_ocr=True):
    """Check complete nickname evidence, then batch only questionable names."""
    result = list(outputs)
    audit = [{} for _ in images]
    masks = {}
    image_only = set()
    for i, image in enumerate(images):
        masks[i] = ink_mask(image)
        if nickname_requires_image(outputs[i][0]) or has_large_ink_gap(masks[i]):
            image_only.add(i)
            result[i] = ('', 0.)
            audit[i] = {'method': 'rejected', 'original': outputs[i],
                        'reason': '昵称含空白、不可显示字符或异常空白间隔，保留原图',
                        'image_only': True}
            continue
        correction = reviewed_name(masks[i], image=image)
        if correction is not None:
            text, score, source = correction
            if nickname_requires_image(text):
                image_only.add(i)
                result[i] = ('', 0.)
                audit[i] = {'method': 'rejected', 'reason': '校对昵称含空白或不可显示字符，保留原图',
                            'image_only': True}
                continue
            result[i] = (text, score)
            audit[i] = {'method': 'local_review', 'original': outputs[i],
                        'result': result[i], 'reference': source, 'glyph_similarity': score}
    if not retry_ocr:
        # Quick scans keep the first field read or an already reviewed image.
        # Uncertain names remain useful image observations; neither another
        # OCR variant nor font recognition should delay scanning the next row.
        for i, (text, score) in enumerate(result):
            if i in image_only or audit[i].get('method') == 'local_review':
                continue
            if (not text or not .90 <= score <= 1. or not CONFIG.nickname_min_length <= len(text) <= 30
                    or tight_rect(masks[i]) is None):
                result[i] = ('', 0.)
                audit[i] = {'method': 'rejected', 'original': outputs[i],
                            'quick': True, 'reason': '昵称单次识别不确定，保留原图等待人工填写'}
        return result, audit
    # A confidently recognized fragment still must account for the full image.
    # Only strong disagreement triggers this extra retry; ordinary rasterization
    # differences must not send every high-confidence nickname to manual review.
    visible = [i for i, (text, score) in enumerate(result)
               if i not in image_only and score >= .90 and text
               and not audit[i] and tight_rect(masks[i]) is not None]
    matcher = get_matcher() if visible else None
    incomplete = {i for i in visible if matcher is not None
                  and matcher.line_score(masks[i], result[i][0]) < .55}
    weak = [i for i, (text, score) in enumerate(result)
            if i not in image_only and (not text or score < .90 or i in incomplete)]
    if not weak:
        return result, audit
    matcher = matcher or get_matcher()
    variants, owners = [], []
    for i in weak:
        mask = masks[i]
        rect = tight_rect(mask, padding=5)
        if rect is None:
            result[i] = ('', 0.)
            audit[i] = {'method': 'rejected', 'reason': '昵称槽没有可确认的笔画'}
            continue
        x1, y1, x2, y2 = rect
        tight = images[i][y1:y2, x1:x2]
        binary = cv2.cvtColor(255-mask[y1:y2, x1:x2], cv2.COLOR_GRAY2RGB)
        variants.extend([cv2.copyMakeBorder(tight, 8, 8, 8, 8, cv2.BORDER_CONSTANT,
                                            value=(235, 235, 235)), binary])
        owners.extend([i, i])
    if should_continue is not None and not should_continue():
        return [('', 0.) if i in weak else value for i, value in enumerate(result)], audit
    retries = ocr_engine.recognize_lines(variants, minimum_width=320) if variants else []
    for i in weak:
        candidates = [value for owner, value in zip(owners, retries) if owner == i]
        mask = masks[i]
        if any(nickname_requires_image(text) for text, _ in candidates):
            result[i] = ('', 0.)
            audit[i] = {'method': 'rejected', 'original': outputs[i], 'retries': candidates,
                        'reason': '昵称复读含空白或不可显示字符，保留原图', 'image_only': True}
            continue
        font_audit = {}
        matched = matcher.recognize(mask, outputs[i][0], audit=font_audit) if matcher else None
        detail = {'original': outputs[i], 'retries': candidates}
        punctuation_audit = {}
        if matched is not None:
            result[i] = matched
            detail.update(method='local_font', result=matched)
        elif (punctuation := punctuation_candidate(matcher, mask, candidates, original=outputs[i], audit=punctuation_audit)) is not None:
            result[i] = punctuation
            detail.update(method='font_punctuation', result=punctuation)
        else:
            consensus = (len(candidates) >= 2 and len({t for t, _ in candidates}) == 1
                         and min(s for _, s in candidates) >= .85 and max(s for _, s in candidates) >= .97)
            # Tiny round punctuation loses a larger fraction of its pixels when
            # rasterized at 1080p. Require independent OCR agreement as well as
            # full-line geometry before accepting the lower raster similarity.
            accepted = [(text, score) for text, score in candidates if text and score >= .90
                        and (matcher.line_score(mask, text) >= (.86 if consensus else .90) if matcher else
                             bool(outputs[i][0]) and len(text) >= len(outputs[i][0])
                             and sum(other == text for other, _ in candidates) >= 2)]
            if accepted:
                result[i] = max(accepted, key=lambda item: item[1])
                detail.update(method='tight_ocr', result=result[i])
            else:
                # A weaker detector pass must not later silently replace this
                # rejected nickname by one remaining digit or accent.
                result[i] = ('', 0.)
                detail.update(method='rejected', reason='昵称重识别未通过完整字形校验')
        if font_audit:
            detail['font_check'] = font_audit
        if punctuation_audit:
            detail['punctuation_check'] = punctuation_audit
        if detail.get('method') == 'rejected' and matcher is not None:
            detail['font_candidates'] = [
                {'text': text, 'glyph_score': round(float(matcher.line_score(mask, text)), 6)}
                for text in dict.fromkeys(t for t, _ in [outputs[i]]+candidates if t)]
        audit[i] = detail
    return result, audit

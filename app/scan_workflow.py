"""Quick roster collection followed by UID enrollment only when necessary.

Positions exist only within one traversal for coverage checking. Persistent
identity, rename detection and history updates always use confirmed UIDs.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
import re
from statistics import median
import time
import unicodedata

import cv2
import numpy as np

from app import fast_layout as layout, input as game_input, ocr_engine, profile_layout
from app.config import CONFIG
from app.database import now_str
from app.fast_scan import FastScan, RosterChanged, ScanIncomplete, _decode, _png
from app.logger import logger
from app.roster_comparison import compare_rosters, valid_uid_roster
from app.scan_retention import retained_scan_round


def names(rows):
    return Counter(r['nickname'] for r in rows if not r.get('nickname_pending'))


def signature_key(value):
    """Only a complete nonempty signature can corroborate a rename."""
    text = unicodedata.normalize('NFC', value or '').strip()
    if not text or '\ufffd' in text or text.endswith(('…', '...')):
        return ''
    return re.sub(r'\s+', ' ', text)


def observed_signature(obs):
    entry = obs.entry
    return (signature_key(getattr(entry, 'signature', None))
            if getattr(entry, 'signature_confidence', 0.) >= .90 else '')


def observed_level(entry):
    return (getattr(entry, 'qianxing_level', None)
            if getattr(entry, 'interface_mode', 'normal') == 'qianxing' else entry.level)


def _blank_nickname(image):
    from app.nickname_ocr import ink_mask
    return image is not None and image.size > 0 and not np.any(ink_mask(image))


def _whole_nickname_foreground_agrees(a, b):
    """Reject added/deleted strokes hidden by a high whole-line correlation."""
    from app.nickname_ocr import whole_nickname_foreground_agrees
    return whole_nickname_foreground_agrees(a, b)


def roster_matches(left, right):
    """Compare unordered coverage; unread names require matching name+avatar images.

    A perfect matching preserves multiplicity for indistinguishable twins. This
    checks roster membership only; persistent identity is still the profile UID.
    A unique complete-image match can expose conflicting OCR spellings. Retain
    those nicknames as images only after the whole coverage check succeeds.
    """
    if len(left) != len(right):
        return False
    if len({getattr(o.entry, 'interface_mode', 'normal') for o in left+right}) > 1:
        return False
    if not any(o.entry.nickname_pending for o in left+right):
        if Counter(o.entry.nickname for o in left) == Counter(o.entry.nickname for o in right):
            return True
    left_signatures = Counter(observed_signature(o) for o in left)
    right_signatures = Counter(observed_signature(o) for o in right)
    image_scores = {}

    def strict_images(a_index, b_index):
        key = (a_index, b_index)
        if key not in image_scores:
            a, b = left[a_index], right[b_index]
            avatar = layout.visual_similarity(a.avatar_image, b.avatar_image)
            ink = (layout.nickname_ink_similarity(a.name_image, b.name_image)
                   if avatar >= .95 else 0.)
            if ink >= .97 and not _whole_nickname_foreground_agrees(a.name_image, b.name_image):
                ink = 0.
            image_scores[key] = ink, avatar
        return image_scores[key]

    candidates = []
    for a_index, a in enumerate(left):
        matches = []
        for i, b in enumerate(right):
            if observed_level(a.entry) != observed_level(b.entry):
                continue
            signature = observed_signature(a)
            same_signature = (signature and signature == observed_signature(b)
                              and left_signatures[signature] == right_signatures[signature] == 1
                              and layout.visual_similarity(a.avatar_image, b.avatar_image) >= .90)
            if same_signature:
                same = True
            elif not (a.entry.nickname_pending or b.entry.nickname_pending):
                same = a.entry.nickname == b.entry.nickname
            else:
                same = (layout.visual_similarity(a.name_image, b.name_image) >= .91
                        and layout.visual_similarity(a.avatar_image, b.avatar_image) >= .85)
                if not same:
                    ink, avatar = strict_images(a_index, i)
                    same = ink >= .97 and avatar >= .95
                if (not same and a.entry.nickname_pending and b.entry.nickname_pending
                        and _blank_nickname(a.name_image) and _blank_nickname(b.name_image)):
                    same = layout.visual_similarity(a.avatar_image, b.avatar_image) >= .95
            if same:
                matches.append(i)
        candidates.append(matches)

    # Most named friends have exact-name/signature candidates already. Compare
    # the full foreground only for otherwise unmatched OCR spellings, and never
    # use a many-to-one image match to fill a missing friend in the roster.
    image_conflicts = set()
    for a_index, a in enumerate(left):
        if candidates[a_index] or a.entry.nickname_pending:
            continue
        for b_index, b in enumerate(right):
            if (b.entry.nickname_pending or a.entry.nickname == b.entry.nickname
                    or observed_level(a.entry) != observed_level(b.entry)):
                continue
            ink, avatar = strict_images(a_index, b_index)
            if ink >= .97 and avatar >= .95:
                candidates[a_index].append(b_index)
                image_conflicts.add((a_index, b_index))
    target_counts = Counter(target for matches in candidates for target in matches)
    # Preserve original degrees so removing one ambiguous edge cannot make
    # another edge from that source look unique merely due to iteration order.
    ambiguous_sources = {a_index for a_index, _ in image_conflicts
                         if len(candidates[a_index]) != 1}
    for a_index, b_index in image_conflicts:
        if a_index in ambiguous_sources or target_counts[b_index] != 1:
            candidates[a_index].remove(b_index)
    owners = {}
    def assign(index, visited):
        for target in candidates[index]:
            if target in visited:
                continue
            visited.add(target)
            if target not in owners or assign(owners[target], visited):
                owners[target] = index
                return True
        return False
    if not all(assign(i, set()) for i in sorted(range(len(left)), key=lambda i: len(candidates[i]))):
        return False

    # Keep the decision transactional: an unsuccessful comparison must not
    # erase any text. Carry an established OCR conflict into a later UID pass,
    # so its higher-confidence spelling cannot create a false rename on commit.
    for b_index, a_index in owners.items():
        a, b = left[a_index], right[b_index]
        inherited = any(o.name_audit.get('roster_ocr_conflict') for o in (a, b))
        conflicting_text = (not a.entry.nickname_pending and not b.entry.nickname_pending
                            and a.entry.nickname != b.entry.nickname)
        if not (inherited or conflicting_text):
            continue
        if (not inherited and (len(candidates[a_index]) != 1
                               or target_counts[b_index] != 1)):
            continue
        ink, avatar = strict_images(a_index, b_index)
        if not inherited and (ink < .97 or avatar < .95):
            continue
        variants = sorted({text for o in (a, b)
                           for text in ([o.entry.nickname]
                                        + o.name_audit.get('roster_ocr_conflict', {}).get('variants', []))
                           if text})
        reason = '多轮扫描出现昵称文字分歧；保留原图待校对'
        for obs in (a, b):
            obs.entry = replace(obs.entry, nickname='', nickname_pending=True)
            obs.name_audit = dict(obs.name_audit, image_only=True,
                roster_ocr_conflict={'variants': variants, 'name_ink_similarity': ink,
                                     'avatar_similarity': avatar, 'inherited': inherited,
                                     'reason': reason})
            obs.issue = reason
    return True


def observations(scan):
    return [scan.slots[i] for i in range(scan.expected)]


def records_from_observations(items):
    return [{'id': None, 'entry': obs.entry, 'name_image': _png(obs.name_image),
             'avatar_image': _png(obs.avatar_image),
             'signature_image': (_png(obs.signature_image)
                                 if getattr(obs, 'signature_image', None) is not None else None)}
            for obs in items]


def reuse_identities(items, previous, profiles):
    """Return a one-to-one unordered mapping, or request fresh UID evidence."""
    if previous is None or not valid_uid_roster(previous['rows']):
        return None
    rows = previous['rows']
    if len(rows) != len(items):
        return None
    pictures = {p['friend_id']: p for p in profiles}
    modes = {getattr(o.entry, 'interface_mode', 'normal') for o in items}
    if len(modes) > 1:
        return None
    mode = next(iter(modes), 'normal')
    if any(pictures.get(r['id'], {}).get('interface_mode', 'normal') != mode for r in rows):
        # The two interfaces can show different avatars and level decorations.
        # Reconfirm their shared UID once before replacing visual evidence.
        return None
    decoded = {fid: (_decode(p['name_image']), _decode(p['avatar_image'])) for fid,p in pictures.items()}
    mapping = []
    bases = []
    old_signatures = Counter(signature_key(r.get('signature')) for r in rows)
    new_signatures = Counter(observed_signature(o) for o in items)
    for obs in items:
        signature = observed_signature(obs)
        signed = [r for r in rows if (signature and signature_key(r.get('signature')) == signature
                  and old_signatures[signature] == new_signatures[signature] == 1
                  and r['nickname'] != obs.entry.nickname and r['id'] in decoded
                  and layout.visual_similarity(obs.avatar_image, decoded[r['id']][1]) >= .90)]
        if len(signed) == 1:
            mapping.append(signed[0])
            bases.append('signature')
            continue
        candidates = [r for r in rows if (obs.entry.nickname_pending or r.get('nickname_pending')
                                         or r['nickname'] == obs.entry.nickname)]
        strict = len(candidates) > 1 or obs.entry.nickname_pending
        matched = []
        for row in candidates:
            if row['id'] not in decoded:
                continue
            name, avatar = decoded[row['id']]
            pending = obs.entry.nickname_pending or row.get('nickname_pending')
            if pending and row.get('qianxing_level' if mode == 'qianxing' else 'level') != observed_level(obs.entry):
                continue
            if (layout.visual_similarity(obs.name_image, name) >= (.91 if strict or pending else .86)
                    and layout.visual_similarity(obs.avatar_image, avatar) >= (.85 if pending else .90 if strict else .82)):
                matched.append(row)
        if len(matched) != 1:
            return None
        mapping.append(matched[0])
        bases.append('name_image')
    if len({r['uid'] for r in mapping}) != len(items):
        return None
    records = records_from_observations(items)
    for record, row, basis in zip(records, mapping, bases):
        record.update(id=row['id'], uid=row['uid'], match_basis=basis)
        if row.get('nickname_pending'):
            record['entry'] = replace(record['entry'], nickname='', nickname_pending=True)
    return records


class UidPass(FastScan):
    """Fresh traversal: bind each clicked card directly to its profile UID."""
    def __init__(self, scanner, expected):
        super().__init__(scanner)
        self.expected = expected
        self.phase = 'UID 采集'
        self.profile_images = {}
        self.uids = {}
        self.visible_checks = 0

    def click(self, rect, point):
        self.check()
        if self.scanner._get_rect() != rect:
            raise ScanIncomplete('点击前游戏窗口位置已改变，请重新扫描')
        if not game_input.click_client(rect, point, hwnd=self.scanner._hwnd,
                                       should_stop=self.scanner._should_stop):
            raise ScanIncomplete('UID 点击被停止或游戏失去焦点')

    def wait(self, seconds):
        if not self.scanner._wait(max(0., seconds)):
            raise ScanIncomplete('UID 采集已停止，未提交部分身份')

    def park_pointer(self, rect):
        self.check()
        if self.scanner._get_rect() != rect:
            raise ScanIncomplete('移开鼠标前游戏窗口位置已改变，请重新扫描')
        # Same neutral client-area point as the drag scanner: away from cards,
        # scrollbars and the blacklist control. Moving never clicks the game.
        if not game_input.move_client(rect, (1860, 1000), hwnd=self.scanner._hwnd,
                                      should_stop=self.scanner._should_stop):
            raise ScanIncomplete('移开鼠标被停止或游戏失去焦点')

    @staticmethod
    def visible_reference(frame, interface_mode='normal'):
        return [layout.CardObservation(band, None,
                    layout.nickname_crop(frame, band, interface_mode),
                    layout.crop(frame, (158, band.top+10, 232, band.top+105)))
                for band in layout.bands(frame, interface_mode=interface_mode)]

    @staticmethod
    def visible_scores(frame, bands, observations, interface_mode='normal'):
        rows = []
        for band, obs in zip(bands, observations):
            name = layout.nickname_crop(frame, band, interface_mode)
            avatar = layout.crop(frame, (158, band.top+10, 232, band.top+105))
            name_score = layout.visual_similarity(name, obs.name_image)
            avatar_score = layout.visual_similarity(avatar, obs.avatar_image)
            # Single-character names have far more card background than ink.
            # Use the complete foreground only with a stronger avatar match.
            ink_score = (layout.nickname_ink_similarity(name, obs.name_image)
                         if name_score < .88 and avatar_score >= .90 else None)
            name_matches = name_score >= .88 or (ink_score is not None and ink_score >= .95)
            if (not name_matches and (obs.entry is None or obs.entry.nickname_pending)
                    and _blank_nickname(name) and _blank_nickname(obs.name_image)):
                name_matches = avatar_score >= .95
            rows.append({'top': band.top, 'reference_top': obs.band.top,
                         'top_delta': abs(band.top-obs.band.top),
                         'y_shift': band.top-obs.band.top,
                         'nickname': obs.entry.nickname if obs.entry else None,
                         'name_similarity': name_score, 'name_ink_similarity': ink_score,
                         'name_matched': name_matches, 'avatar_similarity': avatar_score})
        # At the bottom the game can settle the entire page after the first
        # screenshot. Preserve ordered identity checks, but compare geometry
        # relative to a shared translation rather than obsolete screen pixels.
        common_shift = median(r['y_shift'] for r in rows) if rows else 0.
        coherent_shift = (len(rows) >= 3 and abs(common_shift) <= layout.ROW_PITCH/2
                          and all(abs(r['y_shift']-common_shift) <= 2 for r in rows))
        fixed_geometry = bool(rows) and all(r['top_delta'] <= 6 for r in rows)
        geometry = fixed_geometry or coherent_shift
        for row in rows:
            row.update(common_shift=common_shift,
                       aligned_top_delta=abs(row['y_shift']-common_shift),
                       geometry_matched=geometry,
                       translated=geometry and not fixed_geometry)
        matched = (len(bands) == len(observations) and bool(rows) and geometry
                   and all(r['name_matched'] and r['avatar_similarity'] >= .80 for r in rows))
        return matched, rows

    def await_visible(self, observations, context, *, timeout_seconds=None):
        # A fading modal or a hovered card is not evidence of a changed roster.
        # Move first, then retry fresh frames; never lower the identity cutoffs.
        self.visible_checks += 1
        events = []
        last_visible = False
        previous_positions = None
        previous_rect = None
        waiting_for_settle = False
        deadline = time.perf_counter()+timeout_seconds if timeout_seconds is not None else None
        attempts = max(5, int(timeout_seconds/.15)+1) if timeout_seconds is not None else 5
        for attempt in range(attempts):
            if attempt:
                if deadline is not None and time.perf_counter() >= deadline:
                    break
                self.wait(min(.15, max(0., deadline-time.perf_counter())) if deadline is not None else .15)
            rect, frame = self.shot()
            confirmed, count = layout.read_header(frame)
            bands = layout.bands(frame, interface_mode=self.interface_mode) if confirmed else []
            matched, rows = self.visible_scores(frame, bands, observations, self.interface_mode)
            visible_match = confirmed and count == self.expected and matched
            translated = bool(rows) and rows[0]['translated']
            positions = [band.top for band in bands]
            stable = (not translated or
                      (previous_positions is not None and previous_rect == rect
                       and len(previous_positions) == len(positions)
                       and all(abs(a-b) <= 2 for a, b in zip(previous_positions, positions))))
            ready = visible_match and stable
            waiting_for_settle = visible_match and not stable
            previous_positions = positions if visible_match else None
            previous_rect = rect if visible_match else None
            event = {'check': self.visible_checks, 'context': context, 'attempt': attempt,
                     'elapsed_seconds': round(time.perf_counter()-self.started, 3),
                     'client_rect': rect, 'title_verified': confirmed, 'total': count,
                     'band_count': len(bands), 'expected_bands': len(observations),
                     'matched': matched, 'positions_stable': stable, 'ready': ready, 'rows': rows}
            events.append(event)
            with (self.evidence/'uid-checks.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(event, ensure_ascii=False)+'\n')
            if confirmed and count is not None and count != self.expected:
                self.save_uid_failure('count-changed', frame, events)
                raise RosterChanged('UID 采集中好友总数发生变化', 'count_changed')
            if ready:
                return rect, frame, bands
            last_visible = confirmed and count == self.expected and bool(bands)
        self.save_uid_failure('visible', frame, events)
        if waiting_for_settle:
            raise ScanIncomplete('UID 点击前好友身份一致，但列表仍在移动；已保存截图，未继续点击')
        if last_visible:
            raise RosterChanged('UID 点击前可见好友持续不匹配，未沿用旧坐标；已保存逐行分数和截图')
        raise ScanIncomplete('UID 操作后未确认好友列表恢复；已保存截图，未继续点击')

    def save_uid_failure(self, suffix, frame, events):
        # Unconfirmed screenshots cannot be attached to a friend UID. Bound this
        # separate diagnostic queue to two images per account as well.
        stem = 'uid-failed-%03d-%s' % (self.visible_checks, suffix)
        directory = self.scanner.evidence_root/'uid-failures'
        directory.mkdir(parents=True, exist_ok=True)
        path = directory/('uid-failed-%020d.jpg' % time.time_ns())
        ok, data = cv2.imencode('.jpg', cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                                [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise ScanIncomplete('无法保存 UID 失败截图')
        path.write_bytes(data.tobytes())
        for old in sorted(directory.glob('uid-failed-*.jpg'))[:-2]:
            old.unlink()
        (self.evidence/(stem+'-image.json')).write_text(
            json.dumps({'image': str(path), 'retention': '账号内最近两张失败截图'}, ensure_ascii=False),
            encoding='utf-8')
        (self.evidence/(stem+'.json')).write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding='utf-8')

    def verify_visible(self, page):
        self.park_pointer(self.scanner._get_rect())
        self.wait(.12)
        return self.await_visible(page.observations, 'before_click')

    def restore_list(self, reference):
        # One ESC closes either the context menu or the profile. Poll the result
        # instead of pressing ESC again into a list that is still fading in.
        self.park_pointer(self.scanner._get_rect())
        if not game_input.press_esc(hwnd=self.scanner._hwnd):
            raise ScanIncomplete('未能关闭资料卡')
        # Start checking immediately; the former fixed pause plus its retry
        # allowance is now a deadline, preserving slow-animation recovery.
        self.await_visible(self.visible_reference(reference, self.interface_mode), 'after_close',
                           timeout_seconds=max(.6, float(CONFIG.profile_close_seconds)+.6))

    @staticmethod
    def profile_targets(before, menu, results):
        words = {profile_layout.compact(word) for word in CONFIG.view_profile_words if word}
        targets = []
        for item in results:
            if item.confidence < CONFIG.ocr_confidence or profile_layout.compact(item.text) not in words:
                continue
            # A nickname/signature containing the menu words is not a button.
            # Accept exact labels only when new pixels appeared at their bounds.
            x1 = max(0, int(min(p[0] for p in item.box))-4)
            x2 = min(1920, int(max(p[0] for p in item.box))+5)
            y1, y2 = max(0, int(item.top)-4), min(1080, int(item.bottom)+5)
            if x2 <= x1 or y2 <= y1:
                continue
            old = layout.crop(before, (x1, y1, x2, y2))
            new = layout.crop(menu, (x1, y1, x2, y2))
            difference = float(abs(old.astype('int16')-new.astype('int16')).mean())
            if difference >= 6 and layout.visual_similarity(old, new) < .80:
                targets.append(item)
        return targets

    def read_uid(self, rect, before, band, serial):
        opened = False
        error = None
        try:
            menu_checks = []
            for click_attempt in range(2):
                self.click(rect, (194+CONFIG.avatar_click_offset_x*1920/(rect[2]-rect[0]), band.center))
                opened = True
                timeout = max(.4, float(CONFIG.menu_wait_seconds)+.4)
                deadline = time.perf_counter()+timeout
                fresh_rechecks = 0
                menu_current = False
                base_attempts = max(3, int(timeout/.20)+1)
                for attempt in range(base_attempts+2):
                    if attempt:
                        self.wait(min(.20, max(0., deadline-time.perf_counter())))
                    rect, menu = self.shot()
                    started = time.perf_counter()
                    changed = profile_layout.menu_changed(before, menu)
                    results = profile_layout.read_menu(menu) if changed else []
                    targets = self.profile_targets(before, menu, results)
                    ocr_seconds = time.perf_counter()-started
                    # Detection can take longer than the entire menu timeout.
                    # Never retry an avatar or click a target using the frame
                    # from BEFORE that blocking OCR call.
                    fresh_rect, fresh = self.shot()
                    if fresh_rect != rect:
                        raise ScanIncomplete('菜单识别期间游戏窗口位置已改变，未继续点击')
                    menu_current = (profile_layout.menu_target_stable(menu, fresh, targets[0])
                                    if len(targets) == 1 else
                                    profile_layout.menu_frame_stable(menu, fresh))
                    menu_checks.append({'click_attempt': click_attempt, 'frame_attempt': attempt,
                                        'ocr_seconds': round(ocr_seconds, 4),
                                        'timeout_seconds': timeout,
                                        'menu_pixels_changed': changed,
                                        'fresh_frame_verified': menu_current,
                                        'targets': len(targets), 'ocr': [
                        {'text': r.text, 'confidence': r.confidence, 'box': r.box} for r in results]})
                    menu = fresh
                    if not menu_current:
                        # A late menu must get a fresh read even if the first
                        # OCR consumed the deadline. This allowance is bounded
                        # and sends no extra input while the pixels change.
                        fresh_rechecks += 1
                        if fresh_rechecks <= 2:
                            continue
                        targets = []
                        break
                    if (targets or time.perf_counter() >= deadline
                            or attempt+1 >= base_attempts+fresh_rechecks):
                        break
                if menu_current and len(targets) == 1:
                    break
                # Retry a missed avatar click only after proving the entire
                # visible list is still present and every row is unchanged.
                confirmed, total = layout.read_header(menu)
                bands = layout.bands(menu, interface_mode=self.interface_mode) if confirmed else []
                reference = self.visible_reference(before, self.interface_mode)
                unchanged, scores = self.visible_scores(menu, bands, reference, self.interface_mode)
                bare_list = not targets and confirmed and unchanged
                menu_checks[-1].update(list_verified=bare_list, total=total, rows=scores)
                if bare_list:
                    opened = False  # No menu opened: ESC would close the list.
                if confirmed and total is not None and total != self.expected:
                    # The workflow will navigate afresh after a roster change.
                    # Do not close a possibly already-restored list here.
                    opened = False
                    self.save_uid_failure('menu-count-%03d' % serial, menu, menu_checks)
                    raise RosterChanged('UID 菜单等待期间好友总数发生变化', 'count_changed')
                if menu_current and bare_list and total == self.expected:
                    if click_attempt == 0:
                        selected = min(range(len(reference)), key=lambda i: abs(reference[i].band.top-band.top))
                        if scores[0]['translated']:
                            rect, menu, bands = self.await_visible(reference, 'before_avatar_retry')
                        band, before = bands[selected], menu
                        self.scanner._say('未出现资料菜单，但当前可见好友已复核；补点头像一次')
                        continue
                self.save_uid_failure('menu-%03d' % serial, menu,
                    {'targets': len(targets), 'ocr': menu_checks[-1]['ocr'], 'checks': menu_checks})
                raise ScanIncomplete('无法唯一定位新弹出的“查看资料”菜单，未点击其他操作')
            (self.evidence/('uid-%03d-menu-read.json' % serial)).write_text(
                json.dumps(menu_checks, ensure_ascii=False, indent=2), encoding='utf-8')
            self.save_image('uid-%03d-menu.png' % serial, menu)
            self.click(rect, targets[0].center)
            values = []
            samples = []
            recent_frames = []
            seen_uid = None
            timeout = max(.4, float(CONFIG.profile_wait_seconds)+.4)
            profile_started = time.perf_counter()
            deadline = profile_started+timeout
            for sample in range(max(2, int(timeout/.15)+1)):
                if sample:
                    if time.perf_counter() >= deadline:
                        break
                    self.wait(min(.15, max(0., deadline-time.perf_counter())))
                _, profile = self.shot()
                started = time.perf_counter()
                result = profile_layout.read(profile)
                samples.append({'sample': sample, 'visible': result.visible,
                                'uid': result.uid, 'fields': result.fields,
                                'ocr_seconds': round(time.perf_counter()-started, 4)})
                recent_frames = (recent_frames+[profile])[-2:]
                # Fading/unreadable frames cannot count as one of the two.
                # Different readable UIDs are contradictory evidence, never a
                # majority vote salvaged by waiting for another matching pair.
                if result.uid:
                    if seen_uid is not None and result.uid != seen_uid:
                        values = [seen_uid, result.uid]
                        break
                    seen_uid = result.uid
                    values.append(result.uid)
                    if len(values) == 2:
                        break
                else:
                    values = []
            for sample, captured in enumerate(recent_frames):
                (self.evidence/('uid-%03d-crop-%d.png' % (serial, sample))).write_bytes(
                    _png(layout.crop(captured, profile_layout.UID)))
            audit = {'traversal_position': serial, 'uid_rect': profile_layout.UID,
                     'samples': samples, 'timeout_seconds': timeout,
                     'ready_seconds': round(time.perf_counter()-profile_started, 4),
                     'matched': len(values) == 2 and values[0] == values[1]}
            audit['failure_reason'] = ('' if audit['matched'] else
                'uid_conflict' if len({s['uid'] for s in samples if s['uid']}) > 1 else
                'profile_not_visible' if not any(s['visible'] for s in samples) else
                'uid_unreadable' if not any(s['uid'] for s in samples) else 'uid_not_confirmed')
            (self.evidence/('uid-%03d-read.json' % serial)).write_text(
                json.dumps(audit, ensure_ascii=False, indent=2), encoding='utf-8')
            if not audit['matched']:
                if audit['failure_reason'] == 'profile_not_visible':
                    # A missed menu click can leave the original list open.
                    # In that state ESC would close the list, not a profile.
                    confirmed, total = layout.read_header(profile)
                    if confirmed:
                        opened = False
                        if total is not None and total != self.expected:
                            self.save_uid_failure('read-%03d' % serial, profile, audit)
                            raise RosterChanged('打开资料期间好友总数发生变化', 'count_changed')
                self.save_uid_failure('read-%03d' % serial, profile, audit)
                messages = {
                    'profile_not_visible': '点击查看资料后未确认主页出现，未读取到好友 UID',
                    'uid_conflict': '资料卡两次读到不同 UID，未写入好友身份',
                    'uid_unreadable': '主页已出现，但固定 UID 区域未能识别完整号码',
                    'uid_not_confirmed': '主页 UID 尚未获得连续两帧一致确认，未写入好友身份',
                }
                raise ScanIncomplete(messages[audit['failure_reason']]+'；已保留截图')
            # Retain only the complete profile book. The account UID/focus
            # guards already checked the full frame before this archive crop.
            if getattr(CONFIG, 'save_profile_screenshots', True):
                book = profile_layout.crop_profile_book(profile)
                ok, encoded = cv2.imencode('.jpg', cv2.cvtColor(book, cv2.COLOR_RGB2BGR),
                                           [cv2.IMWRITE_JPEG_QUALITY, 90])
                if not ok:
                    raise ScanIncomplete('主页截图压缩失败，未提交本轮')
                self.profile_images[values[0]] = encoded.tobytes()
            return values[0]
        except Exception as exc:
            error = exc
            raise
        finally:
            if opened and not self.scanner._active_stop().is_set():
                try:
                    self.restore_list(before)
                except RosterChanged:
                    raise
                except Exception:
                    if error is None:
                        raise
                    logger.warning('UID 读取失败后的列表恢复也失败；保留原始错误', exc_info=True)

    def prepare_page(self, rect, frame, page):
        """Retry a rejected UID page on at most two verified fresh captures."""
        missing = [i for i, obs in enumerate(page.observations) if obs.entry is None]
        if (self.interface_mode != 'qianxing' or not page.confirmed or not missing
                or page.expected_total != self.expected):
            return rect, frame, page
        stem = 'field-recovery-%03d' % self.frame_count
        before_image, after_image = stem+'-before.png', stem+'-after.png'
        audit = {'page': self.frame_count, 'client_rect': rect,
                 'missing_page_rows': [i+1 for i in missing],
                 'reference_bands': [(o.band.top, o.band.bottom) for o in page.observations],
                 'original_fields': [{'row': i+1, 'fields': obs.fields, 'issue': obs.issue,
                                      'level_audit': obs.level_audit, 'status_audit': obs.status_audit}
                                     for i, obs in enumerate(page.observations)],
                 'before_image': before_image, 'after_image': None,
                 'retention': '原始页和最新恢复帧，每页最多两张',
                 'offset_before': self.offset, 'offset_after': self.offset,
                 'accepted': False, 'attempts': []}
        positions = None
        try:
            self.check()
            self.save_image(before_image, frame, always=True)
            self.park_pointer(rect)
            for attempt in range(2):
                self.wait(.15)
                fresh_rect, fresh = self.shot()
                if fresh_rect != rect:
                    raise ScanIncomplete('字段恢复期间游戏窗口位置已改变，未沿用旧坐标')
                self.save_image(after_image, fresh, always=True)
                audit['after_image'] = after_image
                confirmed, total = layout.read_header(fresh)
                bands = layout.bands(fresh, interface_mode=self.interface_mode) if confirmed else []
                matched, rows = self.visible_scores(fresh, bands, page.observations, self.interface_mode)
                same_page = confirmed and total == self.expected and matched
                current_positions = [band.top for band in bands]
                shift = median(row['y_shift'] for row in rows) if rows else 0.
                # A one/two-pixel edge jitter needs no extra settling sample.
                # Larger translations must recur in both available captures.
                stable = (abs(shift) <= 2 or
                          (positions is not None and len(positions) == len(current_positions)
                           and all(abs(a-b) <= 2 for a, b in zip(positions, current_positions))))
                event = {'attempt': attempt+1, 'client_rect': fresh_rect, 'title_verified': confirmed,
                         'total': total, 'same_page': same_page, 'positions_stable': stable,
                         'bands': [(b.top, b.bottom) for b in bands], 'rows': rows, 'y_shift': shift}
                audit['attempts'].append(event)
                positions = current_positions if same_page else None
                if confirmed and total is not None and total != self.expected:
                    raise RosterChanged('字段恢复期间好友总数发生变化', 'count_changed')
                if not same_page or not stable:
                    continue
                retry = layout.read_page(fresh, should_continue=self.scanner._check_pause,
                                         interface_mode=self.interface_mode)
                self.check()
                event['read'] = {'confirmed': retry.confirmed, 'total': retry.expected_total,
                                 'issues': retry.issues,
                                 'rows': [{'top': o.band.top, 'bottom': o.band.bottom,
                                           'entry': o.entry.to_dict() if o.entry else None,
                                           'fields': o.fields, 'issue': o.issue,
                                           'name_audit': o.name_audit, 'level_audit': o.level_audit,
                                           'status_audit': o.status_audit}
                                          for o in retry.observations]}
                if retry.confirmed and retry.expected_total is not None and retry.expected_total != self.expected:
                    raise RosterChanged('字段恢复识别到好友总数变化', 'count_changed')
                complete = (retry.confirmed and retry.expected_total == self.expected
                            and len(retry.observations) == len(bands)
                            and len(retry.entries) == len(bands)
                            and all(o.band == band and o.entry is not None
                                    and getattr(o.entry, 'interface_mode', 'normal') == self.interface_mode
                                    for o, band in zip(retry.observations, bands)))
                event['complete_fields'] = complete
                if not complete:
                    continue
                offset = self.offset-shift if self.origin is not None else self.offset
                self.scanner._say('必要字段已在新的完整页面恢复，继续核验 UID')
                audit.update(accepted=True, accepted_attempt=attempt+1,
                             y_shift=shift, offset_after=offset)
                # Save evidence before publishing any replacement state. The
                # normal add_page/record_page path then sees one coherent frame.
                with (self.evidence/'field-recovery.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(audit, ensure_ascii=False)+'\n')
                retry.field_recovery_shift = shift
                self.offset = offset
                return fresh_rect, fresh, retry
            raise ScanIncomplete('必要字段经两次新帧核验仍未恢复，保留原始页，未继续点击 UID')
        except Exception as error:
            audit['error'] = str(error)
            with (self.evidence/'field-recovery.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(audit, ensure_ascii=False)+'\n')
            raise

    def after_page(self, rect, frame, page, indices):
        for index, obs in zip(indices, page.observations):
            if index in self.uids:
                continue
            if obs.entry is None:
                raise ScanIncomplete('UID 采集遇到等级或状态等必要字段缺失，未猜测身份')
            rect, before, bands = self.verify_visible(page)
            band = bands[indices.index(index)]
            uid_started = time.perf_counter()
            uid = self.read_uid(rect, before, band, index+1)
            if uid in self.uids.values():
                raise RosterChanged('两个不同横条读到相同 UID，名单可能已重排')
            self.uids[index] = uid
            event = {'traversal_position': index+1, 'nickname': obs.entry.nickname, 'nickname_pending': obs.entry.nickname_pending,
                     'uid': uid, 'read_seconds': round(time.perf_counter()-uid_started, 3),
                     'elapsed_seconds': round(time.perf_counter()-self.started, 3)}
            with (self.evidence/'uids.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(event, ensure_ascii=False)+'\n')
            self.scanner._say('UID 采集 %d/%d：%s → %s（本阶段 %.1f 秒）' %
                              (len(self.uids), self.expected, obs.entry.nickname or '待人工核查昵称', uid,
                               time.perf_counter()-self.started))
        if page.observations:
            rect, frame, bands = self.verify_visible(page)
            # advance() measures the next drag from this fresh frame. Keep its
            # scroll origin in the same coordinate system after a settled shift.
            shift = median(band.top-obs.band.top for band, obs in zip(bands, page.observations))
            if shift:
                event = {'indices': indices, 'reference_tops': [o.band.top for o in page.observations],
                         'current_tops': [b.top for b in bands], 'y_shift': shift,
                         'offset_before': self.offset, 'offset_after': self.offset-shift}
                self.offset -= shift
                with (self.evidence/'uid-layout.jsonl').open('a', encoding='utf-8') as f:
                    f.write(json.dumps(event, ensure_ascii=False)+'\n')
        return rect, frame


class ScanWorkflow:
    def __init__(self, scanner, *, force_uid=False):
        self.scanner = scanner
        self.force_uid = force_uid
        self.previous = scanner.db.get_complete_roster()
        self.started = time.perf_counter()
        self.quick_seconds = 0.
        self.uid_seconds = 0.
        self.passes = []
        self.last = None
        self.restarts = 0
        self.count_changed_during_scan = False
        self.nickname_conflicts = []

    def record_nickname_conflicts(self, items, phase, scan):
        conflicts = [{'phase': phase, 'traversal_position': index+1,
                      'scan_evidence': str(scan.evidence), **obs.name_audit['roster_ocr_conflict']}
                     for index, obs in enumerate(items) if obs.name_audit.get('roster_ocr_conflict')]
        self.nickname_conflicts.extend(conflicts)
        if conflicts:
            self.scanner._say('%d 个昵称出现 OCR 文字分歧，保留原图并通过 UID 核验身份' % len(conflicts))

    def collect(self, uid=False, expected=None):
        scan = UidPass(self.scanner, expected) if uid else FastScan(self.scanner)
        self.last = scan
        self.passes.append(str(scan.evidence))
        start = time.perf_counter()
        result = scan.run(commit=False)
        elapsed = time.perf_counter()-start
        if uid:
            self.uid_seconds += elapsed
        else:
            self.quick_seconds += elapsed
        if not result.complete:
            if scan.failure_reason in ('count_changed', 'reordered'):
                raise RosterChanged(result.message, scan.failure_reason)
            raise ScanIncomplete(getattr(scan, 'failure_message', '') or result.message)
        return scan

    @retained_scan_round
    def run(self):
        comparison = {}
        error = ''
        complete = False
        current = []
        budget = max(0, min(10, int(CONFIG.roster_restart_attempts)))
        try:
            while True:
                try:
                    self.scanner._say('阶段 1：快速采集完整名单；行号只检查本轮是否漏行')
                    quick = self.collect()
                    items = observations(quick)
                    records = reuse_identities(items, self.previous, self.scanner.db.get_visual_profiles())
                    changed = (self.previous is not None and records is None and
                               (names(self.previous['rows']) != Counter(o.entry.nickname for o in items if not o.entry.nickname_pending)
                                or any(o.entry.nickname_pending for o in items)
                                or any(r.get('nickname_pending') for r in self.previous['rows'])))
                    if changed:
                        self.scanner._say('昵称或人数与上次完整名单不同：重新快速扫描确认，然后读取 UID')
                        confirmed = self.collect()
                        if not roster_matches(observations(confirmed), items):
                            raise RosterChanged('两轮快速扫描的昵称或人数仍在变化')
                        quick, items = confirmed, observations(confirmed)
                        self.record_nickname_conflicts(items, 'quick_confirmation', confirmed)
                    records = reuse_identities(items, self.previous, self.scanner.db.get_visual_profiles())
                    signature_renames = records is not None and any(
                        r.get('match_basis') == 'signature' for r in records)
                    if signature_renames:
                        self.scanner._say('昵称变化已优先按唯一签名对应；采集主页并用 UID 复核身份')
                    if (self.force_uid or self.count_changed_during_scan or records is None or signature_renames
                            or any(obs.name_audit.get('roster_ocr_conflict') for obs in items)):
                        self.scanner._say('阶段 2：逐个打开资料卡记录 UID；本阶段与快速扫描分别计时')
                        uid_scan = self.collect(uid=True, expected=quick.expected)
                        uid_items = observations(uid_scan)
                        if not roster_matches(uid_items, items):
                            raise RosterChanged('UID 采集后的昵称与快速名单不同，重新采集')
                        self.record_nickname_conflicts(uid_items, 'uid_confirmation', uid_scan)
                        records = records_from_observations(uid_items)
                        # Quick monitoring can create local friends before a UID
                        # is known. Preserve that exact local history when this
                        # explicit UID traversal confirms it; never merge names.
                        from app.quick_scan import QuickIdentities
                        local_rows = self.scanner.db.get_all_friends()
                        local_by_id = {row['id']: row for row in local_rows}
                        local_matches = QuickIdentities(local_rows,
                            self.scanner.db.get_visual_profiles()).resolve(list(enumerate(uid_items)))
                        for i, record in enumerate(records):
                            record['uid'] = uid_scan.uids.get(i)
                            match = local_matches[i]
                            candidate_id = match.get('id')
                            if candidate_id is None and match.get('rename_candidate'):
                                candidate_id = match['rename_candidate']['id']
                            old = local_by_id.get(candidate_id)
                            if old is not None and old.get('uid') in (None, '', record['uid']):
                                record['id'] = old['id']
                            picture = getattr(uid_scan, 'profile_images', {}).get(record['uid'])
                            if picture is not None:
                                record['profile_image'] = picture
                    else:
                        self.scanner._say('昵称和人数未变，沿用已确认 UID；排列顺序变化不触发 UID 重扫')
                    current = [{'uid': r.get('uid'), 'nickname': r['entry'].nickname,
                                'nickname_pending': r['entry'].nickname_pending} for r in records]
                    if not valid_uid_roster(current):
                        raise ScanIncomplete('UID 缺失或重复，未提交不完整身份名单')
                    # One final fresh header check before the atomic commit.
                    self.last.check()
                    _, frame = self.last.shot()
                    self.scanner._verify_account_frame(frame)
                    title, total = layout.read_header(frame)
                    if not title or total is None:
                        raise ScanIncomplete('提交前无法确认好友标题和人数')
                    if total != len(records):
                        raise RosterChanged('提交前好友人数又发生变化', 'count_changed')
                    # The transaction compares final identities after UID merges.
                    comparison = {}
                    stamp = now_str()
                    self.scanner.db.apply_verified_scan(records, stamp,
                        time.perf_counter()-self.started, str(self.last.evidence),
                        comparison=comparison, identity_complete=True)
                    complete = True
                    break
                except RosterChanged as changed_error:
                    self.count_changed_during_scan |= changed_error.reason == 'count_changed'
                    if self.restarts >= budget:
                        raise ScanIncomplete('名单持续变化，已用完 %d 次重扫；%s' % (budget, changed_error))
                    self.restarts += 1
                    self.scanner._say('人数或名单变化，丢弃本轮草稿并从顶部重新扫描（%d/%d）；原因：%s' %
                                      (self.restarts, budget, changed_error))
                    if not self.scanner._check_pause():
                        raise ScanIncomplete('已停止，未开始下一次重扫')
                    if not self.scanner._ensure_friend_list(self.scanner._get_rect()):
                        raise ScanIncomplete('重新扫描前未恢复好友界面')
        except Exception as exc:
            error = str(exc)
            if not isinstance(exc, ScanIncomplete):
                logger.exception('名单与 UID 工作流失败')
        return self.finish(complete, comparison, error)

    def finish(self, complete, comparison, error):
        from app.scanner import ScanResult
        elapsed = time.perf_counter()-self.started
        rows = self.scanner.db.export_rows()
        online = sum(r.get('current_status') == 'online' for r in rows)
        note = ('完成 %d 位好友：快速扫描 %.1f 秒，UID 采集 %.1f 秒，总计 %.1f 秒' %
                (len(rows), self.quick_seconds, self.uid_seconds, elapsed) if complete else
                '扫描未完成：%s；保留上次完整名单（快速 %.1f 秒，UID %.1f 秒）' %
                (error, self.quick_seconds, self.uid_seconds))
        if complete:
            if comparison.get('basis') == 'skipped':
                note += '；此前无完整名单，跳过变动比较'
            else:
                note += '；与上次完整名单比较：新增 %d，减少 %d，改名 %d' % tuple(
                    sum(v.get('count', 1) for v in comparison.get(k, [])) for k in ('added', 'removed', 'renamed'))
        pending = sum(bool(r.get('nickname_pending')) for r in rows)
        if complete and pending:
            note += '；%d 个昵称显示截图，待人工核查（双击昵称编辑）' % pending
        report = {'complete': complete, 'pending_nicknames': pending, 'quick_seconds': self.quick_seconds,
                  'uid_seconds': self.uid_seconds, 'workflow_seconds': elapsed,
                  'restarts': self.restarts, 'passes': self.passes,
                  'comparison': comparison, 'error': error, 'message': note}
        report['nickname_conflicts'] = self.nickname_conflicts
        report['account_uid'] = self.scanner.account_uid
        report['interface_mode'] = getattr(self.scanner, 'interface_mode', None)
        target = self.scanner.evidence_root/'workflow-latest.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if self.last is not None:
            (self.last.evidence/'workflow.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        self.scanner._say(note)
        return ScanResult(now_str(), len(rows), online, len(rows)-online, rows,
                          self.scanner.db.get_recent_events(limit=300), note, complete,
                          mode='uid' if self.force_uid else 'scan', quick_seconds=self.quick_seconds, uid_seconds=self.uid_seconds,
                          workflow_seconds=elapsed, uid_complete=complete,
                          restarts=self.restarts, comparison=comparison)

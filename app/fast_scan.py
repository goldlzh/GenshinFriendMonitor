"""Six-row scan with measured movement, ordered coverage and staged writes."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median
import time

import cv2
import numpy as np

from app import capture, friend_screen, fast_layout as layout, nickname_ocr, ocr_engine, parser, scroller
from app.config import CONFIG
from app.database import now_str
from app.logger import logger, ocr_dump_logger
from app.scan_retention import retained_scan_round, new_pass_directory


class ScanIncomplete(RuntimeError):
    pass


class RosterChanged(ScanIncomplete):
    """A recoverable change invalidates this whole draft, never a stored identity."""
    def __init__(self, message, reason='reordered'):
        super().__init__(message)
        self.reason = reason


def _png(image):
    ok, data = cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR),
                            [cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not ok:
        raise RuntimeError('无法保存识别证据')
    return data.tobytes()


def _decode(data):
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB) if image is not None else None


class FastScan:
    def __init__(self, scanner):
        self.scanner = scanner
        self.interface_mode = getattr(scanner, 'interface_mode', None) or 'normal'
        self.started = time.perf_counter()
        self.scan_time = now_str()
        self.slots = {}
        self.expected = None
        self.top_verified = False
        self.offset = 0.
        self.origin = None
        self.frame_count = 0
        self.frames = []
        self.nickname_reviews = []
        self.drag_bias = 0.
        self.drag_errors = []
        self.tail_probes = 0
        self.home_observations = []
        self.failure_reason = ''
        self.failure_message = ''
        self.records = []
        self.commit_enabled = True
        self.phase = '快速扫描'
        nickname_ocr.configure_game_font(scanner._hwnd)
        self.evidence = new_pass_directory(scanner.evidence_root)

    def check(self):
        active_mode = getattr(self.scanner, 'interface_mode', None)
        if active_mode is not None and active_mode != self.interface_mode:
            raise ScanIncomplete('好友界面模式已改变，本轮草稿未提交')
        if not self.scanner._check_pause():
            raise ScanIncomplete('已停止；本轮草稿未写入好友档案')
        if not self.scanner._wait_for_game_foreground():
            raise ScanIncomplete(self.scanner._navigation_error)

    def shot(self):
        self.check()
        rect = self.scanner._get_rect()
        try:
            image = capture.capture_window(self.scanner._hwnd, *rect)
        except capture.CaptureError as error:
            self.scanner._unsupported_view(str(error))
            raise ScanIncomplete(str(error)) from error
        frame = friend_screen.normalize(image)
        if frame is None:
            message = '截图不是完整的 16:9 游戏画面，已停止扫描；请调整游戏分辨率后重新开始。'
            self.scanner._unsupported_view(message)
            raise ScanIncomplete(message)
        self.scanner._verify_account_frame(frame)
        return rect, frame

    def drag(self, rect, start=None, end=None, *, distance=None):
        self.check()
        options = dict(hwnd=self.scanner._hwnd, should_stop=self.scanner._should_stop)
        if start is None:
            success = scroller.drag_six_rows(rect, distance=distance, **options)
        else:
            success = scroller.drag_client(rect, start, end, **options)
        if not success:
            raise ScanIncomplete('拖动被停止或游戏失去焦点')

    def home(self):
        def observe(rect, frame, attempt, sample):
            title_ok = layout.is_friend_list(frame)
            thumb = layout.scrollbar(frame)
            number = len(self.home_observations)
            name = 'home-%02d.png' % number
            self.save_image(name, frame)
            record = {'frame': name, 'attempt': attempt, 'sample': sample,
                      'elapsed_seconds': round(time.perf_counter()-self.started, 4),
                      'client_rect': rect, 'title_verified': title_ok,
                      'scrollbar': vars(thumb) if thumb else None,
                      'at_top': bool(thumb and thumb.at_top)}
            self.home_observations.append(record)
            with (self.evidence/'home.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False)+'\n')
            if not title_ok:
                raise ScanIncomplete('回到顶部前好友标题校验失败；已保存定位截图')
            return thumb

        rect, frame = self.shot()
        for attempt in range(3):
            # Verify title before any mouse operation, including the initial reset.
            thumb = observe(rect, frame, attempt, 0)
            stable = False
            if attempt or (thumb and thumb.at_top):
                # A drag can finish before the game presents its final frame.
                # Resample before using a stale handle position for another drag.
                for sample in range(1, 4):
                    if not self.scanner._wait(.12):
                        raise ScanIncomplete('顶部确认期间已停止')
                    rect, frame = self.shot()
                    current = observe(rect, frame, attempt, sample)
                    stable = bool(thumb and current and abs(thumb.top-current.top) <= 1
                                  and abs(thumb.bottom-current.bottom) <= 1
                                  and abs(thumb.x-current.x) <= 1)
                    thumb = current
                    if stable:
                        break
                if stable and thumb.at_top:
                    self.top_verified = True
                    return rect, frame
            if thumb is None:
                # A list that completely fits the viewport may have no thumb.
                page = self.read_home_page(frame)
                if page.confirmed and page.expected_total is not None and page.expected_total <= len(page.entries):
                    self.top_verified = True
                    return rect, frame
                self.save_image('top-unconfirmed.png', frame)
                raise ScanIncomplete('无法定位滚动条，未盲目滚动；已保存诊断截图')
            if attempt == 2:
                break
            if not stable and (attempt or thumb.at_top):
                # Do not act on a still-moving handle or accept a transient top.
                continue
            self.scanner._say('定位列表顶部：拖动右侧滚动条并核验滑块位置')
            self.drag(rect, (thumb.x, thumb.center), (thumb.x, layout.TRACK_TOP))
            rect, frame = self.shot()
        self.save_image('top-unconfirmed.png', frame)
        raise ScanIncomplete('拖动后未能确认列表顶部；已保存 home.jsonl 和定位截图')

    def read_home_page(self, frame):
        return layout.read_page(frame, interface_mode=self.interface_mode)

    def save_image(self, name, frame, *, always=False):
        if CONFIG.save_scan_evidence or always:
            (self.evidence/name).write_bytes(_png(frame))

    def recover_fields(self, frame, page):
        """Only retry rejected single-row fields; never combine neighboring rows."""
        if self.interface_mode == 'qianxing':
            if any(o.entry is None for o in page.observations):
                self.check()
                retry = layout.read_page(frame, should_continue=self.scanner._check_pause,
                                         interface_mode=self.interface_mode)
                by_band = {(o.band.top, o.band.bottom): o for o in retry.observations}
                for obs in page.observations:
                    other = by_band.get((obs.band.top, obs.band.bottom))
                    if obs.entry is None and other is not None and other.entry is not None:
                        obs.entry, obs.issue = other.entry, ''
                        obs.name_image, obs.name_audit = other.name_image, other.name_audit
                        obs.fields, obs.level_audit = other.fields, other.level_audit
                        obs.tag_images = other.tag_images
                        obs.status_audit = other.status_audit
                        obs.signature_image = getattr(other, 'signature_image', None)
                        page.entries.append(other.entry)
            return  # A whole-card retry would mix the adjacent tag into the name.
        for obs in page.observations:
            if obs.entry is not None:
                continue
            if obs.name_audit.get('method') == 'rejected':
                obs.issue += '；'+obs.name_audit.get('reason', '昵称待校对')
                continue
            self.check()
            band = obs.band
            roi = layout.crop(frame, (layout.NAME[0], band.top, layout.NAME[2], band.bottom))
            results = ocr_engine.recognize(roi)
            entry = parser.parse_card(results, band.height, 194, int(band.center))
            if entry is not None:
                entry.signature_pending = layout.signature_read_pending(
                    getattr(obs, 'signature_image', None), (None, 0.))
                obs.entry, obs.issue = entry, ''
                page.entries.append(entry)
            else:
                obs.issue += '；重试 OCR 框=' + repr([(r.text, r.confidence, r.box) for r in results])

    def same_person(self, previous, current):
        return layout.same_person(previous, current)

    def add_page(self, frame, page):
        if not page.confirmed:
            raise ScanIncomplete('好友标题校验失败：'+'；'.join(page.issues))
        if page.expected_total is None:
            raise ScanIncomplete('无法确认右上角好友总数')
        if self.expected is None:
            self.expected = page.expected_total
        elif self.expected != page.expected_total:
            raise RosterChanged('扫描期间好友总数发生变化，需要重新扫描', 'count_changed')
        if self.expected == 0:
            return []
        if not page.observations:
            raise ScanIncomplete('没有检测到完整好友卡片')
        self.recover_fields(frame, page)
        if self.origin is None:
            self.origin = page.observations[0].band.top
        first = page.observations[0].band.top
        first_index = round((self.offset+first-self.origin)/layout.ROW_PITCH)
        corrected_offset = first_index*layout.ROW_PITCH+self.origin-first
        if abs(corrected_offset-self.offset) > 9:
            raise ScanIncomplete('实测移动距离与好友行距不一致，拒绝跨行归档')
        self.offset = corrected_offset
        indices = []
        for i, obs in enumerate(page.observations):
            index = first_index+i
            if not 0 <= index < self.expected:
                raise ScanIncomplete('检测到名单范围之外的卡片，不能确认列表位置')
            indices.append(index)
            previous = self.slots.get(index)
            if previous is not None:
                same = self.same_person(previous, obs)
                if (not same and previous.entry and obs.entry
                        and not previous.entry.nickname_pending and not obs.entry.nickname_pending):
                    same = (previous.entry.nickname == obs.entry.nickname
                            and getattr(previous.entry, 'interface_mode', 'normal') == getattr(obs.entry, 'interface_mode', 'normal')
                            and previous.entry.level == obs.entry.level
                            and getattr(previous.entry, 'qianxing_level', None) == getattr(obs.entry, 'qianxing_level', None)
                            and layout.visual_similarity(previous.avatar_image, obs.avatar_image) >= .80)
                if not same:
                    # This can happen after a corrective drag, before record_page
                    # has saved the new frame. Keep the actual conflicting pair
                    # so a reorder can be distinguished from a visual mismatch.
                    self.save_image('overlap-failed.png', frame, always=True)
                    for name, image in (('name', previous.name_image), ('avatar', previous.avatar_image)):
                        (self.evidence/('overlap-previous-%s.png' % name)).write_bytes(_png(image))
                    (self.evidence/'overlap-failed.json').write_text(json.dumps({
                        'page': self.frame_count, 'position': index+1, 'offset': self.offset,
                        'previous': previous.entry.to_dict() if previous.entry else None,
                        'current': obs.entry.to_dict() if obs.entry else None,
                        'fields': obs.fields, 'name_audit': obs.name_audit,
                        'name_similarity': layout.visual_similarity(previous.name_image, obs.name_image),
                        'name_ink_similarity': layout.nickname_ink_similarity(previous.name_image, obs.name_image),
                        'avatar_similarity': layout.visual_similarity(previous.avatar_image, obs.avatar_image),
                        'image': 'overlap-failed.png', 'retention': '每轮最后一次重叠冲突及此前昵称/头像裁片'},
                        ensure_ascii=False, indent=2), encoding='utf-8')
                    raise RosterChanged('重叠卡片身份发生变化，名单可能重排；重新从顶部采集')
                # Preserve a successful observation when a later overlap is weak.
                if previous.entry and (obs.entry is None or
                        (not previous.entry.nickname_pending and obs.entry.nickname_pending) or
                        (previous.entry.nickname_pending == obs.entry.nickname_pending
                         and previous.entry.confidence >= obs.entry.confidence)):
                    continue
            self.slots[index] = obs
        return indices

    def record_nickname_reviews(self, page, indices, page_number):
        """Retain unresolved text/image pairs without treating them as labels.

        This small crop is saved even when full-page evidence is disabled.
        Positions are diagnostic coordinates for this traversal, never IDs.
        """
        directory = self.scanner.evidence_root/'nickname-review'/self.evidence.name
        for index, obs in zip(indices, page.observations):
            rejected = obs.name_audit.get('method') == 'rejected'
            weak = bool(obs.fields and obs.fields[0][1] < .85)
            if not (rejected or weak):
                continue
            directory.mkdir(parents=True, exist_ok=True)
            stem = 'page-%03d-row-%03d' % (page_number+1, index+1)
            image_path, record_path = directory/(stem+'.png'), directory/(stem+'.json')
            record = {'schema_version': 1, 'scan_time': self.scan_time,
                      'scan_evidence': str(self.evidence.resolve()), 'phase': self.phase,
                      'page': page_number+1, 'traversal_position': index+1,
                      'status': ('pending_review' if obs.entry and obs.entry.nickname_pending else
                                 'rejected' if rejected else 'low_confidence'),
                      'image': image_path.name,
                      'image_description': '1920x1080 客户区归一化后的原始昵称裁片，未二值化',
                      'fields': obs.fields, 'issue': obs.issue,
                      'entry': obs.entry.to_dict() if obs.entry else None,
                      'name_audit': dict(obs.name_audit)}
            image_path.write_bytes(_png(obs.name_image))
            record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            paths = {'image': str(image_path.resolve()), 'record': str(record_path.resolve())}
            obs.name_audit['review_evidence'] = paths
            self.nickname_reviews.append(paths)

    def record_page(self, frame, page, indices, movement):
        number = self.frame_count
        self.frame_count += 1
        self.save_image('page-%03d.png' % number, frame)
        self.record_nickname_reviews(page, indices, number)
        record = {
            'page': number, 'elapsed_seconds': round(time.perf_counter()-self.started, 4),
            'ocr_seconds': round(page.ocr_seconds, 4),
            'field_ocr_seconds': round(page.field_ocr_seconds, 4),
            'name_refine_seconds': round(page.name_refine_seconds, 4), 'movement': movement,
            'scrollbar': vars(layout.scrollbar(frame)) if layout.scrollbar(frame) else None,
            'indices': indices, 'expected_total': page.expected_total,
            'rows': [{'index': index, 'top': obs.band.top, 'bottom': obs.band.bottom,
                      'fields': obs.fields, 'issue': obs.issue, 'name_audit': obs.name_audit,
                      'level_audit': getattr(obs, 'level_audit', {}),
                      'status_audit': getattr(obs, 'status_audit', {}),
                      'entry': obs.entry.to_dict() if obs.entry else None}
                     for index, obs in zip(indices, page.observations)],
        }
        self.frames.append(record)
        with (self.evidence/'pages.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False)+'\n')
        ocr_dump_logger.info('快速屏%d: %s', number, ' | '.join(page.texts))
        good = sum(o.entry is not None for o in self.slots.values())
        self.scanner._say('第 %d 屏：已核对 %d/%d 行，有效 %d；批量识别 %.2f 秒，总耗时 %.1f 秒' %
                          (number+1, len(self.slots), self.expected, good, page.ocr_seconds,
                           time.perf_counter()-self.started))

    def advance_page(self, rect, before):
        """A near-bottom thumb is not proof that the last card is fully visible.

        Its three-pixel tolerance can represent most of a friend row on a long
        roster. Permit one measured forward probe; the normal movement/overlap
        guards must prove progress before another page can be read or committed.
        """
        thumb = layout.scrollbar(before)
        if thumb is not None and thumb.at_bottom:
            if self.tail_probes:
                raise ScanIncomplete('末页补滑后覆盖仍不完整，未继续重复拖动或提交')
            self.tail_probes += 1
            self.scanner._say('滑块接近底部，但完整名单仍缺 %d 行；向下核验末页一次' %
                              max(0, (self.expected or 0)-len(self.slots)))
        return self.advance(rect, before)

    def advance(self, rect, before):
        self._movement_audit = {'page': self.frame_count, 'client_rect': rect,
                                'interface_mode': self.interface_mode, 'samples': []}
        self._movement_after = None
        try:
            result = self._advance(rect, before)
            self._movement_audit.update(complete=True, movement=result[2])
            return result
        except Exception as error:
            self._movement_audit.update(complete=False, error=str(error))
            # Keep the exact failed pair even when ordinary page saving is off.
            # Fixed filenames bound this extra evidence to two frames per pass.
            for suffix, frame in (('before', before), ('after', self._movement_after)):
                if isinstance(frame, np.ndarray) and frame.ndim == 3:
                    name = 'movement-failed-%s.png' % suffix
                    self.save_image(name, frame, always=True)
                    self._movement_audit[suffix+'_image'] = name
            raise
        finally:
            with (self.evidence/'movement.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(self._movement_audit, ensure_ascii=False)+'\n')

    def _advance(self, rect, before):
        before_bands = layout.bands(before, interface_mode=self.interface_mode)
        if not before_bands:
            raise ScanIncomplete('无法定位下一组好友的起始位置')
        # Keep the last complete known card visible for nickname+avatar motion
        # evidence. Advancing six new rows left only the faded/clipped tail of
        # a card, which caused unnecessary reverse drags in real Qianxing scans.
        desired = before_bands[-1].top-layout.NEXT_ROW_TOP
        thumb = layout.scrollbar(before)
        tail_probe = bool(thumb and thumb.at_bottom)
        if tail_probe:
            desired = min(desired, 2*layout.ROW_PITCH)
        commanded = max(30., min(780., desired-self.drag_bias))
        self._movement_audit.update(desired_pixels=desired, commanded_pixels=commanded,
                                    tail_probe=tail_probe)

        def measure(image, source=None):
            source = before if source is None else source
            old_thumb, new_thumb = layout.scrollbar(source), layout.scrollbar(image)
            expected = layout.scrollbar_displacement(old_thumb, new_thumb, self.expected)
            pixels, correlation = layout.scroll_displacement(source, image, expected=expected,
                                                              interface_mode=self.interface_mode)
            self._movement_after = image
            self._movement_audit['samples'].append({
                'reference': 'initial' if source is before else 'previous_correction',
                'expected_pixels': expected, 'pixels': pixels, 'correlation': correlation,
                'old_scrollbar': vars(old_thumb) if old_thumb else None,
                'new_scrollbar': vars(new_thumb) if new_thumb else None})
            return pixels, correlation

        self.drag(rect, distance=commanded)
        rect, after = self.shot()
        dy, score = measure(after)

        def alignment(image, pixels):
            rows = layout.bands(image, interface_mode=self.interface_mode)
            if not rows:
                return {'needed': True, 'reason': 'no_rows', 'first': None, 'last': None}
            first = round((self.offset+pixels+rows[0].top-self.origin)/layout.ROW_PITCH)
            last = first+len(rows)-1
            # Progress is relative to this viewport, not the entire saved draft:
            # late identity checks revisit old rows with a complete slots map.
            # The next image must reach the next row without skipping it; its
            # overlapping identities are still checked by add_page afterwards.
            next_row = round((self.offset+before_bands[-1].top-self.origin)/layout.ROW_PITCH)+1
            reason = ('row_gap' if first > next_row else
                      'no_new_rows' if last < next_row else '')
            # Five complete consecutive cards are useful coverage. A partial
            # already-seen row above them does not require reversing the list.
            return {'needed': bool(reason), 'reason': reason, 'first': first, 'last': last}

        def needs_alignment(image, pixels):
            return alignment(image, pixels)['needed']

        if score < .67 or abs(dy) < 20 or needs_alignment(after, dy):
            # Let hover scaling and the last animation frame settle before
            # deciding whether a corrective drag is needed.
            if not self.scanner._wait(.12):
                raise ScanIncomplete('扫描已停止')
            rect, after = self.shot()
            dy, score = measure(after)
        initial = {'pixels': dy, 'correlation': score}
        thumb_fallback = False
        card_fallback = False
        recoveries = []
        for _ in range(2):
            if not (abs(dy) < 20 and score >= .65
                    and layout.stationary_view(before, after, interface_mode=self.interface_mode)):
                break
            thumb = layout.scrollbar(after)
            if thumb is not None and thumb.at_bottom:
                break
            if not layout.is_friend_list(after):
                break
            if thumb is not None and not thumb_fallback and not card_fallback:
                # Some desktop clients don't accept dragging the card surface.
                # The thumb is an explicit drag control; its result is measured too.
                extent = self.expected*layout.ROW_PITCH-(layout.VIEW[3]-layout.VIEW[1])
                travel = layout.TRACK_BOTTOM-layout.TRACK_TOP-(thumb.bottom-thumb.top)
                pixels = desired*travel/max(1, extent)
                self.scanner._say('卡片区未移动，改用右侧滚动条拖动并核验位移')
                recoveries.append({'method': 'scrollbar', 'stationary_verified': True})
                self.drag(rect, (thumb.x, thumb.center),
                          (thumb.x, min(layout.TRACK_BOTTOM, thumb.center+pixels)))
                thumb_fallback = True
            elif not card_fallback:
                rows = layout.bands(after, interface_mode=self.interface_mode)
                start_y = rows[-1].bottom-16
                distance = min(2*layout.ROW_PITCH, max(layout.ROW_PITCH, desired))
                self.scanner._say('完整好友行确认仍在原位，在完整卡片内向前短拖一次')
                recoveries.append({'method': 'card', 'stationary_verified': True,
                                   'start_y': start_y, 'pixels': distance})
                self.drag(rect, (layout.DRAG_X, start_y), (layout.DRAG_X, start_y-distance))
                card_fallback = True
            else:
                break
            self._movement_audit['recoveries'] = recoveries
            rect, after = self.shot()
            dy, score = measure(after)
        if abs(dy) < 20 and score >= .65:
            if not layout.stationary_view(before, after, interface_mode=self.interface_mode):
                raise ScanIncomplete('位移读数与完整好友行不一致，未追加拖动；已保存前后截图')
            raise ScanIncomplete('有限重试后列表仍未向下移动，未继续重复识别同一页')
        corrections = []
        for _ in range(2):
            if score >= .67 and not needs_alignment(after, dy):
                break
            if not layout.is_friend_list(after):
                raise ScanIncomplete('拖动后好友界面校验失败，未继续回拖')
            previous_thumb, next_thumb = layout.scrollbar(before), layout.scrollbar(after)
            if not previous_thumb or not next_thumb or next_thumb.top <= previous_thumb.top:
                break
            status = alignment(after, dy)
            forward = score >= .67 and status['reason'] == 'no_new_rows'
            correction = (max(40., min(layout.ROW_PITCH, desired-dy)) if forward else
                          max(40., min(layout.ROW_PITCH, dy-desired))
                          if score >= .67 else layout.ROW_PITCH)
            corrections.append({'requested_pixels': correction, 'before_pixels': dy,
                                'before_correlation': score,
                                'direction': 'forward' if forward else 'reverse',
                                'reason': status['reason'] if score >= .67 else 'uncertain_displacement'})
            if forward:
                self.scanner._say('位移已确认，但新好友尚未完整露出，向下补滑 %.0f 像素' % correction)
            else:
                reason = '检测到未扫描行缺口' if status['reason'] == 'row_gap' and score >= .67 else '位移证据不足'
                self.scanner._say('%s，回拖 %.0f 像素恢复可核验的重叠' % (reason, correction))
            self.save_image('alignment-%03d-%d.png' % (self.frame_count, len(corrections)), after)
            previous_after = after
            if forward:
                self.drag(rect, distance=correction)
            else:
                self.drag(rect, (layout.DRAG_X, 250), (layout.DRAG_X, 250+correction))
            rect, after = self.shot()
            reversed_pixels, reversed_score = measure(after, previous_after)
            corrections[-1].update(actual_pixels=reversed_pixels, correlation=reversed_score)
            dy, score = measure(after)
        if score < .67:
            self.save_image('movement-unconfirmed.png', after)
            raise ScanIncomplete('无法核实两屏衔接（相关度 %.3f），已停止避免漏行' % score)
        previous_thumb, next_thumb = layout.scrollbar(before), layout.scrollbar(after)
        if previous_thumb and next_thumb and next_thumb.top < previous_thumb.top-1:
            raise ScanIncomplete('滚动条向上移动，方向与向下扫描不一致')
        if dy < 20:
            raise ScanIncomplete('列表未向下移动，未继续重复识别同一页')
        if needs_alignment(after, dy):
            self.save_image('row-gap-unconfirmed.png', after)
            raise ScanIncomplete('校正后仍无法连续采集新的完整好友行，未跳过该好友')
        # A too-large initial drag may leave too little overlap to measure it
        # directly. Once the final shared card is verified, independently
        # measured reverse movements reconstruct the initial distance. Learn
        # that inertia too, instead of repeating the same overshoot next page.
        initial_pixels = initial['pixels'] if initial['correlation'] >= .85 else None
        if initial_pixels is None and score >= .85 and corrections and all(
                c['correlation'] >= .85 and -2*layout.ROW_PITCH <= c['actual_pixels'] < 0
                for c in corrections):
            initial_pixels = dy-sum(c['actual_pixels'] for c in corrections)
            initial['recovered_pixels'] = initial_pixels
        thumb = layout.scrollbar(after)
        if (initial_pixels is not None and initial_pixels >= 20 and not thumb_fallback and not card_fallback
                and not (thumb and thumb.at_bottom)):
            bias = max(-60., min(90., initial_pixels-commanded))
            self.drag_errors = (self.drag_errors+[bias])[-5:]
            self.drag_bias = float(median(self.drag_errors))
        self.offset += dy
        return rect, after, {'pixels': dy, 'correlation': score, 'desired_pixels': desired,
                             'strategy': 'tail_probe' if tail_probe else 'one_complete_row_overlap',
                             'commanded_pixels': commanded, 'initial': initial,
                             'drag_bias': self.drag_bias, 'corrections': corrections,
                             'thumb_fallback': thumb_fallback, 'card_fallback': card_fallback,
                             'recoveries': recoveries}

    def prepare_records(self):
        profiles = self.scanner.db.get_visual_profiles()
        for p in profiles:
            p['name'] = _decode(p['name_image'])
            p['avatar'] = _decode(p['avatar_image'])
        by_id = {p['friend_id']: p for p in profiles}
        used, result = set(), []
        for index in range(self.expected):
            self.check()
            obs = self.slots[index]
            entry = obs.entry
            row = self.scanner.db.find_by_nickname(entry.nickname) if not entry.nickname_pending else None
            if row is None and not entry.nickname_pending:
                row, _ = self.scanner.db.find_friend_by_alias(entry.nickname)
            fid = row['id'] if row and row['id'] not in used else None
            profile = by_id.get(fid)
            if profile and (layout.visual_similarity(obs.name_image, profile['name']) < .86
                            or layout.visual_similarity(obs.avatar_image, profile['avatar']) < .82):
                fid = None
            if fid is None:
                matches = [p['friend_id'] for p in profiles if p['friend_id'] not in used
                           and p.get('interface_mode', 'normal') == self.interface_mode
                           and p.get('qianxing_level' if self.interface_mode == 'qianxing' else 'level') ==
                               (getattr(entry, 'qianxing_level', None) if self.interface_mode == 'qianxing' else entry.level)
                           and layout.visual_similarity(obs.avatar_image, p['avatar']) >= .90
                           and layout.visual_similarity(obs.name_image, p['name']) >= .91]
                if len(matches) == 1:
                    fid = matches[0]
            if fid is not None:
                used.add(fid)
            signature_image = getattr(obs, 'signature_image', None)
            result.append({'id': fid, 'entry': entry, 'name_image': _png(obs.name_image),
                           'avatar_image': _png(obs.avatar_image),
                           'signature_image': (_png(signature_image) if signature_image is not None else None)})
        return result

    def finish(self, error=''):
        self.failure_message = error
        from app.scanner import ScanResult
        good = sum(o.entry is not None for o in self.slots.values())
        elapsed = time.perf_counter()-self.started
        complete = not error
        report = {'complete': complete, 'scan_time': self.scan_time,
                  'interface_mode': self.interface_mode,
                  'elapsed_seconds': elapsed, 'pages': self.frame_count,
                  'expected_total': self.expected, 'covered_rows': len(self.slots),
                  'recognized_rows': good, 'pending_nicknames': sum(o.entry is not None and o.entry.nickname_pending for o in self.slots.values()),
                  'top_verified': self.top_verified, 'error': error,
                  'tail_probes': self.tail_probes,
                  'failure_reason': self.failure_reason, 'phase': self.phase,
                  'committed': bool(complete and self.commit_enabled),
                  'nickname_reviews': self.nickname_reviews,
                  'review_positions': [i+1 for i,o in sorted(self.slots.items())
                                       if o.entry is not None and o.entry.confidence < .85],
                  'local_review_positions': [i+1 for i,o in sorted(self.slots.items())
                                             if o.name_audit.get('method') == 'local_review'],
                  'rows': [{'position': index+1, 'entry': obs.entry.to_dict() if obs.entry else None,
                            'issue': obs.issue, 'name_audit': obs.name_audit,
                            'level_audit': getattr(obs, 'level_audit', {}),
                            'status_audit': getattr(obs, 'status_audit', {})}
                           for index, obs in sorted(self.slots.items())]}
        (self.evidence/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        online = sum(o.entry is not None and o.entry.status == 'online' for o in self.slots.values())
        offline = good-online
        message = ('完整核验 %d/%d 位好友，%d 屏，耗时 %.1f 秒' %
                   (good, self.expected, self.frame_count, elapsed) if complete else
                   '扫描未完成：%s（有效 %d/%s，耗时 %.1f 秒；保留上次完整名单）' %
                   (error, good, self.expected or '?', elapsed))
        pending = sum(o.entry is not None and o.entry.nickname_pending for o in self.slots.values())
        if pending:
            message += '；%d 个昵称以截图保留，待人工核查' % pending
        if complete and not self.commit_enabled:
            message = self.phase+'草稿：'+message+'；等待流程最终校验后提交'
        self.scanner._say(message)
        return ScanResult(self.scan_time, good, online, offline,
                          self.scanner.db.export_rows(), self.scanner.db.get_recent_events(limit=300),
                          message, complete=complete)

    def after_page(self, rect, frame, page, indices):
        return rect, frame

    def prepare_page(self, rect, frame, page):
        return rect, frame, page

    @retained_scan_round
    def run(self, *, commit=True):
        self.commit_enabled = commit
        try:
            rect, frame = self.home()
            movement = None
            for _ in range(min(CONFIG.max_scrolls, 80)):
                self.check()
                page = layout.read_page(frame, should_continue=self.scanner._check_pause,
                                        interface_mode=self.interface_mode)
                rect, frame, page = self.prepare_page(rect, frame, page)
                settled = getattr(page, 'field_recovery_shift', 0.)
                if movement and settled:
                    # Movement was measured against the first capture. Record
                    # the net displacement of the adopted, settled page too.
                    movement = dict(movement, measured_pixels=movement['pixels'],
                                    pixels=movement['pixels']-settled, field_recovery_shift=settled)
                indices = self.add_page(frame, page)
                self.record_page(frame, page, indices, movement)
                rect, frame = self.after_page(rect, frame, page, indices)
                if self.expected is not None and set(self.slots) == set(range(self.expected)):
                    missing = [i+1 for i, obs in self.slots.items() if obs.entry is None]
                    if missing:
                        raise ScanIncomplete('字段仍未通过校验的行：'+str(missing))
                    self.check()
                    if commit:
                        records = self.prepare_records()
                        ids = self.scanner.db.apply_verified_scan(records, self.scan_time,
                                                                 time.perf_counter()-self.started,
                                                                 str(self.evidence))
                        self.scanner._seen_ids = set(ids)
                        self.scanner._seen_statuses = {fid:r['entry'].status for fid,r in zip(ids, records)}
                        self.scanner._online = sum(v == 'online' for v in self.scanner._seen_statuses.values())
                        self.scanner._offline = len(ids)-self.scanner._online
                        self.scanner._detect_removed(now_str())
                    return self.finish()
                if self.slots and set(self.slots) != set(range(max(self.slots)+1)):
                    raise ScanIncomplete('两屏之间出现未覆盖的好友行')
                rect, frame, movement = self.advance_page(rect, frame)
            raise ScanIncomplete('达到最大页数，完整性未确认')
        except ScanIncomplete as error:
            self.failure_reason = error.reason if isinstance(error, RosterChanged) else 'incomplete'
            self.scanner._missing_counts.clear()
            return self.finish(str(error))
        except Exception as error:
            self.scanner._missing_counts.clear()
            logger.exception('快速扫描失败，保留诊断证据')
            return self.finish(str(error))

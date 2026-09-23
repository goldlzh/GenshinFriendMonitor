"""Collect only the verified online prefix without replacing the full roster."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import json
import re
from statistics import median
import time
import unicodedata

from app import fast_layout as layout, ocr_engine, parser
from app.config import CONFIG
from app.database import now_str
from app.fast_scan import FastScan, RosterChanged, ScanIncomplete, _decode
from app.logger import logger
from app.scan_workflow import UidPass, records_from_observations, observed_level


ONLINE_ACTIONS = frozenset(('互动', '前往其大厅', '申请加入'))
ACTION_NARROW = (1540, 34, 1682, 76)
ACTION_WIDE = (1510, 34, 1682, 76)


def _compact(value):
    return ''.join(unicodedata.normalize('NFKC', value or '').split())


def classify_action(reads, left_status):
    """Two exact action reads; blank is offline only with explicit left evidence."""
    if len(reads) != 2:
        return None
    texts = [_compact(value[0]) for value in reads]
    if texts[0] != texts[1]:
        return None
    text = texts[0]
    if not text:
        return 'offline' if left_status == 'offline' else None
    if any(not .90 <= value[1] <= 1. for value in reads):
        return None
    if text in ONLINE_ACTIONS:
        return 'online' if left_status != 'offline' else None
    if text == '离线':
        return 'offline' if left_status != 'online' else None
    return None


def read_actions(frame, observations, interface_mode):
    """Read action labels separately from signatures and classify every full card."""
    images = []
    for obs in observations:
        for x1, y1, x2, y2 in (ACTION_NARROW, ACTION_WIDE):
            images.extend((layout.crop(frame, (x1, obs.band.top+y1, x2, obs.band.top+y2)),
                           layout.crop(frame, (x1, obs.band.top+y1-3, x2, obs.band.top+y2+3))))
        field = layout.QIANXING_STATUS if interface_mode == 'qianxing' else layout.STATUS
        images.append(layout.prepare_status(layout.crop(frame, layout.field_rect(obs.band, field))))
    output = ocr_engine.recognize_lines(images)
    if len(output) != len(images):
        raise ScanIncomplete('在线动作识别返回数量不一致')
    states, evidence = [], []
    for index, obs in enumerate(observations):
        narrow, wide = output[index*5:index*5+2], output[index*5+2:index*5+4]
        left = output[index*5+4]
        left_status = None
        if .90 <= left[1] <= 1.:
            value = parser._is_status_word(left[0])
            if value is not None:
                left_status = 'online' if value else 'offline'
            elif interface_mode == 'qianxing' and (
                    _compact(left[0]) == '空闲中' or re.fullmatch(r'\d{1,3}[/／]\d{1,3}组队中', _compact(left[0]))
                    or layout.qianxing_activity_evidence(frame, obs.band, *left).get('verified')):
                left_status = 'online'
        # The short label has an icon immediately on its left. The wider
        # hypothesis is reserved for complete long labels; never strip an OCR
        # "日/一" icon prefix to turn a partial read into an accepted action.
        found = []
        for reads, allowed in ((narrow, ('互动', '离线')),
                               (wide, ('前往其大厅', '申请加入', '离线'))):
            if _compact(reads[0][0]) in allowed:
                state = classify_action(reads, None)
                if state is not None:
                    found.append(state)
        state = found[0] if found and len(set(found)) == 1 else None
        if state is not None and left_status is not None and state != left_status:
            state = None
        if not found and all(not _compact(value[0]) for value in narrow+wide):
            state = classify_action(narrow, left_status)
        states.append(state)
        evidence.append({'page_row': index+1, 'top': obs.band.top, 'bottom': obs.band.bottom,
                         'narrow_reads': narrow, 'wide_reads': wide,
                         'left_read': left, 'left_status': left_status,
                         'reliable_online_action': any(_compact(value[0]) in ONLINE_ACTIONS
                             and .90 <= value[1] <= 1. for value in narrow+wide),
                         'status': state})
    return states, evidence


def reusable_online_identities(items, profiles, rows=None):
    """Reuse only a mutually unique, unchanged name and strong same-mode pictures.

    This is deliberately independent of full-roster cardinality. A renamed,
    missing, cross-interface or ambiguous item needs its own fresh profile UID.
    """
    decoded = []
    for profile in profiles:
        if re.fullmatch(r'[0-9]{9}', profile.get('uid') or ''):
            name, avatar = _decode(profile['name_image']), _decode(profile['avatar_image'])
            if name is not None and avatar is not None:
                decoded.append((profile, name, avatar))
    pictured_ids = {profile['friend_id'] for profile, _, _ in decoded}
    unpictured = [row for row in rows or [] if row['id'] not in pictured_ids
                  and re.fullmatch(r'[0-9]{9}', row.get('uid') or '')]
    candidates = []
    for obs in items:
        matches = []
        entry = obs.entry
        if entry is None or obs.name_audit.get('roster_ocr_conflict'):
            candidates.append(matches)
            continue
        if any(entry.nickname_pending or row.get('nickname_pending')
               or entry.nickname == row['nickname'] for row in unpictured):
            candidates.append(matches)
            continue
        mode = getattr(entry, 'interface_mode', 'normal')
        for profile, name, avatar in decoded:
            if profile.get('interface_mode', 'normal') != mode:
                continue
            pending = entry.nickname_pending or profile.get('nickname_pending')
            if not pending and entry.nickname != profile['nickname']:
                continue
            if profile.get('qianxing_level' if mode == 'qianxing' else 'level') != observed_level(entry):
                continue
            if layout.visual_similarity(obs.avatar_image, avatar) < .95:
                continue
            # A whole foreground comparison protects short names and dynamic
            # tag widths. Blank names cannot be reused from a shared avatar.
            ink = layout.nickname_ink_similarity(obs.name_image, name)
            if ink < .97:
                continue
            from app.scan_workflow import _whole_nickname_foreground_agrees
            if not _whole_nickname_foreground_agrees(obs.name_image, name):
                continue
            matches.append(profile)
        candidates.append(matches)
    counts = Counter(p['uid'] for matches in candidates for p in matches)
    return [matches[0] if len(matches) == 1 and counts[matches[0]['uid']] == 1 else None
            for matches in candidates]


class OnlineScanWorkflow(UidPass):
    """A guarded six-card traversal ending at the first verified offline row."""
    def __init__(self, scanner):
        super().__init__(scanner, None)
        self.phase = '在线好友扫描'
        self.commit_enabled = False
        self.uid_seconds = 0.
        self.boundary = None
        self.identity_sources = {}
        self.profiles = scanner.db.get_visual_profiles()
        self.known_rows = scanner.db.get_all_friends()
        self.action_evidence = []
        self._page_states = []

    def inspect_actions(self, frame, page, context):
        states, rows = read_actions(frame, page.observations, self.interface_mode)
        states = list(states)
        rows = [dict(row) for row in rows]
        boundary = len(states)
        error = None
        for index, state in enumerate(states):
            # Establish the boundary in reading order from strict right-side
            # evidence. A later offline row must never explain an earlier
            # unknown row. After that boundary, decorative card art can be
            # nonempty OCR without being an action: require positive offline
            # status on the left, and reject any readable online action.
            if boundary < index:
                if state == 'online' or rows[index].get('reliable_online_action'):
                    error = RosterChanged('离线好友之后又出现在线动作，在线排序不稳定，未提交')
                    break
                if rows[index].get('left_status') != 'offline':
                    error = ScanIncomplete('离线边界后左侧状态未明确离线，未提交在线名单')
                    break
                if state is None:
                    states[index] = 'offline'
                    rows[index].update(status='offline', raw_status=None, reason='offline_tail_left_status')
                    continue
            if state not in ('online', 'offline'):
                error = ScanIncomplete('右侧动作或离线状态无法确认，未提交在线名单')
                break
            if state == 'offline' and boundary == len(states):
                boundary = index
        event = {'page': self.frame_count, 'context': context,
                 'elapsed_seconds': round(time.perf_counter()-self.started, 4), 'rows': rows}
        if error is not None:
            event['error'] = str(error)
        self.action_evidence.append(event)
        with (self.evidence/'online-actions.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False)+'\n')
        if error is not None:
            self.save_image('online-actions-unconfirmed-%03d.png' % self.frame_count, frame, always=True)
            raise error
        return states, boundary

    def after_page(self, rect, frame, page, indices):
        online_items = page.observations[:len(indices)]
        reused = reusable_online_identities(online_items, self.profiles, self.known_rows)
        for row_index, (index, obs, profile) in enumerate(zip(indices, online_items, reused)):
            if index in self.uids:
                continue
            if obs.entry is None:
                raise ScanIncomplete('在线好友的必要字段缺失，未猜测身份')
            if profile is not None:
                uid = profile['uid']
                basis = 'unchanged_name_and_images'
                if profile.get('nickname_pending'):
                    obs.entry = replace(obs.entry, nickname='', nickname_pending=True)
                    self.slots[index].entry = obs.entry
            else:
                # Always verify every full card, including the offline tail.
                # Prefix-only verification would reject the real sixth card or
                # lose evidence of a reorder before a profile click.
                rect, before, bands = self.verify_visible(page)
                start = time.perf_counter()
                uid = self.read_uid(rect, before, bands[row_index], index+1)
                self.uid_seconds += time.perf_counter()-start
                basis = 'profile_uid'
            if not re.fullmatch(r'[0-9]{9}', uid or ''):
                raise ScanIncomplete('在线好友 UID 无效，未提交')
            if uid in self.uids.values():
                raise RosterChanged('不同在线横条对应同一 UID，未提交歧义身份')
            self.uids[index] = uid
            self.identity_sources[index] = basis
            with (self.evidence/'uids.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps({'traversal_position': index+1, 'uid': uid,
                    'nickname': obs.entry.nickname, 'basis': basis}, ensure_ascii=False)+'\n')
        if page.observations:
            rect, frame, bands = self.verify_visible(page)
            fresh_page = replace(page, observations=[replace(obs, band=band)
                for obs, band in zip(page.observations, bands)], bands=bands)
            states, _ = self.inspect_actions(frame, fresh_page, 'after_uid')
            if states != self._page_states:
                raise RosterChanged('在线状态在 UID 核验期间改变，未提交过时名单')
            shift = median(band.top-obs.band.top for band, obs in zip(bands, page.observations))
            if shift:
                with (self.evidence/'uid-layout.jsonl').open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'indices': indices, 'y_shift': shift,
                        'offset_before': self.offset, 'offset_after': self.offset-shift})+'\n')
                if self.origin is not None:
                    self.offset -= shift
        return rect, frame

    def run(self):
        error = ''
        complete = False
        try:
            rect, frame = self.home()
            movement = None
            for _ in range(min(CONFIG.max_scrolls, 80)):
                self.check()
                page = layout.read_page(frame, should_continue=self.scanner._check_pause,
                                        interface_mode=self.interface_mode)
                if not page.confirmed or page.expected_total is None:
                    raise ScanIncomplete('在线扫描无法确认好友标题和总数')
                if self.expected is None:
                    self.expected = page.expected_total
                elif page.expected_total != self.expected:
                    raise RosterChanged('在线扫描期间好友总数改变', 'count_changed')
                if self.expected and not page.observations:
                    raise ScanIncomplete('在线扫描未检测到完整好友卡片')
                states, prefix = self.inspect_actions(frame, page, 'read_page')
                if any(obs.entry is None for obs in page.observations[:prefix]):
                    rect, frame, page = self.prepare_page(rect, frame, page)
                    settled = getattr(page, 'field_recovery_shift', 0.)
                    if movement and settled:
                        movement = dict(movement, measured_pixels=movement['pixels'],
                                        pixels=movement['pixels']-settled, field_recovery_shift=settled)
                    states, prefix = self.inspect_actions(frame, page, 'after_field_recovery')
                self._page_states = states
                online_page = replace(page, observations=page.observations[:prefix],
                    entries=[o.entry for o in page.observations[:prefix] if o.entry is not None],
                    bands=[o.band for o in page.observations[:prefix]])
                indices = FastScan.add_page(self, frame, online_page) if prefix or not self.expected else []
                if any(self.slots[i].entry is None for i in indices):
                    raise ScanIncomplete('在线好友的必要字段未通过校验')
                if any(self.slots[i].entry.status != 'online' for i in indices):
                    raise ScanIncomplete('左右状态证据不一致，未提交在线名单')
                first = (indices[0] if indices else
                         round((self.offset+page.observations[0].band.top-self.origin)/layout.ROW_PITCH)
                         if page.observations and self.origin is not None else 0)
                if not indices and page.observations and self.origin is not None:
                    corrected = first*layout.ROW_PITCH+self.origin-page.observations[0].band.top
                    if abs(corrected-self.offset) > 9:
                        raise ScanIncomplete('离线边界位置与实测移动行距不一致')
                    self.offset = corrected
                full_indices = list(range(first, first+len(page.observations)))
                if any(i < 0 or i >= self.expected for i in full_indices):
                    raise ScanIncomplete('完整可见卡片超出好友人数范围')
                self.record_page(frame, page, full_indices, movement)
                if set(self.slots) != set(range(max(self.slots, default=-1)+1)):
                    raise ScanIncomplete('在线名单出现未覆盖的好友行')
                if prefix < len(page.observations):
                    self.boundary = first+prefix
                    if set(self.slots) != set(range(self.boundary)):
                        raise ScanIncomplete('离线边界之前的在线名单不连续')
                rect, frame = self.after_page(rect, frame, page, indices)
                if self.boundary is not None or len(self.slots) == self.expected:
                    if set(self.uids) != set(self.slots):
                        raise ScanIncomplete('在线名单仍有未核验 UID')
                    self.check()
                    _, final_frame = self.shot()
                    confirmed, total = layout.read_header(final_frame)
                    if not confirmed or total != self.expected:
                        raise ScanIncomplete('在线名单提交前标题或好友人数改变')
                    self.records = records_from_observations([self.slots[i] for i in sorted(self.slots)])
                    for index, record in zip(sorted(self.slots), self.records):
                        uid = self.uids[index]
                        record.update(uid=uid, match_basis=self.identity_sources[index])
                        if uid in self.profile_images:
                            record['profile_image'] = self.profile_images[uid]
                    self.scan_time = now_str()
                    self.scanner.db.apply_online_scan(self.records, self.scan_time,
                        time.perf_counter()-self.started, str(self.evidence))
                    complete = True
                    break
                rect, frame, movement = self.advance_page(rect, frame)
            else:
                raise ScanIncomplete('达到最大页数，在线边界未确认')
        except Exception as exc:
            error = str(exc)
            self.failure_reason = exc.reason if isinstance(exc, RosterChanged) else 'incomplete'
            if not isinstance(exc, ScanIncomplete):
                logger.exception('在线好友扫描失败，保留诊断证据')
        return self.finish_online(complete, error)

    def finish_online(self, complete, error):
        from app.scanner import ScanResult
        elapsed = time.perf_counter()-self.started
        count = len(self.records) if complete else len(self.uids)
        note = ('在线扫描完成：%d 位在线好友，补读 %d 位 UID，耗时 %.1f 秒；保留完整名单' %
                (count, len(self.profile_images), elapsed) if complete else
                '在线扫描未完成：%s；保留原名单和本轮诊断证据' % error)
        report = {'complete': complete, 'mode': 'online', 'account_uid': self.scanner.account_uid,
                  'interface_mode': self.interface_mode, 'expected_total': self.expected,
                  'online_count': count, 'offline_boundary': self.boundary,
                  'covered_indices': sorted(self.slots), 'uid_count': len(self.uids),
                  'identity_sources': self.identity_sources, 'pages': self.frame_count,
                  'top_verified': self.top_verified, 'error': error, 'message': note,
                  'tail_probes': self.tail_probes,
                  'uid_seconds': self.uid_seconds, 'workflow_seconds': elapsed,
                  'committed': complete, 'full_roster_unchanged': True}
        (self.evidence/'online-workflow.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        rows = self.scanner.db.export_rows()
        online = sum(r.get('current_status') == 'online' for r in rows)
        self.scanner._say(note)
        return ScanResult(self.scan_time, len(rows), online, len(rows)-online, rows,
            self.scanner.db.get_recent_events(limit=300), note, complete=complete, mode='online',
            quick_seconds=max(0., elapsed-self.uid_seconds), uid_seconds=self.uid_seconds,
            workflow_seconds=elapsed, uid_complete=complete)

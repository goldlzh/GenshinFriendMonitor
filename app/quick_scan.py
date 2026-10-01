"""Quick monitoring with selective profile UID checks for uncertain identities."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import json
import re
from statistics import median
import time

import numpy as np

from app import fast_layout as layout, nickname_ocr, ocr_engine
from app.config import CONFIG
from app.database import now_str
from app.fast_scan import RosterChanged, ScanIncomplete, _decode, _png
from app.logger import logger
from app.membership_events import comparison_identity_note
from app.scan_retention import retained_scan_round
from app.scan_workflow import UidPass, signature_key, observed_signature


def _review_key(name, signature):
    return hashlib.sha256((name+'\x1f'+signature).encode('utf-8')).hexdigest()


class QuickIdentities:
    """Resolve individuals independently of roster size, avatar and level changes."""
    def __init__(self, rows, profiles, *, matching_ids=None):
        self.rows = list(rows)
        self.by_id = {r['id']: r for r in self.rows}
        self.candidates = [r for r in self.rows if matching_ids is None or r['id'] in matching_ids]
        candidate_ids = {r['id'] for r in self.candidates}
        self.cache = {}
        self.images = []
        self.profile_geometry = {}
        for p in profiles:
            if p['friend_id'] not in candidate_ids:
                continue
            name = _decode(p['name_image'])
            if name is not None:
                self.images.append((p, name))

    def resolve(self, indexed, verified=None, failed=None):
        verified, failed = verified or {}, failed or set()
        items = list(indexed)
        def usable_signature(obs):
            # A pending crop may retain the previous text for display. That
            # stale text is never identity evidence until it is confirmed.
            return '' if getattr(obs.entry, 'signature_pending', False) else observed_signature(obs)
        name_counts = Counter(o.entry.nickname for _, o in items if not o.entry.nickname_pending)
        pairs = Counter((o.entry.nickname, usable_signature(o)) for _, o in items
                        if not o.entry.nickname_pending)
        signatures = Counter(usable_signature(o) for _, o in items)
        result = []
        review_usage = Counter()
        review_allocations = {}

        def review_draft(index, key):
            token = (index, key)
            if token not in review_allocations:
                pool = sorted((r for r in self.candidates if not r.get('uid')
                               and r.get('identity_review_key') == key), key=lambda r: r['id'])
                ordinal = review_usage[key]
                review_usage[key] += 1
                review_allocations[token] = pool[ordinal]['id'] if ordinal < len(pool) else None
            return review_allocations[token]

        def review(record, name, sig, reason, candidates=()):
            key = _review_key(name, sig)
            record.update(id=review_draft(record['index'], key),
                          uid=None, match_basis='needs_remark', rename_candidate=None,
                          identity_review_key=key, identity_review_reason=reason)
            candidates = list(candidates) or [r for r in self.candidates if
                (name and r['nickname'] == name) or
                (sig and not r.get('signature_pending') and signature_key(r.get('signature')) == sig)]
            if candidates:
                record['identity_candidate_ids'] = sorted({r['id'] for r in candidates})
            record['entry'] = replace(record['entry'], nickname='', nickname_pending=True,
                                      status='unknown', location=None,
                                      qianxing_activity=None, qianxing_activity_verified=False)

        for index, obs in items:
            entry = obs.entry
            name, sig = entry.nickname, usable_signature(obs)
            cached = self.cache.setdefault(id(obs), {'observation': obs})
            if 'name_image' not in cached:
                signature_image = getattr(obs, 'signature_image', None)
                cached.update(name_image=_png(obs.name_image), avatar_image=_png(obs.avatar_image),
                              signature_image=(_png(signature_image) if signature_image is not None else None))
            record = dict(index=index, entry=entry, id=None, uid=None,
                          name_image=cached['name_image'], avatar_image=cached['avatar_image'],
                          signature_image=cached['signature_image'],
                          match_basis='new_nickname', rename_candidate=None,
                          identity_review_key=None, identity_review_reason=None)
            names = [r for r in self.candidates if not r.get('identity_review_key')
                     and name and r['nickname'] == name]
            row = None
            if not entry.nickname_pending and pairs[(name, sig)] > 1:
                review(record, name, sig, '昵称与签名相同，无法区分；请在游戏内设置不同好友备注后重扫')
            elif not entry.nickname_pending and names:
                if len(names) == 1 and name_counts[name] == 1:
                    row = names[0]
                else:
                    same_signature = [r for r in names if sig and not r.get('signature_pending')
                                      and signature_key(r.get('signature')) == sig]
                    if len(same_signature) == 1:
                        row = same_signature[0]
                    else:
                        review(record, name, sig, '同名好友的签名无法唯一对应；请在游戏内设置不同好友备注后重扫')
                if row is not None:
                    record['match_basis'] = 'nickname' if len(names) == 1 else 'nickname_and_signature'
            elif entry.nickname_pending or not names:
                # Compare the nickname itself, never use an avatar or a level
                # as a substitute identity. Blank images cannot identify anyone.
                if 'image_matches' not in cached:
                    matches = []
                    possible = []
                    cached['geometry'] = layout.nickname_ink_geometry(obs.name_image)
                    cached['has_ink'] = cached['geometry']['area'] > 0
                    if cached['has_ink']:
                        for p, image in self.images:
                            if p.get('identity_review_key'):
                                continue
                            key = id(image)
                            if key not in self.profile_geometry:
                                self.profile_geometry[key] = layout.nickname_ink_geometry(image)
                            if layout.compatible_nickname_geometry(cached['geometry'], self.profile_geometry[key]):
                                score = layout.nickname_ink_similarity(obs.name_image, image)
                                if score >= .95 and nickname_ocr.whole_nickname_foreground_agrees(obs.name_image, image):
                                    possible.append(p['friend_id'])
                                    if score >= .985:
                                        matches.append(self.by_id[p['friend_id']])
                    cached['image_matches'] = matches
                    cached['identity_candidate_ids'] = possible
                if entry.nickname_pending and cached.get('identity_candidate_ids'):
                    # Weaker similarities only block an unsupported removal.
                    # They must never bind UID, merge history, or infer status.
                    record['identity_candidate_ids'] = sorted(set(cached['identity_candidate_ids']))
                matches = cached['image_matches']
                if len(matches) == 1:
                    row = matches[0]
                    record['match_basis'] = 'nickname_image'
                    if not entry.nickname_pending and name != row['nickname']:
                        # Same complete pixels with different OCR spelling are
                        # a text review, not a reason to open a profile.
                        record['entry'] = replace(entry, nickname='', nickname_pending=True)
                elif len(matches) > 1 or (entry.nickname_pending and not cached['has_ink']):
                    review(record, 'unreadable', sig, '昵称图像无法唯一对应；请人工填写昵称或在游戏内设置不同备注', matches)
                elif entry.nickname_pending:
                    record['match_basis'] = 'pending_nickname_image'
            if (row is None and record['match_basis'] == 'new_nickname' and sig
                    and signatures[sig] == 1):
                pending = next((r for r in self.candidates if not r.get('uid')
                                and r.get('identity_review_key') == _review_key(name, sig)), None)
                candidates = [r for r in self.candidates if not r.get('identity_review_key')
                              and not r.get('signature_pending')
                              and signature_key(r.get('signature')) == sig
                              and r['nickname'] != name]
                if pending is not None:
                    # Reuse the pending draft; a new round may retry its UID.
                    review(record, name, sig, pending['identity_review_reason'])
                elif len(candidates) == 1:
                    if candidates[0].get('uid'):
                        record['rename_candidate'] = candidates[0]
                        record['match_basis'] = 'possible_rename'
                    else:
                        review(record, name, sig, '签名对应的旧档案尚无UID，无法核验改名；请人工确认')
            if row is not None:
                record.update(id=row['id'], uid=row.get('uid'))
                if row.get('nickname_pending'):
                    record['entry'] = replace(record['entry'], nickname='', nickname_pending=True)
                if not row.get('uid') and record['entry'].nickname_pending and cached.get('identity_candidate_ids'):
                    record['identity_candidate_ids'] = sorted(set(cached['identity_candidate_ids']))
            reason = None
            if len(names) > 1 or (name and name_counts[name] > 1):
                reason = '同名好友'
            elif record.get('rename_candidate'):
                reason = '疑似改名'
            elif record.get('identity_review_key') or (entry.nickname_pending and not record.get('uid')):
                reason = '昵称无法唯一对应'
            elif record['match_basis'] == 'new_nickname' and self.candidates:
                reason = '新昵称或未匹配旧档案'
            record['uid_check_reason'] = reason
            if reason and record['id'] is None:
                key = _review_key(name if name and not entry.nickname_pending else 'unreadable', sig)
                record['id'] = review_draft(index, key)
            if index in failed:
                draft_id = record.get('id')
                review(record, name if name and not entry.nickname_pending else 'unreadable', sig,
                       '主页UID未能确认，保留待核对；下轮重试，也可手动录入主页')
                if draft_id is not None and not self.by_id[draft_id].get('uid'):
                    record['id'] = draft_id
            elif index in verified:
                uid = verified[index]
                if not isinstance(uid, str) or not re.fullmatch(r'[0-9]{9}', uid):
                    raise ScanIncomplete('主页未返回有效 UID，未绑定身份')
                owner = next((r for r in self.rows if r.get('uid') == uid), None)
                draft = self.by_id.get(record.get('id'))
                # Only the current UID-less draft may join the UID owner.
                # A different known UID can never inherit this card's history.
                fid = draft['id'] if draft and not draft.get('uid') else owner['id'] if owner else None
                record.update(id=fid, uid=uid, entry=entry,
                              match_basis='verified_rename' if record.get('rename_candidate') else 'verified_uid',
                              rename_candidate=None, identity_review_key=None, identity_review_reason=None,
                              identity_candidate_ids=[], uid_check_reason=None)
            result.append(record)
        # Do not resolve duplicate assignments by whichever row happened to be
        # scanned first. Both need a remark, even across different pages.
        verified_uids = [r['uid'] for r in result if r['index'] in verified]
        if len(verified_uids) != len(set(verified_uids)):
            raise RosterChanged('不同卡片核验到相同 UID，名单可能重排；本轮未提交')
        counts = Counter(r['id'] for r in result if r['id'] is not None)
        uid_counts = Counter(r['uid'] for r in result if r.get('uid'))
        for record, (_, obs) in zip(result, items):
            conflict = ((record['id'] is not None and counts[record['id']] > 1)
                        or (record.get('uid') and uid_counts[record['uid']] > 1))
            if conflict and record['index'] not in verified:
                review(record, obs.entry.nickname, usable_signature(obs),
                       '多张好友卡片对应同一档案；请设置不同好友备注后重扫', [self.by_id[record['id']]])
                record['uid_check_reason'] = '多张卡片对应同一档案'
        return result


def read_quick_actions(frame, observations, mode):
    """One action crop per card; unknown actions are recorded, never OCR-retried."""
    left = 1540 if mode == 'qianxing' else 1510
    reads = ocr_engine.recognize_lines([
        layout.crop(frame, (left, o.band.top+31, 1682, o.band.top+79)) for o in observations])
    states = []
    for obs, (text, confidence) in zip(observations, reads):
        text = ''.join(text.split())
        status = obs.entry.status
        if confidence >= .90 and text in ('互动', '前往其大厅', '申请加入') and status != 'offline':
            states.append('online')
        elif status == 'offline' and not (confidence >= .90 and text in ('互动', '前往其大厅', '申请加入')):
            states.append('offline')
        else:
            states.append('unknown')
    if len(states) != len(observations):
        raise ScanIncomplete('在线动作识别返回数量不一致')
    return states, reads


class QuickScanWorkflow(UidPass):
    """Continue the current page after checking only a suspected rename's UID."""
    def __init__(self, scanner, *, online_only=False):
        super().__init__(scanner, None)
        self.phase = '在线快速扫描' if online_only else '快速扫描'
        self.online_only = online_only
        self.identities = QuickIdentities(scanner.db.get_all_friends(), scanner.db.get_visual_profiles(),
                                          matching_ids=scanner.db.get_identity_candidate_ids())
        self.uid_seconds = 0.
        self.failed_uids = set()
        self.active_indices = set()
        self.boundary = None
        self.action_evidence = []
        self._field_review_drafts = {}

    def record_nickname_reviews(self, page, indices, page_number):
        """Successful rounds archive fields through the per-friend DB store.

        Hold small diagnostic crops until the round fails, so continuous quick
        scans cannot create another review directory for every successful pass.
        """
        for index, obs in zip(indices, page.observations):
            entry = obs.entry
            rejected = obs.name_audit.get('method') == 'rejected'
            weak = bool(obs.fields and obs.fields[0][1] < .85)
            fields = []
            if entry is None or rejected or weak or entry.nickname_pending:
                fields.append(('nickname', obs.name_image))
            if entry is None or getattr(entry, 'signature_pending', False):
                fields.append(('signature', getattr(obs, 'signature_image', None)))
            for field, image in fields:
                if image is not None and image.size:
                    self._field_review_drafts[(page_number, index, field)] = {
                        'image': image, 'page': page_number+1, 'traversal_position': index+1,
                        'field': field, 'entry': entry.to_dict() if entry else None,
                        'issue': obs.issue, 'name_audit': dict(obs.name_audit),
                        'position_verified': entry is not None}

    def finish_field_reviews(self, complete):
        if not complete:
            # The evidence root is account-scoped; failed rounds retain their
            # own diagnosis even when optional whole-page screenshots are off.
            directory = self.evidence/'field-review'
            for draft in self._field_review_drafts.values():
                directory.mkdir(parents=True, exist_ok=True)
                stem = 'page-%03d-row-%03d-%s' % (draft['page'], draft['traversal_position'], draft['field'])
                image_path, record_path = directory/(stem+'.png'), directory/(stem+'.json')
                image_path.write_bytes(_png(draft['image']))
                record = {key: value for key, value in draft.items() if key != 'image'}
                record.update(schema_version=1, image=image_path.name, scan_time=self.scan_time,
                              account_uid=self.scanner.account_uid, phase=self.phase,
                              image_description='1920x1080 客户区归一化后的原始字段裁片，未二值化')
                record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                self.nickname_reviews.append(dict(field=draft['field'], image=str(image_path.resolve()),
                                                   record=str(record_path.resolve())))
        self._field_review_drafts.clear()

    def recover_fields(self, frame, page):
        # quick=True has already retained uncertain fields as pending entries.
        if any(o.entry is None for o in page.observations):
            self.record_nickname_reviews(page, range(len(page.observations)), self.frame_count)
            self.save_image('card-unconfirmed.png', frame, always=True)
            raise ScanIncomplete('完整卡片边界无法确认，已保留截图；未重复识别或猜测跨行身份')

    def read_home_page(self, frame):
        page = layout.read_page(frame, should_continue=self.scanner._check_pause,
                                interface_mode=self.interface_mode, quick=True)
        self.home_page = frame, page
        return page

    def same_person(self, previous, current):
        a, b = previous.entry, current.entry
        if a and b and not a.nickname_pending and not b.nickname_pending and a.nickname == b.nickname:
            return True
        if (a and b and a.nickname_pending and b.nickname_pending
                and all(not np.any(nickname_ocr.ink_mask(image))
                        for image in (previous.name_image, current.name_image))):
            # A measured overlapping slot can contain a blank name. This only
            # checks page continuity; resolve() still keeps identity unknown.
            return layout.visual_similarity(previous.avatar_image, current.avatar_image) >= .95
        return (layout.nickname_ink_similarity(previous.name_image, current.name_image) >= .97
                and nickname_ocr.whole_nickname_foreground_agrees(previous.name_image, current.name_image)
                and layout.visual_similarity(previous.avatar_image, current.avatar_image) >= .85)

    def resolved(self, *, include_offline=False):
        # Offline cards on this page still disambiguate an online namesake.
        # Resolve active items first so uncommitted offline review placeholders
        # never consume the saved placeholder IDs needed by this online round.
        indices = sorted(self.active_indices)+sorted(set(self.slots)-self.active_indices)
        records = self.identities.resolve([(i, self.slots[i]) for i in indices],
                                          self.uids, self.failed_uids)
        return records if include_offline else [r for r in records if r['index'] in self.active_indices]

    def after_page(self, rect, frame, page, indices):
        current = set(indices)
        for record in self.resolved():
            index = record['index']
            if (index not in current or not record.get('uid_check_reason')
                    or index in self.uids or index in self.failed_uids):
                continue
            self.scanner._say('%s，打开第%d位好友主页核对UID后继续本页' % (record['uid_check_reason'], index+1))
            rect, before, bands = self.verify_visible(page)
            started = time.perf_counter()
            try:
                uid = self.read_uid(rect, before, bands[indices.index(index)], index+1)
                if not isinstance(uid, str) or not re.fullmatch(r'[0-9]{9}', uid):
                    raise ScanIncomplete('主页UID未能确认')
                if uid in self.uids.values():
                    raise RosterChanged('不同卡片核验到相同 UID，名单可能重排；本轮未提交')
                self.uids[index] = uid
            except RosterChanged:
                raise
            except ScanIncomplete:
                self.failed_uids.add(index)
                # Continuing is allowed only when the original page is restored.
                self.verify_visible(page)
            finally:
                self.uid_seconds += time.perf_counter()-started
        if any(index in self.uids or index in self.failed_uids for index in indices):
            rect, frame, bands = self.verify_visible(page)
            shift = median(b.top-o.band.top for b, o in zip(bands, page.observations))
            if self.origin is not None:
                self.offset -= shift
        return rect, frame

    def verify_late_ambiguities(self):
        """Revisit earlier cards only when a later page exposed a conflict."""
        def remaining():
            return {r['index'] for r in self.resolved() if r.get('uid_check_reason')
                    and r['index'] not in self.uids and r['index'] not in self.failed_uids}
        if not remaining():
            return
        self.scanner._say('后续页面发现同名或身份冲突，返回核对尚未确认的卡片UID')
        rect, frame = self.home()
        self.offset, self.origin = 0., None
        movement = None
        for _ in range(min(CONFIG.max_scrolls, 80)):
            self.check()
            page = layout.read_page(frame, should_continue=self.scanner._check_pause,
                                    interface_mode=self.interface_mode, quick=True)
            indices = self.add_page(frame, page)
            self.record_page(frame, page, indices, movement)
            rect, frame = self.after_page(rect, frame, page, indices)
            if not remaining():
                return
            if indices and max(indices) >= max(remaining()):
                break
            rect, frame, movement = self.advance_page(rect, frame)
        raise ScanIncomplete('未能返回身份冲突的卡片核验UID，保留上次完整名单')

    @retained_scan_round
    def run(self):
        complete, error, comparison = False, '', {}
        self.commit_enabled = False
        try:
            rect, frame = self.home()
            movement = None
            for _ in range(min(CONFIG.max_scrolls, 80)):
                self.check()
                cached = getattr(self, 'home_page', None)
                if cached is not None and cached[0] is frame:
                    page = cached[1]
                    self.home_page = None
                else:
                    page = layout.read_page(frame, should_continue=self.scanner._check_pause,
                                            interface_mode=self.interface_mode, quick=True)
                indices = self.add_page(frame, page)
                self.record_page(frame, page, indices, movement)
                if self.online_only:
                    states, reads = read_quick_actions(frame, page.observations, self.interface_mode)
                    self.action_evidence.append({'page': self.frame_count, 'states': states, 'reads': reads})
                    # The game can mix online/offline cards within one page.
                    # Process every complete card before ending this prefix.
                    for index, obs, state in zip(indices, page.observations, states):
                        self.slots[index].entry = replace(
                            obs.entry, status=state,
                            qianxing_activity=obs.entry.qianxing_activity if state == 'online' else None,
                            qianxing_activity_verified=obs.entry.qianxing_activity_verified and state == 'online')
                        if state != 'offline':
                            self.active_indices.add(index)
                        else:
                            self.active_indices.discard(index)
                    if 'offline' in states:
                        self.boundary = min(i for i, s in zip(indices, states) if s == 'offline')
                else:
                    self.active_indices.update(indices)
                rect, frame = self.after_page(rect, frame, page, indices)
                if self.boundary is not None or set(self.slots) == set(range(self.expected)):
                    break
                rect, frame, movement = self.advance_page(rect, frame)
            else:
                raise ScanIncomplete('达到最大页数，完整性未确认')
            self.verify_late_ambiguities()
            resolved = self.resolved(include_offline=True)
            self.records = [r for r in resolved if r['index'] in self.active_indices]
            for record in self.records:
                picture = self.profile_images.get(record.get('uid'))
                if picture is not None:
                    record['profile_image'] = picture
            self.check()
            _, final = self.shot()
            title, total = layout.read_header(final)
            if not title or total is None:
                raise ScanIncomplete('提交前无法确认好友列表')
            if total != self.expected:
                raise RosterChanged('提交前好友人数发生变化', 'count_changed')
            stamp = now_str()
            if self.online_only:
                # A card actually seen offline can prove a transition; cards
                # below the scanned page are only unobserved, not logged out.
                offline = [r for r in resolved if r['index'] not in self.active_indices
                           and r.get('id') is not None and not r.get('identity_review_key')
                           and r['entry'].status == 'offline']
                ids = self.scanner.db.apply_online_scan(self.records, stamp,
                    time.perf_counter()-self.started, str(self.evidence), identity_complete=False,
                    observed_offline=offline)
            else:
                ids = self.scanner.db.apply_verified_scan(self.records, stamp,
                    time.perf_counter()-self.started, str(self.evidence), comparison=comparison,
                    identity_complete=False)
            self.scanner._seen_ids = set(ids)
            complete = True
        except Exception as exc:
            error = str(exc)
            self.failure_reason = exc.reason if isinstance(exc, RosterChanged) else 'incomplete'
            if not isinstance(exc, ScanIncomplete):
                logger.exception('快速扫描异常')
        return self.finish_quick(complete, error, comparison)

    def finish_quick(self, complete, error, comparison):
        from app.scanner import ScanResult
        self.finish_field_reviews(complete)
        elapsed = time.perf_counter()-self.started
        rows = self.scanner.db.export_rows()
        online = sum(r.get('current_status') == 'online' for r in rows)
        offline = sum(r.get('current_status') == 'offline' for r in rows)
        warnings = sorted({r['identity_review_reason'] for r in self.records if r.get('identity_review_reason')})
        pending = sum(bool(r.get('nickname_pending')) for r in rows)
        note = ('%s完成：采集%d位，单独核验%d位好友UID，耗时%.1f秒' %
                (self.phase, len(self.records), len(self.uids), elapsed) if complete else
                '%s未完成：%s；保留原名单' % (self.phase, error))
        if complete and pending:
            note += '；%d个昵称保留截图，待人工填写' % pending
        if complete and comparison.get('basis') in ('uid', 'local_id'):
            note += '；新增%d位，消失/可能被删%d位，确认改名%d位' % tuple(
                len(comparison.get(key, [])) for key in ('added', 'removed', 'renamed'))
        if complete:
            note += comparison_identity_note(comparison)
        if warnings:
            note += '；'+'；'.join(warnings)
        unknown = sum(r['entry'].status == 'unknown' for r in self.records)
        if complete and unknown:
            note += '；%d位身份或状态待确认，未累计在线时长' % unknown
        report = dict(complete=complete, mode='online' if self.online_only else 'quick',
                      scan_time=now_str(), account_uid=self.scanner.account_uid,
                      interface_mode=self.interface_mode, expected_total=self.expected,
                      collected=len(self.records), pages=self.frame_count, tail_probes=self.tail_probes,
                      uid_count=len(self.uids), failed_uid_count=len(self.failed_uids),
                      uid_seconds=self.uid_seconds, quick_seconds=max(0., elapsed-self.uid_seconds),
                      workflow_seconds=elapsed, passes=[str(self.evidence)], restarts=0,
                      comparison=comparison, pending_nicknames=pending, review_warnings=warnings,
                      field_reviews=self.nickname_reviews,
                      unknown_count=unknown,
                      identity_sources=[dict(position=r['index']+1, basis=r['match_basis'],
                          uid=r.get('uid'), local_id=r.get('id'),
                          identity_candidate_ids=r.get('identity_candidate_ids', [])) for r in self.records],
                      action_evidence=self.action_evidence, offline_boundary=self.boundary,
                      committed=complete, error=error, message=note)
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        (self.evidence/'workflow.json').write_text(payload, encoding='utf-8')
        (self.evidence/'result.json').write_text(payload, encoding='utf-8')
        (self.scanner.evidence_root/'workflow-latest.json').write_text(payload, encoding='utf-8')
        self.scanner._say(note)
        return ScanResult(now_str(), len(rows), online, offline, rows,
            self.scanner.db.get_recent_events(limit=300), note, complete=complete,
            mode='online' if self.online_only else 'scan',
            quick_seconds=max(0., elapsed-self.uid_seconds), uid_seconds=self.uid_seconds,
            workflow_seconds=elapsed,
            uid_complete=complete and all(re.fullmatch(r'[0-9]{9}', r.get('uid') or '') for r in self.records),
            comparison=comparison)

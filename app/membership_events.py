"""Separate archive/identity observations from confirmed roster changes."""
from __future__ import annotations

import json

from app.status_events import has_event_identity

PENDING_REMOVALS_KEY = 'membership_pending_removals'


def unresolved_identity(row):
    return not row.get('uid') and (not str(row.get('nickname') or '').strip()
        or row.get('nickname_pending') or row.get('identity_review_key')
        or row.get('identity_candidate_ids'))


def pending_removals(conn):
    saved = conn.execute('SELECT value FROM account_metadata WHERE key=?', (PENDING_REMOVALS_KEY,)).fetchone()
    return json.loads(saved[0]) if saved else []


def save_pending_removals(conn, rows):
    conn.execute('INSERT OR REPLACE INTO account_metadata(key,value) VALUES (?,?)',
                 (PENDING_REMOVALS_KEY, json.dumps(rows, ensure_ascii=False)))


def defer_unresolved_removals(previous, current, comparison):
    """An unidentified card may be a missing archive, not a missing friend.

    Candidate IDs exclude unrelated disappearances. A new unreadable card
    without candidates leaves all unmatched identities unresolved; we cannot
    pick a deleted friend by count, position, avatar, or fuzzy text. Existing
    unresolved cards retain their candidate scope for subsequent comparisons.
    """
    if previous is None or comparison.get('basis') not in ('uid', 'local_id'):
        return []
    old_by_id = {row['id']: row for row in previous['rows'] if row.get('id') is not None}
    old_by_uid = {row['uid']: row for row in previous['rows'] if row.get('uid')}
    missing = {}
    for item in comparison.get('removed', []):
        old = old_by_id.get(item.get('id')) or old_by_uid.get(item.get('uid'))
        if old is not None:
            missing[old['id']] = (item, old)
    blocked = set()
    for row in current:
        old = old_by_id.get(row.get('id'), {})
        inherited = old.get('identity_candidate_ids') or []
        if not row.get('uid') and inherited:
            row['identity_candidate_ids'] = sorted(set(inherited) | set(row.get('identity_candidate_ids') or []))
        if not unresolved_identity(row):
            continue
        candidates = row.get('identity_candidate_ids')
        if candidates:
            affected = set(candidates) & missing.keys()
        elif row.get('id') not in old_by_id:
            affected = set(missing)
        else:
            # A previously unresolved card that remains matched is not a
            # reason to suppress a separate, identifiable friend's loss.
            affected = set()
        blocked.update(affected)
        if affected:
            row['identity_candidate_ids'] = sorted(set(candidates or []) | affected)
    if not blocked:
        return []
    comparison['unconfirmed_removed'] = [missing[fid][0] for fid in sorted(blocked)]
    comparison['removed'] = [item for item in comparison['removed']
                             if item not in comparison['unconfirmed_removed']]
    comparison['identity_warning'] = '仍有卡片身份未确认，相关旧档案暂不判断消失；后续扫描继续核对'
    return [missing[fid][1] for fid in sorted(blocked)]


def find_archive(conn, item):
    if item.get('id') is not None:
        row = conn.execute('SELECT * FROM friends WHERE id=?', (item['id'],)).fetchone()
    elif item.get('uid'):
        rows = conn.execute('SELECT * FROM friends WHERE uid=?', (item['uid'],)).fetchall()
        row = rows[0] if len(rows) == 1 else None
    else:
        row = None
    return dict(row) if row is not None else None


def was_removed(conn, row, scan_time):
    """A confirmed disappearance permits a later return to count as an addition."""
    event = conn.execute('''SELECT event_type FROM status_events
        WHERE (friend_id=? OR (uid IS NOT NULL AND uid<>'' AND uid=?))
          AND event_type IN ('added','removed') AND event_time<? ORDER BY id DESC LIMIT 1''',
        (row['id'], row.get('uid'), scan_time)).fetchone()
    return event is not None and event['event_type'] == 'removed'


def classify_additions(conn, comparison, scan_time):
    """Do not guess which formerly anonymous card became a recognized archive."""
    actual = []
    for item in comparison.get('added', []):
        row = find_archive(conn, item)
        already_added = row and comparison.get('previous_scan_time') and conn.execute('''SELECT 1
            FROM status_events WHERE friend_id=? AND event_type='added' AND event_time>? AND event_time<?''',
            (row['id'], comparison['previous_scan_time'], scan_time)).fetchone()
        if comparison.get('basis') == 'uid' or already_added or (row and was_removed(conn, row, scan_time)):
            actual.append(item)
        elif row and row.get('first_seen_at') and row['first_seen_at'] < scan_time:
            comparison.setdefault('reidentified', []).append(item)
        elif comparison.get('unidentified_removed'):
            comparison.setdefault('unconfirmed_added', []).append(item)
        else:
            actual.append(item)
    comparison['added'] = actual


def reclassify_event(conn, event_id, kind):
    conn.execute('''UPDATE status_events SET original_event_type=COALESCE(original_event_type,event_type),
                    event_type=? WHERE id=?''', (kind, event_id))


def resolve_archive_membership(conn, keep, other):
    """A newly-created alias confirmed by UID must not leave a fake add/loss pair."""
    owner_present = conn.execute('SELECT 1 FROM verified_roster WHERE friend_id=?', (keep['id'],)).fetchone()
    additions = conn.execute('''SELECT id,event_time FROM status_events
        WHERE friend_id=? AND event_type='added' AND event_time=?''',
        (other['id'], other.get('first_seen_at'))).fetchall()
    for added in additions:
        removed = conn.execute('''SELECT id FROM status_events
            WHERE friend_id=? AND event_type='removed' AND event_time=?''',
            (keep['id'], added['event_time'])).fetchall()
        if not owner_present and not removed:
            continue
        reclassify_event(conn, added['id'], 'identity_resolved')
        for event in removed:
            reclassify_event(conn, event['id'], 'identity_resolved')
        for saved in conn.execute('SELECT * FROM roster_comparisons WHERE scan_time=?',
                                  (added['event_time'],)).fetchall():
            details = json.loads(saved['details_json'])
            for kind, fid in (('added', other['id']), ('removed', keep['id'] if removed else None)):
                resolved = [item for item in details.get(kind, []) if fid is not None and item.get('id') == fid]
                if resolved:
                    details[kind] = [item for item in details[kind] if item not in resolved]
                    details.setdefault('identity_resolved', []).extend(dict(item, original_change=kind) for item in resolved)
            conn.execute('''UPDATE roster_comparisons SET details_json=?,
                original_details_json=COALESCE(original_details_json,details_json) WHERE id=?''',
                (json.dumps(details, ensure_ascii=False), saved['id']))


def migrate_membership_events(conn):
    """Upgrade old event snapshots in one caller-owned transaction; keep originals.

    Only exact UID or a unique saved comparison entry can link an old event to
    an archive. No nickname-only matching against today's friends is performed.
    """
    cols = {r[1] for r in conn.execute('PRAGMA table_info(status_events)')}
    if {'friend_id', 'original_event_type'} <= cols:
        return
    for name, declaration in (('friend_id', 'INTEGER'), ('original_event_type', 'TEXT')):
        if name not in cols:
            conn.execute(f'ALTER TABLE status_events ADD COLUMN {name} {declaration}')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_status_events_friend ON status_events(friend_id)')
    comparison_cols = {r[1] for r in conn.execute('PRAGMA table_info(roster_comparisons)')}
    if 'original_details_json' not in comparison_cols:
        conn.execute('ALTER TABLE roster_comparisons ADD COLUMN original_details_json TEXT')
    conn.execute('''UPDATE status_events SET friend_id=(SELECT MIN(id) FROM friends
        WHERE friends.uid=status_events.uid HAVING COUNT(*)=1)
        WHERE uid IS NOT NULL AND TRIM(uid)<>'' ''')
    for saved in conn.execute('SELECT * FROM roster_comparisons ORDER BY id').fetchall():
        comparison = json.loads(saved['details_json'])
        events = [dict(row) for row in conn.execute(
            "SELECT * FROM status_events WHERE event_time=? AND event_type IN ('added','removed')",
            (saved['scan_time'],))]
        # Persist only unambiguous links from the original comparison.
        for event in events:
            candidates = [item for item in comparison.get(event['event_type'], [])
                          if (item.get('uid') or '') == (event.get('uid') or '')
                          and (item.get('nickname') or '') == (event.get('nickname') or '')]
            if len(candidates) == 1:
                archive = find_archive(conn, candidates[0])
                if archive:
                    event['friend_id'] = archive['id']
                    conn.execute('UPDATE status_events SET friend_id=? WHERE id=?',
                                 (archive['id'], event['id']))
        for kind in ('added', 'removed'):
            unknown = [item for item in comparison.get(kind, []) if not has_event_identity(item)]
            if unknown:
                comparison['unidentified_' + kind] = unknown
                comparison[kind] = [item for item in comparison[kind] if has_event_identity(item)]
        classify_additions(conn, comparison, saved['scan_time'])
        for event in events:
            kind = event['event_type']
            if not has_event_identity(event):
                kind = 'unidentified_' + kind
            elif kind == 'added' and not comparison.get('previous_scan_time'):
                kind = 'observed_friend'
            elif kind == 'added':
                for category in ('reidentified', 'unconfirmed_added'):
                    if any((item.get('id') is not None and item['id'] == event.get('friend_id'))
                           or (item.get('uid') and item['uid'] == event.get('uid'))
                           for item in comparison.get(category, [])):
                        kind = category
                        break
            if kind != event['event_type']:
                reclassify_event(conn, event['id'], kind)
        revised = json.dumps(comparison, ensure_ascii=False)
        if comparison != json.loads(saved['details_json']):
            conn.execute('''UPDATE roster_comparisons SET details_json=?,
                original_details_json=COALESCE(original_details_json,details_json) WHERE id=?''',
                (revised, saved['id']))
    for row in conn.execute("SELECT * FROM status_events WHERE event_type IN ('added','removed','online','offline')").fetchall():
        if not has_event_identity(dict(row)):
            reclassify_event(conn, row['id'], 'unidentified_' + row['event_type'])


def comparison_identity_note(comparison):
    parts = []
    if comparison.get('uid_completed'):
        parts.append('补全UID%d位（不计新增）' % len(comparison['uid_completed']))
    if comparison.get('reidentified'):
        parts.append('重新识别已有档案%d位（不计新增）' % len(comparison['reidentified']))
    if comparison.get('unconfirmed_removed'):
        parts.append('%d位旧好友身份待确认，暂不判断消失' % len(comparison['unconfirmed_removed']))
    pending = sum(len(comparison.get(key, [])) for key in
                  ('unidentified_added', 'unidentified_removed', 'unconfirmed_added'))
    if pending:
        parts.append('%d条身份变化待确认，未计入好友增减' % pending)
    return ('；' + '；'.join(parts)) if parts else ''

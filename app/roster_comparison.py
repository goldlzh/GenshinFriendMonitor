"""Compare complete rosters independently of their total size or UID coverage."""
from collections import Counter
import re

from app.status_events import has_event_identity


def valid_uid_roster(rows):
    uids = [r.get('uid') for r in rows]
    return (all(isinstance(uid, str) and re.fullmatch(r'[0-9]{9}', uid) for uid in uids)
            and len(set(uids)) == len(uids))


def _valid_local_roster(rows):
    ids = [r.get('id') for r in rows]
    return all(isinstance(fid, int) and fid > 0 for fid in ids) and len(set(ids)) == len(ids)


def compare_rosters(previous, current):
    if previous is None:
        return {'basis': 'skipped', 'reason': '此前没有成功记录的完整名单',
                'added': [], 'removed': [], 'renamed': []}
    old = previous['rows']
    result = {'previous_scan_time': previous['scan_time'],
              'previous_total': previous.get('total', len(old)), 'current_total': len(current), 'renamed': []}
    if valid_uid_roster(old) and valid_uid_roster(current):
        a, b = ({r['uid']: r for r in rows} for rows in (old, current))
        result.update(basis='uid',
                      added=[{'uid': u, 'nickname': b[u]['nickname']} for u in sorted(b.keys()-a.keys())],
                      removed=[{'uid': u, 'nickname': a[u]['nickname']} for u in sorted(a.keys()-b.keys())],
                      renamed=[{'uid': u, 'before': a[u]['nickname'], 'after': b[u]['nickname']}
                               for u in sorted(a.keys() & b.keys()) if a[u]['nickname'] != b[u]['nickname']
                               and not a[u].get('nickname_pending') and not b[u].get('nickname_pending')])
    elif _valid_local_roster(old) and _valid_local_roster(current):
        # Local IDs are assigned by the identity resolver and normalized after
        # confirmed UID merges. They are not UIDs or traversal positions.
        a, b = ({r['id']: r for r in rows} for rows in (old, current))
        def item(row):
            return {key: row.get(key) for key in ('id', 'uid', 'nickname')}
        result.update(basis='local_id',
                      reason='按前后完整名单的本地档案比较；消失表示未匹配到，不证明对方删除了好友',
                      added=[item(b[fid]) for fid in sorted(b.keys()-a.keys())],
                      removed=[item(a[fid]) for fid in sorted(a.keys()-b.keys())])
        # A missing UID elsewhere must not hide a separately confirmed rename.
        result['renamed'] = [dict(uid=b[fid]['uid'], before=a[fid]['nickname'], after=b[fid]['nickname'])
                             for fid in sorted(a.keys() & b.keys())
                             if a[fid].get('uid') and a[fid]['uid'] == b[fid].get('uid')
                             and a[fid]['nickname'] != b[fid]['nickname']
                             and not a[fid].get('nickname_pending') and not b[fid].get('nickname_pending')]
    elif any(r.get('nickname_pending') for r in old+current):
        result.update(basis='skipped', reason='缺少可对应的档案身份，暂不推断名单变化', added=[], removed=[])
    else:
        # Compatibility for callers supplying only names, without persisted IDs.
        a, b = (Counter(r['nickname'] for r in rows) for rows in (old, current))
        result.update(basis='nickname', reason='仅比较昵称及人数，不推断身份或改名',
                      added=[{'nickname': n, 'count': count} for n, count in (b-a).items()],
                      removed=[{'nickname': n, 'count': count} for n, count in (a-b).items()])
    # Retain unidentifiable differences for diagnostics, never as friend changes.
    for kind in ('added', 'removed'):
        unknown = [row for row in result[kind] if not has_event_identity(row)]
        if unknown:
            result['unidentified_' + kind] = unknown
            result[kind] = [row for row in result[kind] if has_event_identity(row)]
    return result

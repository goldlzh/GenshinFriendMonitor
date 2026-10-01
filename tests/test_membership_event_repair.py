"""Legacy membership repair retains raw evidence and never guesses identities."""
from tests import test_regressions as baseline

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.database import Database, _SCHEMA
from app.membership_events import migrate_membership_events


class MembershipEventRepairTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.addCleanup(self.conn.close)

    def archive(self, fid, uid, name, at=baseline.T0):
        self.conn.execute('INSERT INTO friends(id,uid,nickname,first_seen_at) VALUES (?,?,?,?)',
                          (fid, uid, name, at))

    def event(self, kind, uid=None, name='', at=baseline.T1):
        return self.conn.execute('INSERT INTO status_events(uid,nickname,event_type,event_time) VALUES (?,?,?,?)',
                                 (uid, name, kind, at)).lastrowid

    def comparison(self, added=(), removed=(), *, basis='local_id', at=baseline.T1):
        details = dict(basis=basis, previous_scan_time=baseline.T0,
                       added=list(added), removed=list(removed), renamed=[])
        self.conn.execute('INSERT INTO roster_comparisons(scan_time,details_json) VALUES (?,?)',
                          (at, json.dumps(details)))
        return details

    def repair(self):
        self.conn.commit()
        with self.conn:
            self.conn.execute('BEGIN IMMEDIATE')
            migrate_membership_events(self.conn)

    def events(self):
        return [dict(r) for r in self.conn.execute('SELECT * FROM status_events ORDER BY id')]

    def test_blank_events_are_retained_but_reclassified_and_unlinked(self):
        for kind in ('added', 'removed', 'online', 'offline'):
            self.event(kind, name=' \t\n')
        self.repair()
        self.assertEqual(['unidentified_added', 'unidentified_removed', 'unidentified_online',
                          'unidentified_offline'], [r['event_type'] for r in self.events()])
        self.assertEqual(['added', 'removed', 'online', 'offline'],
                         [r['original_event_type'] for r in self.events()])
        self.assertTrue(all(r['friend_id'] is None for r in self.events()))

    def test_initial_scan_is_observation_not_mass_additions(self):
        self.archive(1, '123456789', '好友')
        self.event('added', '123456789', '好友', baseline.T0)
        details = dict(basis='skipped', reason='首次扫描', added=[], removed=[], renamed=[])
        self.conn.execute('INSERT INTO roster_comparisons(scan_time,details_json) VALUES (?,?)',
                          (baseline.T0, json.dumps(details)))
        self.repair()
        self.assertEqual('observed_friend', self.events()[0]['event_type'])
        self.assertEqual('added', self.events()[0]['original_event_type'])
        self.assertEqual(1, self.events()[0]['friend_id'])

    def test_reidentification_and_unidentified_loss_are_not_claimed_as_friend_changes(self):
        self.archive(1, '123456789', '旧档案')
        self.archive(2, None, '')
        self.archive(3, '223456789', '新识别', baseline.T1)
        self.event('added', '123456789', '旧档案')
        self.event('added', '223456789', '新识别')
        self.event('removed')
        original = self.comparison(
            [dict(id=1, uid='123456789', nickname='旧档案'), dict(id=3, uid='223456789', nickname='新识别')],
            [dict(id=2, uid=None, nickname='')])
        friends = [tuple(r) for r in self.conn.execute('SELECT * FROM friends')]
        self.repair()
        self.assertEqual(['reidentified', 'unconfirmed_added', 'unidentified_removed'],
                         [r['event_type'] for r in self.events()])
        saved = self.conn.execute('SELECT * FROM roster_comparisons').fetchone()
        updated = json.loads(saved['details_json'])
        self.assertEqual(([], []), (updated['added'], updated['removed']))
        self.assertEqual(original, json.loads(saved['original_details_json']))
        self.assertEqual(friends, [tuple(r) for r in self.conn.execute('SELECT * FROM friends')])

    def test_known_disappearance_and_return_remain_even_without_uid(self):
        self.archive(1, None, '旧好友')
        lost = dict(id=1, uid=None, nickname='旧好友')
        self.event('removed', name='旧好友')
        self.comparison(removed=[lost])
        self.event('added', name='旧好友', at=baseline.T2)
        self.comparison(added=[lost], at=baseline.T2)
        self.repair()
        self.assertEqual(['removed', 'added'], [r['event_type'] for r in self.events()])
        self.assertEqual([1, 1], [r['friend_id'] for r in self.events()])
        self.assertTrue(all(r['original_event_type'] is None for r in self.events()))

    def test_uid_based_new_member_is_not_suppressed_just_because_archive_exists(self):
        self.archive(1, '123456789', '旧档案')
        self.event('added', '123456789', '旧档案')
        self.comparison([dict(uid='123456789', nickname='旧档案')], basis='uid')
        self.repair()
        self.assertEqual('added', self.events()[0]['event_type'])

    def test_same_name_alone_cannot_link_a_legacy_event(self):
        self.archive(1, None, '同名')
        self.archive(2, '123456789', '同名')
        self.event('removed', name='同名')
        self.repair()
        self.assertIsNone(self.events()[0]['friend_id'])
        self.assertEqual('removed', self.events()[0]['event_type'])

    def test_repair_is_idempotent(self):
        self.event('added')
        self.repair()
        before = list(self.conn.iterdump())
        self.repair()
        self.assertEqual(before, list(self.conn.iterdump()))

    def test_failure_rolls_back_schema_events_and_comparisons(self):
        self.event('added')
        self.conn.execute('''CREATE TRIGGER reject_repair BEFORE UPDATE ON status_events
            BEGIN SELECT RAISE(ABORT,'isolated failure'); END''')
        self.conn.commit()
        before = list(self.conn.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            self.repair()
        self.assertEqual(before, list(self.conn.iterdump()))

    def test_database_upgrade_backs_up_original_once(self):
        with tempfile.TemporaryDirectory(dir=baseline.WORK) as folder:
            path = Path(folder) / 'old.db'
            self.event('added')
            self.conn.commit()
            with closing(sqlite3.connect(path)) as target:
                self.conn.backup(target)
            db = Database(str(path))
            try:
                self.assertEqual([], db.get_recent_events(changes_only=True))
                self.assertEqual('unidentified_added', db.export_events()[0]['event_type'])
            finally:
                db.close()
            backups = list(path.parent.glob(path.name + '.backup-*'))
            self.assertEqual(1, len(backups))
            with closing(sqlite3.connect(backups[0])) as original:
                self.assertEqual('added', original.execute('SELECT event_type FROM status_events').fetchone()[0])
            Database(str(path)).close()
            self.assertEqual(backups, list(path.parent.glob(path.name + '.backup-*')))


if __name__ == '__main__':
    unittest.main()

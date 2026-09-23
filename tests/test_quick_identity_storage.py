"""Nullable UID snapshots retain local history without resolving ambiguity."""
from tests import test_regressions as baseline
from tests import test_presence_monitoring as presence

from pathlib import Path
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest

import numpy as np

from app.database import Database, _SCHEMA
from app.fast_scan import _png
from app.quick_scan import QuickIdentities


def record(uid=None, *, fid=None, name='旅行者', status='online', review=None):
    item = presence.record(uid, name=name, status=status)
    item['id'] = fid
    if review is not None:
        item.update(identity_review_key=review, identity_review_reason='同名同签名，请在游戏内加备注')
    return item


class QuickIdentityStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)

    def full(self, items, seconds=0, **kwargs):
        return self.db.apply_verified_scan(items, presence.at(seconds), 1., 'isolated quick scan', **kwargs)

    def test_mixed_snapshot_is_stable_when_live_fields_change(self):
        ids = self.full([record('123456789'), record(name='新好友')])
        snapshot = self.db.get_complete_roster()
        self.assertEqual(ids, [r['id'] for r in snapshot['rows']])
        self.assertEqual(['123456789', None], [r['uid'] for r in snapshot['rows']])
        self.db.update_status(ids[1], 'offline', '后来的昵称', 59, presence.at(60))
        self.assertEqual(snapshot, self.db.get_complete_roster())
        self.assertEqual('后来的昵称', self.db.get_friend(ids[1])['nickname'])

    def test_explicit_pending_id_gains_uid_without_losing_online_history_or_merging_namesakes(self):
        self.db.configure_monitoring(0, at=presence.T0)
        pending, namesake = self.full([record(name='同名'), record(name='同名', status='offline')])
        self.full([record(fid=pending, name='同名'), record(fid=namesake, name='同名', status='offline')], 60)
        self.full([record('123456789', fid=pending, name='同名'),
                   record(fid=namesake, name='同名', status='offline')], 120)
        self.full([record('123456789', fid=pending, name='同名', status='offline'),
                   record(fid=namesake, name='同名', status='offline')], 180)
        row = self.db.get_friend(pending)
        self.assertEqual(('123456789', 180), (row['uid'], row['cumulative_online_seconds']))
        self.assertIsNone(self.db.get_friend(namesake)['uid'])
        sessions = list(self.db._conn.execute('SELECT * FROM online_sessions WHERE friend_id=?', (pending,)))
        self.assertEqual(1, len(sessions))
        self.assertEqual(('123456789', 180), (sessions[0]['uid'], sessions[0]['duration_seconds']))
        self.assertEqual(2, len(self.db.get_all_friends()))

    def test_explicit_pending_id_merges_only_with_confirmed_uid_owner_and_retains_both_histories(self):
        self.db.configure_monitoring(0, at=presence.T0)
        owner = self.full([record('123456789', name='旧昵称')], identity_complete=True)[0]
        self.db.update_status(owner, 'offline', None, None, presence.at(60))
        pending = self.db.add_friend(None, '新昵称', 60, None, 'online', presence.at(120))
        namesake = self.db.add_friend(None, '新昵称', 60, None, 'offline', presence.at(120))
        self.full([record(fid=pending, name='新昵称')], 180)
        current = self.full([record('123456789', fid=pending, name='新昵称', status='offline')], 240,
                            identity_complete=True)
        self.assertEqual([owner], current)
        self.assertIsNone(self.db.get_friend(pending))
        self.assertIsNone(self.db.get_friend(namesake)['uid'])
        self.assertEqual(180, self.db.get_friend(owner)['cumulative_online_seconds'])
        sessions = list(self.db._conn.execute('SELECT * FROM online_sessions WHERE friend_id=? ORDER BY start_time', (owner,)))
        self.assertEqual([60, 120], [row['duration_seconds'] for row in sessions])
        self.assertTrue(all(row['uid'] == '123456789' for row in sessions))
        self.assertEqual(owner, self.db.get_complete_roster()['rows'][0]['id'])

    def test_online_uid_resolution_updates_pending_baseline_id_without_changing_scan_stamp(self):
        owner = self.db.add_friend('123456789', '旧名', 60, None, 'offline', presence.T0)
        pending = self.full([record(name='新名')])[0]
        original_stamp = self.db.get_complete_roster()['scan_time']
        self.db.apply_online_scan([record('123456789', fid=pending, name='新名')],
                                  presence.at(60), 1., 'UID confirmation')
        baseline_row = self.db.get_complete_roster()
        self.assertEqual(original_stamp, baseline_row['scan_time'])
        self.assertEqual((owner, '123456789'), (baseline_row['rows'][0]['id'], baseline_row['rows'][0]['uid']))
        self.assertIsNone(self.db.get_friend(pending))

    def test_wrong_explicit_uid_binding_and_late_merge_failure_are_atomic(self):
        first, second = self.full([record('123456789'), record('223456789')], identity_complete=True)
        pending = self.db.add_friend(None, '临时', 60, None, 'online', presence.at(20))
        before = list(self.db._conn.iterdump())
        with self.assertRaises(ValueError):
            self.full([record('123456789', fid=second)], 60)
        self.assertEqual(before, list(self.db._conn.iterdump()))
        malformed = record('323456789')
        malformed['name_image'] = None
        with self.assertRaises(sqlite3.IntegrityError):
            self.full([record('123456789', fid=pending), malformed], 60)
        self.assertEqual(before, list(self.db._conn.iterdump()))
        self.assertIsNotNone(self.db.get_friend(first))

    def test_online_uid_binding_resolves_saved_pending_identity_without_rewriting_observed_fields(self):
        pending = self.full([record(name='待备注', status='unknown', review='same-signature')])[0]
        previous = self.db.get_complete_roster()
        self.db.apply_online_scan([record('123456789', fid=pending, name='已确认')],
                                  presence.at(60), 1., 'confirmed online')
        snapshot = self.db.get_complete_roster()
        self.assertEqual(previous['scan_time'], snapshot['scan_time'])
        self.assertEqual(('123456789', None, None),
                         tuple(snapshot['rows'][0][key] for key in
                               ('uid', 'identity_review_key', 'identity_review_reason')))
        self.assertEqual('待备注', snapshot['rows'][0]['nickname'])
        self.assertEqual('unknown', snapshot['rows'][0]['current_status'])

    def test_nonempty_invalid_uids_and_duplicate_ids_or_uids_are_rejected_in_mixed_mode(self):
        existing = self.full([record('123456789')])[0]
        before = list(self.db._conn.iterdump())
        batches = ([record('bad')], [record(123456789)], [record('123456789'), record('123456789')],
                   [record(fid=existing), record(fid=existing)])
        for online in (False, True):
            for batch in batches:
                with self.subTest(online=online, batch=batch), self.assertRaises(ValueError):
                    if online:
                        self.db.apply_online_scan(batch, presence.at(60), 1., 'invalid', identity_complete=False)
                    else:
                        self.full(batch, 60)
                self.assertEqual(before, list(self.db._conn.iterdump()))

    def test_online_mixed_mode_keeps_null_uid_and_unknown_separate_from_offline(self):
        self.db.configure_monitoring(0, at=presence.T0)
        owner = self.full([record('123456789')], identity_complete=True)[0]
        previous = self.db.get_complete_roster()
        items = [record(name='尚无UID'), record(name='同名', status='unknown', review='same-name-signature')]
        new_id, review_id = self.db.apply_online_scan(items, presence.at(60), 1., 'online quick',
                                                     identity_complete=False)
        self.assertEqual(previous, self.db.get_complete_roster())
        self.assertIsNone(self.db.get_friend(new_id)['uid'])
        self.assertEqual('offline', self.db.get_friend(owner)['current_status'])
        unknown = self.db.get_friend(review_id)
        self.assertEqual(('unknown', None, 0), (unknown['current_status'], unknown['first_online_at'],
                                              unknown['cumulative_online_seconds']))
        counts = self.db._conn.execute('SELECT total_friends,online_count,offline_count FROM scan_sessions ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual((3, 1, 1), tuple(counts))
        with self.assertRaises(ValueError):
            self.db.apply_online_scan([record()], presence.at(120), 1., 'strict legacy caller')

    def test_unknown_full_record_does_not_add_online_or_offline_count(self):
        self.db.configure_monitoring(0, at=presence.T0)
        pending = self.full([record(status='unknown', review='ambiguous-group')])[0]
        self.full([record(fid=pending, status='unknown', review='ambiguous-group')], 120)
        row = self.db.get_friend(pending)
        self.assertEqual('unknown', row['current_status'])
        self.assertEqual(0, row['cumulative_online_seconds'])
        self.assertIsNone(row['first_online_at'])
        counts = self.db._conn.execute('SELECT total_friends,online_count,offline_count FROM scan_sessions ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual((1, 0, 0), tuple(counts))
        self.assertEqual([], list(self.db._conn.execute('SELECT * FROM online_sessions')))

    def test_becoming_unknown_settles_time_without_an_offline_event_or_further_accrual(self):
        self.db.configure_monitoring(0, at=presence.T0)
        pending = self.full([record()])[0]
        self.full([record(fid=pending, status='unknown', review='needs-note')], 60)
        self.full([record(fid=pending, status='unknown', review='needs-note')], 120)
        row = self.db.get_friend(pending)
        self.assertEqual(('unknown', 60), (row['current_status'], row['cumulative_online_seconds']))
        events = [row[0] for row in self.db._conn.execute('SELECT event_type FROM status_events')]
        self.assertIn('unknown', events)
        self.assertNotIn('offline', events)

    def test_review_mapping_reuses_local_ids_and_manual_name_confirmation_clears_it(self):
        fid = self.full([record(status='unknown', review='review-hash')])[0]
        self.full([record(fid=fid, status='unknown')], 60)
        self.assertEqual(1, len(self.db.get_all_friends()))
        visual = self.db.get_visual_profiles()[0]
        self.assertEqual('review-hash', visual['identity_review_key'])
        self.assertIn('加备注', visual['identity_review_reason'])
        self.db.update_friend_manual(fid, None, '已填写备注', 60, '签名')
        row = self.db.get_friend(fid)
        self.assertIsNone(row['identity_review_key'])
        self.assertIsNone(row['identity_review_reason'])
        self.assertEqual('已填写备注', self.db.get_complete_roster()['rows'][0]['nickname'])
        self.assertIsNone(self.db.get_complete_roster()['rows'][0]['identity_review_key'])

    def test_explicit_clear_or_uid_confirmation_removes_review_metadata(self):
        fid = self.full([record(status='unknown', review='review-hash')])[0]
        cleared = record(fid=fid)
        cleared.update(identity_review_key=None, identity_review_reason=None)
        self.full([cleared], 60)
        self.assertIsNone(self.db.get_friend(fid)['identity_review_key'])
        self.full([record(fid=fid, status='unknown', review='another-review')], 120)
        self.full([record('123456789', fid=fid)], 180, identity_complete=True)
        self.assertIsNone(self.db.get_friend(fid)['identity_review_key'])
        self.assertIsNone(self.db.get_friend(fid)['identity_review_reason'])

    def pending_image_review(self):
        pixels = np.full((36, 120, 3), 220, np.uint8)
        pixels[7:29, 14:19] = 45
        pixels[7:12, 14:35] = 45
        pixels[17:22, 14:30] = 45
        pixels[7:29, 46:51] = 45
        pixels[24:29, 46:69] = 45
        entry = replace(baseline.entry('', status='unknown'), nickname_pending=True,
                        signature='签名', signature_confidence=.99)
        pending = record(name='', status='unknown', review='unreadable-review')
        pending.update(entry=entry, name_image=_png(pixels), avatar_image=_png(pixels))
        fid = self.full([pending])[0]
        observation = SimpleNamespace(entry=replace(entry, status='online'),
                                      name_image=pixels, avatar_image=pixels)
        return fid, observation

    def assert_manual_uid_image_identity(self, fid, observation):
        row = self.db.get_friend(fid)
        self.assertEqual(('123456789', '', 1, None, None),
                         tuple(row[key] for key in ('uid', 'nickname', 'nickname_pending',
                                                    'identity_review_key', 'identity_review_reason')))
        snapshot = self.db.get_complete_roster()
        self.assertEqual(presence.T0, snapshot['scan_time'])
        self.assertEqual((fid, '123456789', '', 1, None, None),
                         tuple(snapshot['rows'][0][key] for key in
                               ('id', 'uid', 'nickname', 'nickname_pending',
                                'identity_review_key', 'identity_review_reason')))
        original_image = self.db.get_nickname_image(fid)
        for seconds in (60, 120):
            resolved = QuickIdentities(self.db.get_all_friends(), self.db.get_visual_profiles()).resolve(
                [(0, observation)])
            self.assertEqual((fid, '123456789', 'nickname_image'),
                             tuple(resolved[0][key] for key in ('id', 'uid', 'match_basis')))
            self.assertEqual([fid], self.full(resolved, seconds))
            self.assertEqual(1, len(self.db.get_all_friends()))
            self.assertEqual(original_image, self.db.get_nickname_image(fid))

    def test_manual_uid_with_blank_nickname_clears_review_and_next_scans_reuse_image_identity(self):
        fid, observation = self.pending_image_review()
        self.db.update_friend_manual(fid, '123456789', '', 60, '签名')
        self.assert_manual_uid_image_identity(fid, observation)

    def test_manual_uid_merge_with_blank_nickname_clears_review_and_remaps_snapshot(self):
        owner = self.db.add_friend('123456789', '旧昵称', 60, '签名', 'offline', presence.T0)
        pending, observation = self.pending_image_review()
        self.db.update_friend_manual(pending, '123456789', '', 60, '签名')
        self.assertIsNone(self.db.get_friend(pending))
        self.assert_manual_uid_image_identity(owner, observation)

    def test_invalid_manual_uid_keeps_pending_review_and_snapshot_unchanged(self):
        fid, _ = self.pending_image_review()
        before = list(self.db._conn.iterdump())
        with self.assertRaises(ValueError):
            self.db.update_friend_manual(fid, '123', '', 60, '签名')
        self.assertEqual(before, list(self.db._conn.iterdump()))

    def test_ambiguous_placeholders_cannot_be_accidentally_charged_online(self):
        with self.assertRaises(ValueError):
            self.full([record(status='online', review='ambiguous')])
        self.assertEqual([], self.db.get_all_friends())


class ReviewFieldMigrationTests(unittest.TestCase):
    def test_existing_database_gains_review_fields_with_backup_and_retains_identity(self):
        with tempfile.TemporaryDirectory(dir=baseline.WORK) as directory:
            path = Path(directory) / 'old.db'
            old_schema = '\n'.join(line for line in _SCHEMA.splitlines()
                                   if 'identity_review_key' not in line and 'identity_review_reason' not in line)
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(old_schema)
                connection.execute("INSERT INTO friends(uid,nickname,current_status) VALUES ('123456789','原昵称','offline')")
                connection.commit()
            database = Database(str(path))
            try:
                row = database.get_all_friends()[0]
                self.assertEqual(('123456789', '原昵称'), (row['uid'], row['nickname']))
                self.assertIsNone(row['identity_review_key'])
                self.assertIsNone(row['identity_review_reason'])
                self.assertEqual(1, len(list(Path(directory).glob('old.db.backup-*'))))
            finally:
                database.close()


if __name__ == '__main__':
    unittest.main()

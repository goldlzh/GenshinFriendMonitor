"""Account separation and bounded, UID-bound profile evidence."""
from tests import test_regressions as _baseline  # Initialize the shared isolated runtime first.
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from app.accounts import AccountStore
from app.database import Database


T0, T1, T2 = ('2026-09-20T10:00:00', '2026-09-20T10:01:00', '2026-09-20T10:02:00')
OWNER_A, OWNER_B, FRIEND = '111111111', '222222222', '333333333'


def record(nickname='旧名字', signature='同一个签名', uid=FRIEND, **kwargs):
    parsed = SimpleNamespace(nickname=nickname, signature=signature,
                             nickname_pending=False, level=60, status='online',
                             location=None, last_seen=None)
    return dict(entry=parsed, uid=uid, name_image=b'name', avatar_image=b'avatar', **kwargs)


class AccountStorageTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.store = AccountStore(Path(self.scratch.name) / 'legacy.db')

    def database(self, uid):
        result = self.store.open_account(uid)
        self.addCleanup(result.close)
        return result

    def test_recaptured_old_image_becomes_recent_without_duplicate_storage(self):
        db=self.database(OWNER_A)
        db.apply_verified_scan([record()],T0,1,'',identity_complete=True)
        db.save_profile_capture(1,b'A',T0,signature='版本 A')
        db.save_profile_capture(1,b'B',T1,signature='版本 B')
        db.save_profile_capture(1,b'A',T2,signature='版本 A')
        db.save_profile_capture(1,b'C','2026-09-20T10:03:00',signature='版本 C')
        self.assertEqual([b'C',b'A'],[c['image'] for c in db.get_profile_captures(1)])
        db.save_profile_capture(1,b'C',T0,signature='版本 C')
        self.assertEqual('2026-09-20T10:03:00',db.get_profile_captures(1)[0]['observed_at'])

    def test_uid_metadata_is_persistent_and_cannot_rebind(self):
        database = self.database(OWNER_A)
        self.assertEqual(OWNER_A, database.account_uid)
        database.set_account_uid(OWNER_A)
        with self.assertRaises(ValueError):
            database.set_account_uid(OWNER_B)
        database.close()
        self.assertEqual(OWNER_A, self.database(OWNER_A).account_uid)
        for bad in ('../111111111', '123', 111111111, '１２３４５６７８９'):
            with self.assertRaises(ValueError):
                self.store.path_for(bad)

    def test_same_friend_uid_has_separate_rosters_signatures_and_evidence(self):
        first, second = self.database(OWNER_A), self.database(OWNER_B)
        first.apply_verified_scan([record(profile_image=b'home A')], T0, 1, '', identity_complete=True)
        second.apply_verified_scan([record(nickname='第二个账号昵称', signature='B', profile_image=b'home B')],
                                   T1, 1, '', identity_complete=True)
        self.assertEqual('同一个签名', first.get_complete_roster()['rows'][0]['signature'])
        self.assertEqual('第二个账号昵称', second.export_rows()[0]['nickname'])
        self.assertEqual(b'home A', first.get_profile_captures(1)[0]['image'])
        self.assertEqual(b'home B', second.get_profile_captures(1)[0]['image'])
        self.assertEqual(T0, first.export_events()[0]['event_time'])
        self.assertEqual(T1, second.export_events()[0]['event_time'])
        self.assertEqual([OWNER_A, OWNER_B], self.store.list_accounts())
        self.assertFalse(self.store.legacy_path.exists())

    def test_legacy_import_is_explicit_and_does_not_change_source(self):
        source = Database(str(self.store.legacy_path))
        source.apply_verified_scan([record(profile_image=b'old home')], T0, 1, '', identity_complete=True)
        source.close()
        original = self.store.legacy_path.read_bytes()
        empty = self.database(OWNER_A)
        empty.close()
        imported = self.store.import_legacy(OWNER_A)
        self.addCleanup(imported.close)
        self.assertEqual(original, self.store.legacy_path.read_bytes())
        self.assertEqual(OWNER_A, imported.account_uid)
        self.assertEqual(FRIEND, imported.get_display_friends()[0]['uid'])
        self.assertEqual(b'old home', imported.get_profile_captures(1)[0]['image'])
        imported.close()
        with self.assertRaises(ValueError):
            self.store.import_legacy(OWNER_A)
        with closing(sqlite3.connect(self.store.legacy_path)) as connection:
            self.assertIsNone(connection.execute(
                "SELECT value FROM account_metadata WHERE key='account_uid'").fetchone())

    def test_import_cannot_reassign_an_already_bound_source(self):
        source = Database(str(self.store.legacy_path))
        source.set_account_uid(OWNER_A)
        source.close()
        with self.assertRaises(ValueError):
            self.store.import_legacy(OWNER_B)
        self.assertFalse(self.store.path_for(OWNER_B).exists())

    def test_nonempty_history_prevents_import_even_without_friends(self):
        source = Database(str(self.store.legacy_path))
        source.close()
        target = self.database(OWNER_A)
        target.add_session(T0, 0, 0, 0)
        target.close()
        with self.assertRaises(ValueError):
            self.store.import_legacy(OWNER_A)


class ProfileCaptureTests(unittest.TestCase):
    def setUp(self):
        self.database = Database(':memory:')
        self.addCleanup(self.database.close)
        self.friend = self.database.add_friend(FRIEND, '名字', 60, None, 'online', T0)

    def test_only_two_newest_distinct_images_remain(self):
        self.database.save_profile_capture(self.friend, b'old', T0, 'old name')
        self.database.save_profile_capture(self.friend, b'middle', T1, 'middle name')
        self.database.save_profile_capture(self.friend, b'newest', T2, 'new name')
        self.database.save_profile_capture(self.friend, b'newest', T2, 'new name')
        captures = self.database.get_profile_captures(self.friend)
        self.assertEqual([b'newest', b'middle'], [row['image'] for row in captures])
        self.assertEqual(['new name', 'middle name'], [row['nickname'] for row in captures])
        self.assertEqual(2, self.database.profile_capture_count(self.friend))
        self.database.save_profile_capture(self.friend, b'out of order', T0)
        self.assertEqual([b'newest', b'middle'],
                         [row['image'] for row in self.database.get_profile_captures(self.friend)])

    def test_failed_roster_rolls_back_new_captures_and_signature(self):
        self.database.apply_verified_scan([record(profile_image=b'original')], T0, 1, '', identity_complete=True)
        original = self.database.get_profile_captures(self.friend)
        with self.assertRaises(ValueError):
            self.database.apply_verified_scan(
                [record(signature='new signature', profile_image=b'new'),
                 record(nickname='duplicate', profile_image=b'another')], T1, 1, '')
        self.assertEqual(original, self.database.get_profile_captures(self.friend))
        self.assertEqual('同一个签名', self.database.get_friend(self.friend)['signature'])

    def test_captures_require_verified_friend_uid_and_do_not_follow_rebinding(self):
        unknown = self.database.add_friend(None, '未确认', None, None, 'offline', T0)
        with self.assertRaises(ValueError):
            self.database.save_profile_capture(unknown, b'unsafe', T0)
        with self.assertRaises(ValueError):
            self.database.apply_verified_scan([record(uid=None, id=self.friend, profile_image=b'unsafe')], T0, 1, '')
        self.database.save_profile_capture(self.friend, b'original', T0)
        self.database.update_friend_manual(self.friend, '444444444', '名字', 60, None)
        self.assertEqual([], self.database.get_profile_captures(self.friend))

    def test_merging_same_uid_keeps_two_latest_images_and_rejects_other_uid(self):
        other = self.database.add_friend(FRIEND, '另一个名字', 60, None, 'online', T0)
        self.database.save_profile_capture(self.friend, b'first', T0)
        self.database.save_profile_capture(self.friend, b'middle', T1)
        self.database.save_profile_capture(other, b'latest', T2)
        result = self.database.resolve_uid(FRIEND, '改名后', T2)
        self.assertEqual([b'latest', b'middle'],
                         [row['image'] for row in self.database.get_profile_captures(result)])
        other_uid = self.database.add_friend('444444444', '不同好友', 60, None, 'online', T0)
        self.database.save_profile_capture(other_uid, b'different identity', T0)
        with self.assertRaises(ValueError):
            with self.database._lock, self.database._conn:
                self.database._merge_friend_locked(result, other_uid)
        self.assertEqual(b'different identity', self.database.get_profile_captures(other_uid)[0]['image'])

    def test_delete_cleans_profile_images(self):
        self.database.save_profile_capture(self.friend, b'first', T0)
        self.database.delete_friend(self.friend)
        self.assertEqual(0, self.database._conn.execute('SELECT COUNT(*) FROM profile_captures').fetchone()[0])
        for delete in (self.database.delete_friends, lambda ids: self.database.delete_all_friends()):
            friend = self.database.add_friend(FRIEND, '名字', 60, None, 'online', T0)
            self.database.save_profile_capture(friend, b'first', T0)
            delete([friend])
            self.assertEqual(0, self.database._conn.execute('SELECT COUNT(*) FROM profile_captures').fetchone()[0])

    def test_signature_clear_is_preserved_in_new_baseline(self):
        self.database.apply_verified_scan([record()], T0, 1, '', identity_complete=True)
        self.database.apply_verified_scan([record(signature='')], T1, 1, '', identity_complete=True)
        self.assertEqual('', self.database.get_complete_roster()['rows'][0]['signature'])

    def test_manual_blank_pending_nickname_keeps_screenshot(self):
        pending = record()
        pending['entry'].nickname_pending = True
        self.database.apply_verified_scan([pending], T0, 1, '', identity_complete=True)
        self.database.update_friend_manual(self.friend, FRIEND, '', 60, 'changed signature')
        self.assertEqual(1, self.database.get_friend(self.friend)['nickname_pending'])
        self.assertEqual(b'name', self.database.get_nickname_image(self.friend))
        self.assertEqual(1, self.database.get_complete_roster()['rows'][0]['nickname_pending'])


if __name__ == '__main__':
    unittest.main()

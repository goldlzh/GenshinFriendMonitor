"""Reviewed offline imports preserve live presence, history and roster evidence."""
from tests import test_regressions as baseline

from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image

from app.config import CONFIG
from app.database import Database


def picture(color='white'):
    output = BytesIO()
    Image.new('RGB', (160,96), color).save(output, format='PNG')
    return output.getvalue()


class ProfileImportStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.db.set_account_uid('111111111')
        self.addCleanup(self.db.close)
        self.image = picture()

    def save(self, uid='123456789', name='测试好友', level=60, signature='签名', **kwargs):
        return self.db.import_friend_profile(uid, name, level, signature, self.image, **kwargs)

    def baseline(self, fid):
        row = self.db.get_friend(fid)
        record = dict(id=fid, uid=row['uid'], entry=baseline.entry(row['nickname']),
                      name_image=self.image, avatar_image=self.image)
        self.db.apply_verified_scan([record], baseline.T0, 0, 'isolated')

    def test_new_record_is_unknown_not_an_online_observation_or_verified_member(self):
        old = self.db.add_friend('223456789', '原好友', 50, None, 'offline', baseline.T0)
        self.baseline(old)
        before = self.db.get_complete_roster()
        fid = self.save()
        row = self.db.get_friend(fid)
        self.assertEqual(('unknown', None, None, 0),
                         (row['current_status'], row['last_scan_at'], row['first_online_at'], row['cumulative_online_seconds']))
        self.assertEqual(before, self.db.get_complete_roster())
        self.assertEqual([old], [row['id'] for row in self.db.get_display_friends()])
        self.assertEqual('manual_import', self.db.get_recent_events()[0]['event_type'])
        self.assertEqual(1, self.db.profile_capture_count(fid))

    def test_same_uid_updates_one_record_without_changing_presence_or_sessions(self):
        self.db.configure_monitoring(0, at=baseline.T0)
        fid = self.db.add_friend('123456789', '原昵称', 50, '原签名', 'online', baseline.T0)
        self.db.update_status(fid, 'online', None, None, baseline.T1)
        before = self.db.get_friend(fid)
        imported = self.save(name='新昵称', level=None, signature=None, signature_confirmed=False)
        after = self.db.get_friend(fid)
        self.assertEqual(fid, imported)
        self.assertEqual(1, len(self.db.get_all_friends()))
        for key in ('current_status', 'last_scan_at', 'first_online_at', 'cumulative_online_seconds',
                    'last_observed_status', 'status_updated_at', 'level', 'signature'):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual('新昵称', after['nickname'])

    def test_shared_name_does_not_implicitly_merge_a_uidless_record(self):
        pending = self.db.add_friend(None, '测试好友', 40, None, 'offline', baseline.T0)
        other = self.db.add_friend('223456789', '测试好友', 30, None, 'offline', baseline.T0)
        fid = self.save()
        self.assertNotIn(fid, (pending, other))
        self.assertIsNone(self.db.get_friend(pending)['uid'])
        self.assertEqual('223456789', self.db.get_friend(other)['uid'])

    def test_explicit_pending_target_keeps_id_and_fills_baseline_uid(self):
        pending = self.db.add_friend(None, '列表备注', 40, None, 'offline', baseline.T0)
        self.baseline(pending)
        before = self.db.get_complete_roster()
        self.assertEqual(pending, self.save(friend_id=pending))
        after = self.db.get_complete_roster()
        self.assertEqual(before['scan_time'], after['scan_time'])
        self.assertEqual(pending, after['rows'][0]['id'])
        self.assertEqual('123456789', after['rows'][0]['uid'])

    def test_explicit_pending_target_merges_only_with_the_exact_uid_owner(self):
        owner = self.save()
        pending = self.db.add_friend(None, '待确认', 40, None, 'offline', baseline.T0)
        self.baseline(pending)
        result = self.save(friend_id=pending)
        self.assertEqual(owner, result)
        self.assertIsNone(self.db.get_friend(pending))
        self.assertEqual(1, len(self.db.get_all_friends()))
        self.assertEqual(owner, self.db.get_complete_roster()['rows'][0]['id'])

    def test_merge_preserves_owner_signature_when_signature_was_not_confirmed(self):
        owner = self.save(signature='已校准签名')
        pending = self.db.add_friend(None, '待确认', 40, None, 'offline', baseline.T0)
        self.save(friend_id=pending, signature=None, signature_confirmed=False)
        self.assertEqual('已校准签名', self.db.get_friend(owner)['signature'])

    def test_merging_two_current_roster_rows_cannot_corrupt_complete_baseline(self):
        owner = self.save()
        pending = self.db.add_friend(None, '待确认', 40, None, 'offline', baseline.T0)
        records = [dict(id=fid, uid=self.db.get_friend(fid)['uid'], entry=baseline.entry(str(fid)),
                        name_image=self.image, avatar_image=self.image) for fid in (owner, pending)]
        self.db.apply_verified_scan(records, baseline.T0, 0, 'isolated')
        before = list(self.db._conn.iterdump())
        with self.assertRaises(ValueError):
            self.save(friend_id=pending)
        self.assertEqual(before, list(self.db._conn.iterdump()))

    def test_different_uid_cannot_be_overwritten_and_invalid_input_is_atomic(self):
        fid = self.save('223456789')
        before = list(self.db._conn.iterdump())
        cases = (dict(friend_id=fid), dict(uid='bad'), dict(name=''), dict(level=61), dict(level=True))
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.save(**kwargs)
            self.assertEqual(before, list(self.db._conn.iterdump()))

    def test_late_archive_failure_rolls_back_both_new_and_existing_records(self):
        fid = self.save()
        for uid in ('123456789', '223456789'):
            before = list(self.db._conn.iterdump())
            with patch.object(self.db, 'save_profile_capture', side_effect=RuntimeError('disk failed')):
                with self.assertRaises(RuntimeError):
                    self.save(uid=uid, name='应回滚')
            self.assertEqual(before, list(self.db._conn.iterdump()))
        self.assertIsNotNone(self.db.get_friend(fid))

    def test_explicit_manual_image_choice_and_two_version_retention(self):
        with patch.object(CONFIG, 'save_profile_screenshots', False):
            fid = self.save(save_image=False)
            self.assertEqual(0, self.db.profile_capture_count(fid))
            for index, color in enumerate(('red', 'blue', 'green')):
                self.image = picture(color)
                with patch('app.database.now_str', return_value=f'2026-09-23T12:0{index}:00'):
                    self.save(signature=f'签名{index}')
            self.assertEqual(2, self.db.profile_capture_count(fid))
            self.assertFalse(CONFIG.save_profile_screenshots)

    def test_confirmed_empty_signature_clears_it_without_inventing_presence(self):
        fid = self.save()
        self.save(signature='', signature_confirmed=True)
        row = self.db.get_friend(fid)
        self.assertEqual('', row['signature'])
        self.assertEqual(0, row['signature_pending'])
        self.assertEqual('unknown', row['current_status'])

    def test_account_is_required_and_same_uid_is_isolated_between_accounts(self):
        other = Database(':memory:')
        self.addCleanup(other.close)
        with self.assertRaises(ValueError):
            other.import_friend_profile('123456789', '好友', 60, '', self.image)
        other.set_account_uid('222222222')
        self.save()
        other.import_friend_profile('123456789', '另一个账号的备注', 40, '', self.image)
        self.assertEqual('测试好友', self.db.find_by_uid('123456789')['nickname'])
        self.assertEqual('另一个账号的备注', other.find_by_uid('123456789')['nickname'])


if __name__ == '__main__':
    unittest.main()

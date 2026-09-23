"""Pending signatures cannot corroborate a rename or distinguish namesakes."""
from tests import test_regressions as _baseline

import unittest

import numpy as np

from app.fast_layout import CardObservation
from app.friend_screen import CardBand
from app.parser import FriendEntry
from app.quick_scan import QuickIdentities


def observation(name='新昵称', signature='可靠签名', pending=False):
    entry = FriendEntry(name, 'online', None, 60, 194, 177, .99,
                        signature=signature, signature_confidence=.99,
                        signature_pending=pending)
    pixels = np.full((35, 160, 3), 220, np.uint8)
    return CardObservation(CardBand(120, 235), entry, pixels, pixels)


def saved(fid=1, name='旧昵称', signature='可靠签名', pending=False):
    return dict(id=fid, uid=str(100000000+fid), nickname=name,
                signature=signature, signature_pending=pending)


class QuickPendingSignatureIdentityTests(unittest.TestCase):
    def resolve(self, rows, items):
        return QuickIdentities(rows, []).resolve(list(enumerate(items)))

    def test_saved_pending_signature_cannot_trigger_rename_uid(self):
        record = self.resolve([saved(pending=True)], [observation()])[0]
        self.assertEqual('new_nickname', record['match_basis'])
        self.assertIsNone(record['rename_candidate'])
        self.assertIsNone(record['id'])

    def test_current_pending_signature_cannot_trigger_rename_uid(self):
        record = self.resolve([saved()], [observation(pending=True)])[0]
        self.assertEqual('new_nickname', record['match_basis'])
        self.assertIsNone(record['rename_candidate'])
        self.assertIsNone(record['id'])

    def test_confirmed_signature_still_proposes_uid_verification(self):
        record = self.resolve([saved()], [observation()])[0]
        self.assertEqual('possible_rename', record['match_basis'])
        self.assertEqual(1, record['rename_candidate']['id'])
        self.assertIsNone(record['id'])

    def test_pending_saved_namesake_signature_cannot_select_identity(self):
        rows = [saved(name='同名', pending=True), saved(2, '同名', '其他签名')]
        record = self.resolve(rows, [observation('同名')])[0]
        self.assertEqual('needs_remark', record['match_basis'])
        self.assertIsNone(record['uid'])

    def test_pending_current_signature_cannot_select_namesake(self):
        rows = [saved(name='同名'), saved(2, '同名', '其他签名')]
        record = self.resolve(rows, [observation('同名', pending=True)])[0]
        self.assertEqual('needs_remark', record['match_basis'])
        self.assertIsNone(record['uid'])

    def test_two_pending_signatures_do_not_distinguish_current_namesakes(self):
        rows = [saved(name='同名'), saved(2, '同名', '其他签名')]
        records = self.resolve(rows, [observation('同名', pending=True),
                                     observation('同名', '其他签名', pending=True)])
        self.assertTrue(all(record['match_basis'] == 'needs_remark' for record in records))
        self.assertTrue(all(record['id'] is None for record in records))
        self.assertEqual(records[0]['identity_review_key'], records[1]['identity_review_key'])

    def test_unique_name_does_not_depend_on_pending_signature(self):
        record = self.resolve([saved(name='同一昵称', pending=True)],
                              [observation('同一昵称', pending=True)])[0]
        self.assertEqual(('nickname', 1), (record['match_basis'], record['id']))

    def test_pending_candidate_is_excluded_without_excluding_confirmed_namesake(self):
        rows = [saved(name='同名', pending=True), saved(2, '同名')]
        record = self.resolve(rows, [observation('同名')])[0]
        self.assertEqual(('nickname_and_signature', 2), (record['match_basis'], record['id']))

    def test_legacy_absent_pending_flag_retains_confirmed_behavior(self):
        row = saved()
        row.pop('signature_pending')
        obs = observation()
        del obs.entry.signature_pending
        record = self.resolve([row], [obs])[0]
        self.assertEqual('possible_rename', record['match_basis'])


if __name__ == '__main__':
    unittest.main()

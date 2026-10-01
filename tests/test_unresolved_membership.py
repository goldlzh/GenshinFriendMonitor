"""Unresolved observations must not turn existing friends into false removals."""
from tests import test_regressions as baseline
from tests import test_presence_monitoring as presence
from tests import test_quick_identity_storage as storage

from dataclasses import replace
import sqlite3
import unittest
from unittest.mock import patch

import numpy as np

from app.database import Database
from app.fast_scan import _png
from app.fast_layout import CardObservation
from app.membership_events import pending_removals
from app.quick_scan import QuickIdentities


class UnresolvedMembershipTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)

    def scan(self, records, seconds=0):
        comparison = {}
        ids = self.db.apply_verified_scan(records, presence.at(seconds), 1., 'synthetic', comparison=comparison)
        return ids, comparison

    def changes(self):
        return [r for r in self.db.get_recent_events(changes_only=True) if r['event_type'] in ('added', 'removed')]

    def test_eight_unreadable_cards_do_not_remove_eight_known_friends_or_readd_them_later(self):
        known = [storage.record(str(100000000+i), name='好友%d' % i, status='offline') for i in range(8)]
        owners, _ = self.scan(known)
        unknown = [storage.record(name='', status='unknown') for _ in range(8)]
        drafts, result = self.scan(unknown, 60)
        self.assertEqual((8, 8), (result['previous_total'], result['current_total']))
        self.assertEqual([], result['removed'])
        self.assertEqual(8, len(result['unconfirmed_removed']))
        self.assertEqual([], self.changes())
        self.assertEqual(8, len(self.db.get_display_friends()))
        self.assertEqual(owners, [r['id'] for r in pending_removals(self.db._conn)])
        for draft, row in zip(unknown, drafts):
            draft['id'] = row
        _, repeated = self.scan(unknown, 120)
        self.assertEqual([], repeated['removed'])
        self.assertEqual(8, len(repeated['unconfirmed_removed']))
        self.assertEqual(8, repeated['previous_total'])
        _, resolved = self.scan(known, 180)
        self.assertEqual(([], []), (resolved['added'], resolved['removed']))
        self.assertEqual([], pending_removals(self.db._conn))
        self.assertEqual([], self.changes())

    def test_candidate_scope_does_not_hide_unrelated_missing_friend(self):
        (uncertain, lost), _ = self.scan([storage.record('123456789', name='待核对'),
                                        storage.record('223456789', name='确实消失')])
        draft = dict(storage.record(name='', status='unknown'), identity_candidate_ids=[uncertain])
        (draft_id,), result = self.scan([draft], 60)
        self.assertEqual([lost], [r['id'] for r in result['removed']])
        self.assertEqual([uncertain], [r['id'] for r in result['unconfirmed_removed']])
        self.assertEqual(['223456789'], [r['uid'] for r in self.changes()])
        _, repeat = self.scan([storage.record(fid=draft_id, name='', status='unknown')], 120)
        self.assertEqual([], repeat['removed'])
        self.assertEqual([uncertain], [r['id'] for r in repeat['unconfirmed_removed']])

    def test_confirmed_absence_after_uncertain_scan_is_recorded_once(self):
        (owner,), _ = self.scan([storage.record('123456789', name='好友')])
        self.scan([storage.record(name='', status='unknown')], 60)
        _, result = self.scan([], 120)
        self.assertEqual(['123456789'], [r['uid'] for r in result['removed']])
        self.assertEqual([owner], [r['friend_id'] for r in self.changes()])
        self.assertEqual(['removed'], [r['event_type'] for r in self.changes()])
        self.assertEqual([], pending_removals(self.db._conn))
        self.scan([], 180)
        self.assertEqual(1, len(self.changes()))

    def test_different_uid_after_uncertainty_proves_replacement_despite_equal_totals(self):
        self.scan([storage.record('123456789', name='原好友')])
        (draft,), _ = self.scan([storage.record(name='', status='unknown')], 60)
        _, result = self.scan([storage.record('223456789', fid=draft, name='新好友')], 120)
        self.assertEqual(['123456789'], [r['uid'] for r in result['removed']])
        self.assertEqual(['223456789'], [r['uid'] for r in result['added']])
        self.assertEqual(['removed', 'added'], [r['event_type'] for r in self.changes()])

    def test_uid_confirmation_of_draft_merges_with_original_without_false_return(self):
        (owner,), _ = self.scan([storage.record('123456789', name='好友')])
        (draft,), _ = self.scan([storage.record(name='', status='unknown')], 60)
        (same,), result = self.scan([storage.record('123456789', fid=draft, name='好友')], 120)
        self.assertEqual(owner, same)
        self.assertEqual(([], []), (result['added'], result['removed']))
        self.assertEqual([], self.changes())
        self.assertEqual([], pending_removals(self.db._conn))

    def test_known_unrelated_disappearance_survives_stationary_anonymous_card(self):
        (pending, lost), _ = self.scan([storage.record(name='', status='unknown'),
                                      storage.record('123456789', name='消失')])
        _, result = self.scan([storage.record(fid=pending, name='', status='unknown')], 60)
        self.assertEqual([lost], [r['id'] for r in result['removed']])
        self.assertFalse(result.get('unconfirmed_removed'))

    def test_deferred_state_rolls_back_with_failed_complete_scan(self):
        self.scan([storage.record('123456789', name='好友')])
        self.db._conn.execute('''CREATE TRIGGER reject_test_scan BEFORE INSERT ON scan_sessions
            BEGIN SELECT RAISE(ABORT,'isolated failure'); END''')
        before = list(self.db._conn.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            self.scan([storage.record(name='', status='unknown')], 60)
        self.assertEqual(before, list(self.db._conn.iterdump()))

    def test_online_scan_does_not_erase_unresolved_full_scan_identity(self):
        self.scan([storage.record('123456789', name='好友')])
        (draft,), _ = self.scan([storage.record(name='', status='unknown')], 60)
        before = pending_removals(self.db._conn)
        self.db.apply_online_scan([storage.record(fid=draft, name='', status='unknown')],
                                  presence.at(120), 1., 'synthetic', identity_complete=False)
        self.assertEqual(before, pending_removals(self.db._conn))


class IdentityCandidateTests(unittest.TestCase):
    def observation(self):
        image = np.full((25, 100, 3), 220, np.uint8)
        image[5:20, 10:65] = 20
        return CardObservation(None, replace(baseline.entry(''), nickname_pending=True), image, image)

    def test_below_binding_threshold_is_only_a_review_candidate_never_uid_binding(self):
        obs = self.observation()
        identity = QuickIdentities([dict(id=1, uid='123456789', nickname='好友')],
                                  [dict(friend_id=1, name_image=_png(obs.name_image))])
        with patch('app.quick_scan.layout.nickname_ink_similarity', return_value=.976):
            record = identity.resolve([(0, obs)])[0]
        self.assertIsNone(record['id'])
        self.assertIsNone(record['uid'])
        self.assertEqual([1], record['identity_candidate_ids'])
        self.assertEqual('pending_nickname_image', record['match_basis'])

    def test_same_name_ambiguity_carries_all_candidates_without_guessing_uid(self):
        obs = self.observation()
        obs.entry = baseline.entry('同名')
        rows = [dict(id=1, uid='123456789', nickname='同名'),
                dict(id=2, uid='223456789', nickname='同名')]
        record = QuickIdentities(rows, []).resolve([(0, obs)])[0]
        self.assertIsNone(record['uid'])
        self.assertEqual([1, 2], record['identity_candidate_ids'])
        self.assertEqual('unknown', record['entry'].status)


if __name__ == '__main__':
    unittest.main()

"""Membership differences survive missing UIDs, equal totals and UID enrollment."""
from tests import test_regressions as baseline
from tests import test_presence_monitoring as presence
from tests import test_quick_identity_storage as storage
from tests import test_quick_scan_workflow as quick

from dataclasses import replace
import sqlite3
import unittest
from unittest.mock import patch

from app.database import Database
from app.fast_scan import ScanIncomplete
from app.quick_scan import QuickScanWorkflow


class RosterMembershipStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)

    def scan(self, records, seconds=0):
        comparison = {}
        ids = self.db.apply_verified_scan(records, presence.at(seconds), 1., 'isolated test',
                                          comparison=comparison)
        self.assertEqual(comparison, self.db.get_roster_comparisons()[0])
        return ids, comparison

    def events(self, kind):
        return [event for event in self.db.export_events() if event['event_type'] == kind]

    def test_equal_total_with_new_uidless_friend_records_both_events_once(self):
        (kept, lost), _ = self.scan([storage.record('123456789', name='留下'),
                                    storage.record('223456789', name='消失')])
        (same, new), diff = self.scan([storage.record('123456789', fid=kept, name='留下'),
                                      storage.record(name='新增')], 60)
        self.assertEqual(kept, same)
        self.assertEqual((2, 2), (diff['previous_total'], diff['current_total']))
        self.assertEqual('local_id', diff['basis'])
        self.assertEqual([dict(id=lost, uid='223456789', nickname='消失')], diff['removed'])
        self.assertEqual([dict(id=new, uid=None, nickname='新增')], diff['added'])
        self.assertEqual(['223456789'], [event['uid'] for event in self.events('removed')])
        self.assertIsNotNone(self.db.get_friend(lost))
        self.assertNotIn(lost, [row['id'] for row in self.db.get_display_friends()])
        _, again = self.scan([storage.record('123456789', fid=kept, name='留下'),
                              storage.record(fid=new, name='新增')], 120)
        self.assertEqual(([], []), (again['added'], again['removed']))
        self.assertEqual(1, len(self.events('removed')))
        self.assertEqual(3, len(self.events('added')))

    def test_first_uidless_scan_is_baseline_and_next_scan_can_report_uidless_removal(self):
        (kept, lost), first = self.scan([storage.record(name='甲'), storage.record(name='乙')])
        self.assertEqual('skipped', first['basis'])
        self.assertEqual([], self.events('removed'))
        (_, new), second = self.scan([storage.record(fid=kept, name='甲'), storage.record(name='丙')], 60)
        self.assertEqual([dict(id=lost, uid=None, nickname='乙')], second['removed'])
        self.assertEqual([dict(id=new, uid=None, nickname='丙')], second['added'])
        self.assertEqual([(None, '乙')], [(e['uid'], e['nickname']) for e in self.events('removed')])

    def test_falling_and_rising_total_still_compare_individuals(self):
        (kept, lost), _ = self.scan([storage.record(name='甲'), storage.record(name='乙')])
        _, fewer = self.scan([storage.record(fid=kept, name='甲')], 60)
        self.assertEqual([lost], [r['id'] for r in fewer['removed']])
        self.assertEqual([], fewer['added'])
        _, more = self.scan([storage.record(fid=kept, name='甲'), storage.record(name='丙'),
                             storage.record(name='丁')], 120)
        self.assertEqual(['丙', '丁'], [r['nickname'] for r in more['added']])
        self.assertEqual([], more['removed'])

    def test_uid_enrollment_and_confirmed_merge_do_not_create_membership_events(self):
        owner = self.db.add_friend('123456789', '历史档案', 60, None, 'offline', presence.T0)
        (pending, other), _ = self.scan([storage.record(name='当前昵称'), storage.record(name='另一个')])
        count = len(self.events('added'))
        ids, diff = self.scan([storage.record('123456789', fid=pending, name='当前昵称'),
                               storage.record(fid=other, name='另一个')], 60)
        self.assertEqual([owner, other], ids)
        self.assertEqual(([], [], []), (diff['added'], diff['removed'], diff['renamed']))
        self.assertEqual([], self.events('removed'))
        self.assertEqual(count, len(self.events('added')))
        self.assertIsNone(self.db.get_friend(pending))

    def test_merging_two_previous_rows_keeps_other_missing_friend_visible_in_diff(self):
        (owner, pending, lost), _ = self.scan([storage.record('123456789', name='旧档案'),
                                              storage.record(name='新档案'), storage.record(name='消失')])
        ids, diff = self.scan([storage.record('123456789', fid=pending, name='新档案')], 60)
        self.assertEqual([owner], ids)
        self.assertEqual((3, 1), (diff['previous_total'], diff['current_total']))
        self.assertEqual([lost], [row['id'] for row in diff['removed']])
        self.assertEqual([], diff['added'])

    def test_removed_friend_returning_counts_added_once_without_duplicate_archive(self):
        (kept, lost), _ = self.scan([storage.record(name='甲'), storage.record(name='乙')])
        self.scan([storage.record(fid=kept, name='甲')], 60)
        _, diff = self.scan([storage.record(fid=kept, name='甲'), storage.record(fid=lost, name='乙')], 120)
        self.assertEqual([lost], [row['id'] for row in diff['added']])
        self.assertEqual(['甲', '乙', '乙'], [row['nickname'] for row in self.events('added')])
        self.assertEqual(2, len(self.db.get_all_friends()))

    def test_online_scan_does_not_remove_or_advance_baseline_or_double_count_added(self):
        (kept, lost), _ = self.scan([storage.record(name='甲'), storage.record(name='乙')])
        previous = self.db.get_complete_roster()
        kept, new = self.db.apply_online_scan([storage.record(fid=kept, name='甲'), storage.record(name='丙')],
                                               presence.at(60), 1., 'online', identity_complete=False)
        self.assertEqual(previous, self.db.get_complete_roster())
        self.assertEqual([], self.events('removed'))
        _, diff = self.scan([storage.record(fid=kept, name='甲'), storage.record(fid=new, name='丙')], 120)
        self.assertEqual([lost], [r['id'] for r in diff['removed']])
        self.assertEqual(3, len(self.events('added')))

    def test_late_commit_failure_rolls_back_events_comparison_and_baseline(self):
        self.scan([storage.record(name='甲')])
        comparison = {}
        self.db._conn.execute("""CREATE TRIGGER reject_test_scan BEFORE INSERT ON scan_sessions
                                  BEGIN SELECT RAISE(ABORT, 'isolated failure'); END""")
        before = list(self.db._conn.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.apply_verified_scan([storage.record(name='乙')], presence.at(60), 1., 'test',
                                         comparison=comparison)
        self.assertEqual({}, comparison)
        self.assertEqual(before, list(self.db._conn.iterdump()))

    def test_unknown_identity_elsewhere_does_not_hide_known_missing_friend(self):
        (pending, lost), _ = self.scan([storage.record(name='', status='unknown', review='unreadable'),
                                       storage.record('123456789', name='消失')])
        _, diff = self.scan([storage.record(fid=pending, name='', status='unknown', review='unreadable'),
                              storage.record(name='新增')], 60)
        self.assertEqual([lost], [r['id'] for r in diff['removed']])
        self.assertEqual(['123456789'], [r['uid'] for r in self.events('removed')])

    def test_reordered_uidless_friends_do_not_count_as_membership_changes(self):
        (first, second), _ = self.scan([storage.record(name='甲'), storage.record(name='乙')])
        _, diff = self.scan([storage.record(fid=second, name='乙'), storage.record(fid=first, name='甲')], 60)
        self.assertEqual(([], []), (diff['added'], diff['removed']))

    def test_returning_friend_seen_online_is_counted_once_before_next_full_scan(self):
        (kept, lost), _ = self.scan([storage.record(name='甲'), storage.record(name='乙')])
        self.scan([storage.record(fid=kept, name='甲')], 60)
        records = [storage.record(fid=kept, name='甲'), storage.record(fid=lost, name='乙')]
        self.db.apply_online_scan(records, presence.at(120), 1., 'online return', identity_complete=False)
        self.assertEqual(3, len(self.events('added')))
        self.scan(records, 180)
        self.assertEqual(3, len(self.events('added')))


class QuickMembershipWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.harness = quick.QuickScanWorkflowTests('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.db = self.harness.db

    def test_equal_count_replacement_is_reported_without_opening_any_profile(self):
        ids = self.harness.seed(6, with_baseline=True)
        def replaced(index, obs):
            if index == 2:
                obs.entry = replace(obs.entry, nickname='新朋友')
        state = self.harness.simulate(6, replaced)
        workflow, result = self.harness.run_workflow(state)
        workflow.read_uid.assert_not_called()
        self.assertEqual(['100000002'], [r['uid'] for r in result.comparison['removed']])
        self.assertEqual(['新朋友'], [r['nickname'] for r in result.comparison['added']])
        self.assertIn('新增1位，消失/可能被删1位', result.message)
        self.assertIsNone(self.db.find_by_nickname('新朋友')['uid'])
        self.assertIsNotNone(self.db.get_friend(ids[2]))
        _, again = self.harness.run_workflow(state, stamp=baseline.T2)
        self.assertEqual(([], []), (again.comparison['added'], again.comparison['removed']))

    def test_verified_rename_is_not_a_replacement(self):
        self.harness.seed(6, signature_index=2, with_baseline=True)
        def renamed(index, obs):
            if index == 2:
                obs.entry = replace(obs.entry, nickname='新昵称', signature='唯一完整签名', signature_confidence=.99)
        state = self.harness.simulate(6, renamed)
        workflow, result = self.harness.run_workflow(state, uid='100000002')
        workflow.read_uid.assert_called_once()
        self.assertEqual(([], []), (result.comparison['added'], result.comparison['removed']))
        self.assertEqual([dict(uid='100000002', before='好友002', after='新昵称')], result.comparison['renamed'])

    def test_unfinished_full_scan_does_not_emit_removals_or_replace_baseline(self):
        self.harness.seed(6, with_baseline=True)
        previous = self.db.get_complete_roster()
        workflow = QuickScanWorkflow(self.harness.scanner)
        with patch.object(workflow, 'home', side_effect=ScanIncomplete('isolated interrupted scan')):
            result = workflow.run()
        self.assertFalse(result.complete)
        self.assertEqual(previous, self.db.get_complete_roster())
        self.assertFalse(any(e['event_type'] == 'removed' for e in self.db.export_events()))


if __name__ == '__main__':
    unittest.main()

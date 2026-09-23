"""Only observed transitions appear, without changing presence accounting."""
from tests import test_regressions as baseline
from tests import test_presence_monitoring as presence
from tests import test_quick_identity_storage as storage

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.database import Database
from app.scanner import Scanner
from app.ui import main_window as ui


class StatusChangeStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.configure_monitoring(0, at=presence.T0)

    def scan(self, records, seconds=0):
        return self.db.apply_verified_scan(records, presence.at(seconds), 1., 'isolated', comparison={})

    def transitions(self):
        return [event['event_type'] for event in reversed(self.db.get_recent_events(changes_only=True))
                if event['event_type'] in ('online', 'offline')]

    def test_initial_online_is_not_login_but_later_transitions_and_ranges_are_recorded(self):
        fid = self.db.add_friend('123456789', '好友', 60, None, 'offline', presence.T0)
        self.db.begin_observation_session()
        self.scan([storage.record('123456789', fid=fid, name='好友')])
        self.assertEqual([], self.transitions())
        self.assertEqual(60, self.db.get_display_friends(now=presence.at(60))[0]['cumulative_online_seconds'])
        self.scan([storage.record('123456789', fid=fid, name='好友', status='offline')], 60)
        self.scan([storage.record('123456789', fid=fid, name='好友')], 120)
        self.db.configure_monitoring(None, at=presence.at(180))
        self.assertEqual(['offline', 'online'], self.transitions())
        self.assertEqual(120, self.db.get_friend(fid)['cumulative_online_seconds'])
        self.assertEqual(['10:00-10:01', '10:02-10:03'], self.db.get_today_online_ranges(fid, now=presence.at(180)))
        self.assertTrue(any(e['event_type'] == 'observed_online' for e in self.db.export_events()))

    def test_stopping_and_starting_again_does_not_infer_login_during_the_gap(self):
        (fid,) = self.scan([storage.record('123456789', name='好友', status='offline')])
        previous = self.db.get_complete_roster()
        self.db.configure_monitoring(None, at=presence.at(60))
        self.db.configure_monitoring(0, at=presence.at(120))
        self.db.begin_observation_session()
        self.assertEqual(previous, self.db.get_complete_roster())
        self.scan([storage.record('123456789', fid=fid, name='好友')], 120)
        self.assertEqual([], self.transitions())
        self.scan([storage.record('123456789', fid=fid, name='好友', status='offline')], 180)
        self.assertEqual(['offline'], self.transitions())
        self.assertEqual(60, self.db.get_friend(fid)['cumulative_online_seconds'])

    def test_session_start_never_erases_existing_online_history(self):
        (fid,) = self.scan([storage.record('123456789', name='好友')])
        self.scan([storage.record('123456789', fid=fid, name='好友', status='offline')], 60)
        before = self.db.get_today_online_ranges(fid, now=presence.at(120))
        events = self.db.export_events()
        self.db.begin_observation_session()
        self.assertEqual(60, self.db.get_friend(fid)['cumulative_online_seconds'])
        self.assertEqual(before, self.db.get_today_online_ranges(fid, now=presence.at(120)))
        self.assertEqual(events, self.db.export_events())

    def test_missing_card_records_removal_not_a_logout(self):
        (fid,) = self.scan([storage.record('123456789', name='好友')])
        self.scan([], 60)
        self.assertEqual([], self.transitions())
        self.assertEqual(['removed', 'added'], [e['event_type'] for e in self.db.get_recent_events(changes_only=True)])
        self.assertEqual(60, self.db.get_friend(fid)['cumulative_online_seconds'])
        self.assertEqual(['10:00-10:01'], self.db.get_today_online_ranges(fid, now=presence.at(120)))

    def test_online_scan_distinguishes_seen_offline_from_not_scanned(self):
        (online, offline, unseen) = self.scan([storage.record('123456789', name='甲'),
            storage.record('223456789', name='乙'), storage.record('323456789', name='丙')])
        previous = self.db.get_complete_roster()
        self.db.apply_online_scan([storage.record('123456789', fid=online, name='甲')],
            presence.at(60), 1., 'isolated online',
            observed_offline=[storage.record('223456789', fid=offline, name='乙', status='offline')])
        changes = [e for e in self.db.get_recent_events(changes_only=True) if e['event_type'] == 'offline']
        self.assertEqual(['223456789'], [e['uid'] for e in changes])
        self.assertEqual(previous, self.db.get_complete_roster())
        self.assertEqual([60, 60], [self.db.get_friend(fid)['cumulative_online_seconds'] for fid in (offline, unseen)])
        self.assertEqual(presence.T0, self.db.get_friend(unseen)['last_scan_at'])
        self.db.apply_online_scan([storage.record('323456789', fid=unseen, name='丙')],
                                  presence.at(120), 1., 'seen again')
        self.assertEqual(['offline'], self.transitions())

    def test_filter_applies_before_limit_and_diagnostic_rows_remain_available(self):
        kinds = ['online', 'offline', 'added', 'removed'] + ['observed_online'] * 501
        self.db._conn.executemany('INSERT INTO status_events(event_type,event_time) VALUES (?,?)',
                                   [(kind, presence.T0) for kind in kinds])
        self.db._conn.commit()
        self.assertEqual(['removed', 'added', 'offline', 'online'],
                         [e['event_type'] for e in self.db.get_recent_events(limit=500, changes_only=True)])
        self.assertEqual('observed_online', self.db.get_recent_events(limit=1)[0]['event_type'])
        self.assertEqual(505, len(self.db.export_events()))

    def test_scanner_start_resets_observation_baseline_only_once_per_run(self):
        fid = self.db.add_friend('123456789', '好友', 60, None, 'offline', presence.T0)
        scanner = Scanner(self.db)
        rounds = []
        def scan():
            state = 'online' if not rounds else 'offline'
            rounds.append(state)
            self.scan([storage.record('123456789', fid=fid, name='好友', status=state)], len(rounds)*60)
            if len(rounds) == 2:
                scanner._stop.set()
            return SimpleNamespace(complete=True)
        with patch.object(baseline.CONFIG, 'scan_frequency_minutes', 0), \
                patch.object(scanner, '_startup_countdown', return_value=True), \
                patch.object(scanner, 'scan_once', side_effect=scan), \
                patch('app.database.now_str', return_value=presence.at(180)):
            scanner._run()
        self.assertEqual(['online', 'offline'], rounds)
        self.assertEqual(['offline'], self.transitions())

    def test_uid_scan_start_is_also_a_new_observation_session(self):
        fid = self.db.add_friend('123456789', '好友', 60, None, 'offline', presence.T0)
        scanner = Scanner(self.db)
        def enroll():
            self.scan([storage.record('123456789', fid=fid, name='好友')], 60)
            return dict(total=1, success=1, complete=True)
        with patch.object(scanner, '_startup_countdown', return_value=True), \
                patch.object(scanner, 'scan_uids_once', side_effect=enroll), \
                patch('app.database.now_str', return_value=presence.at(60)):
            scanner._run_uid_scan()
        self.assertEqual([], self.transitions())


class StatusChangeUiTests(unittest.TestCase):
    def setUp(self):
        self.harness = baseline.Regressions('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.win = self.harness.ui()
        self.win._timer.stop()
        self.db = self.win.db

    def test_visible_records_include_only_four_change_types(self):
        kinds = ['online', 'offline', 'added', 'removed', 'observed_online', 'observed_offline',
                 'scan_default_offline', 'unknown', 'renamed', 'manual_import']
        self.db._conn.executemany('INSERT INTO status_events(event_type,event_time) VALUES (?,?)',
                                   [(kind, presence.T0) for kind in kinds])
        self.db._conn.commit()
        self.win._load_events_table()
        self.assertEqual(['消失/可能被删', '新增', '下线', '上线'],
                         [self.win.events_table.item(row, 3).text() for row in range(self.win.events_table.rowCount())])

    def test_missing_uid_notice_does_not_block_quick_scan_and_clears_after_enrollment(self):
        fid = self.db.add_friend(None, '好友', 60, None, 'offline', presence.T0)
        self.win._load_friends_table()
        self.assertFalse(self.win.uid_notice.isHidden())
        self.assertIn('可能导致身份匹配及统计不准确', self.win.uid_notice.text())
        self.win._manual_rect = (0, 0, 1920, 1080)
        self.win.frequency_edit.setText('')
        with patch.object(self.win.scanner, 'start', return_value=True) as start, \
                patch.object(self.win, '_yield_to_game'), \
                patch.object(ui.QMessageBox, 'warning') as warning, \
                patch.object(ui.QMessageBox, 'question') as question:
            self.win._toggle_scan()
        start.assert_called_once()
        warning.assert_not_called()
        question.assert_not_called()
        self.db.update_friend_manual(fid, '123456789', '好友', 60, None)
        self.win._load_friends_table()
        self.assertTrue(self.win.uid_notice.isHidden())

    def test_history_view_does_not_warn_about_uidless_archives_outside_current_roster(self):
        self.db.add_friend(None, '历史好友', 60, None, 'offline', presence.T0)
        self.db.apply_verified_scan([storage.record('123456789', name='当前好友')], presence.T0, 1., 'test')
        self.win.show_history.setChecked(True)
        self.win._load_friends_table()
        self.assertEqual(2, self.win.friends_table.rowCount())
        self.assertTrue(self.win.uid_notice.isHidden())


class FirstUseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.harness = baseline.WorkflowRegressions('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def test_full_uid_enrollment_works_directly_on_an_empty_database(self):
        specs = [('甲', '111111111', 1), ('乙', '222222222', 2)]
        flow, calls = self.harness.workflow([specs, specs], force=True)
        result = flow.run()
        self.assertTrue(result.complete, result.message)
        self.assertEqual(['quick', 'uid'], calls)
        self.assertEqual(['111111111', '222222222'], [r['uid'] for r in self.harness.db.get_display_friends()])

    def test_full_uid_enrollment_reuses_existing_quick_scan_archives(self):
        specs = [('甲', '111111111', 1), ('乙', '222222222', 2)]
        records = self.harness.flow.records_from_observations(self.harness.items(specs))
        ids = self.harness.db.apply_verified_scan(records, baseline.T0, 1., 'quick baseline')
        flow, calls = self.harness.workflow([specs, specs], force=True)
        result = flow.run()
        self.assertTrue(result.complete, result.message)
        self.assertEqual(['quick', 'uid'], calls)
        self.assertEqual(ids, [r['id'] for r in self.harness.db.get_display_friends()])
        self.assertEqual(['111111111', '222222222'], [r['uid'] for r in self.harness.db.get_display_friends()])
        self.assertEqual(([], []), (result.comparison['added'], result.comparison['removed']))


if __name__ == '__main__':
    unittest.main()

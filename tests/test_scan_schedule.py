"""Serial minute-based scheduling and expiry independent of blocked game work."""
from tests import test_regressions as baseline

from contextlib import ExitStack
from datetime import datetime, timedelta
import threading
import unittest
from unittest.mock import patch

from app.config import CONFIG
from app.database import Database, now_str
from app.scanner import Scanner, ScanResult


class ScanScheduleTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.scan = Scanner(self.db)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (('scan_frequency_minutes', None), ('scan_once_only', False),
                            ('online_only', False)):
            self.stack.enter_context(patch.object(CONFIG, name, value))
        self.stack.enter_context(patch.object(self.scan, '_startup_countdown', return_value=True))

    def result(self, complete=True):
        return ScanResult(now_str(), 0, 0, 0, complete=complete)

    def test_blank_is_one_snapshot_with_no_wait_or_time_accrual(self):
        def once():
            self.assertIsNone(self.db.monitoring_interval_seconds)
            self.db.add_friend('123456789', '好友', 60, None, 'online', now_str())
            return self.result()
        with patch.object(self.scan, 'scan_once', side_effect=once) as run, \
             patch.object(self.scan, '_wait_next_round') as wait:
            self.scan._run()
        run.assert_called_once()
        wait.assert_not_called()
        row = self.db.get_all_friends()[0]
        self.assertEqual(row['current_status'], 'online')
        self.assertIsNone(row['first_online_at'])

    def test_zero_repeats_serially_without_sleep_and_stops_cleanly(self):
        calls = []
        def once():
            calls.append(self.db.monitoring_interval_seconds)
            if len(calls) == 3:
                self.scan.stop()
            return self.result()
        CONFIG.scan_frequency_minutes = 0
        with patch.object(self.scan, 'scan_once', side_effect=once), \
             patch.object(self.scan, '_wait') as wait:
            self.scan._run()
        self.assertEqual(calls, [0, 0, 0])
        wait.assert_not_called()
        self.assertIsNone(self.db.monitoring_interval_seconds)

    def test_fractional_frequency_uses_start_to_start_and_skips_overrun_backlog(self):
        CONFIG.scan_frequency_minutes = .1
        clock, starts, sleeps = [0.], [], []
        def once():
            starts.append(clock[0])
            self.assertEqual(self.db.monitoring_interval_seconds, 6)
            clock[0] += 2 if len(starts) == 1 else 9
            if len(starts) == 3:
                self.scan.stop()
            return self.result()
        def wait(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
            return True
        with patch('app.scanner.time.monotonic', side_effect=lambda: clock[0]), \
             patch.object(self.scan, '_wait', side_effect=wait), \
             patch.object(self.scan, 'scan_once', side_effect=once):
            self.scan._run()
        self.assertEqual(starts, [0, 6, 15])
        self.assertEqual(sleeps, [4])

    def test_legacy_one_shot_flag_cannot_override_explicit_frequency(self):
        CONFIG.scan_frequency_minutes, CONFIG.scan_once_only = .5, True
        intervals = []
        def once():
            intervals.append(self.db.monitoring_interval_seconds)
            if len(intervals) == 2:
                self.scan.stop()
            return self.result()
        with patch.object(self.scan, 'scan_once', side_effect=once) as run, \
             patch.object(self.scan, '_wait_next_round', side_effect=lambda _: not self.scan._stop.is_set()):
            self.scan._run()
        self.assertEqual(2, run.call_count)
        self.assertEqual([30, 30], intervals)
        self.assertIsNone(self.db.monitoring_interval_seconds)

    def test_failed_round_expires_presence_before_callback(self):
        CONFIG.scan_frequency_minutes = 0
        def once():
            self.db.add_friend('123456789', '好友', 60, None, 'online', now_str())
            return self.result(False)
        def received(result):
            self.assertFalse(result.complete)
            self.assertEqual(self.db.get_all_friends()[0]['current_status'], 'offline')
            self.scan.stop()
        self.scan.on_result = received
        with patch.object(self.scan, 'scan_once', side_effect=once):
            self.scan._run()

    def test_exception_marks_failed_and_releases_monitoring(self):
        CONFIG.scan_frequency_minutes = 0
        self.scan.on_result = lambda result: self.scan.stop()
        with patch.object(self.scan, 'scan_once', side_effect=RuntimeError('failed')), \
             patch.object(self.db, 'mark_scan_failed', wraps=self.db.mark_scan_failed) as failed:
            self.scan._run()
        failed.assert_called_once()
        self.assertIsNone(self.db.monitoring_interval_seconds)

    def test_watchdog_expires_while_scan_is_still_blocked(self):
        CONFIG.scan_frequency_minutes = .001
        entered, released, expired = threading.Event(), threading.Event(), threading.Event()
        original = self.db.expire_presence
        def expire(*args, **kwargs):
            count = original(*args, **kwargs)
            if count:
                expired.set()
            return count
        def once():
            # Old observation models a stalled scan without sleeping for a period.
            at = (datetime.now()-timedelta(seconds=2)).isoformat()
            self.db.add_friend('123456789', '好友', 60, None, 'online', at)
            entered.set()
            released.wait(3)
            return self.result(False)
        with patch.object(self.db, 'expire_presence', side_effect=expire), \
             patch.object(self.scan, 'scan_once', side_effect=once):
            self.assertTrue(self.scan.start())
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(expired.wait(2))
                self.assertTrue(self.scan.is_running)
                self.assertEqual(self.db.get_all_friends()[0]['current_status'], 'offline')
            finally:
                self.scan.stop()
                released.set()
                self.scan._thread.join(3)
            self.assertFalse(self.scan.is_running)

    def test_cancelled_countdown_disables_monitoring(self):
        CONFIG.scan_frequency_minutes = .5
        with patch.object(self.scan, '_startup_countdown', return_value=False), \
             patch.object(self.scan, 'scan_once') as run:
            self.scan._run()
        run.assert_not_called()
        self.assertIsNone(self.db.monitoring_interval_seconds)

    def test_invalid_frequency_does_not_create_worker(self):
        CONFIG.scan_frequency_minutes = -1
        with self.assertRaises(ValueError):
            self.scan.start()
        self.assertIsNone(self.scan._thread)

    def test_stop_freezes_continuous_presence_without_waiting_for_ocr(self):
        self.db.configure_monitoring(0)
        at = now_str()
        fid = self.db.add_friend('123456789', '好友', 60, None, 'online', at)
        self.scan.stop()
        self.assertIsNone(self.db.monitoring_interval_seconds)
        row = self.db.get_friend(fid)
        self.assertEqual(row['current_status'], 'offline')
        later = (datetime.fromisoformat(at)+timedelta(hours=1)).isoformat()
        projected = self.db.get_display_friends(now=later)[0]
        self.assertEqual(projected['cumulative_online_seconds'], row['cumulative_online_seconds'])

    def test_stop_is_idempotent_after_old_account_database_is_closed(self):
        self.db.configure_monitoring(0)
        self.db.close()
        self.scan.stop()
        self.scan.stop()
        with self.assertRaises(RuntimeError):
            self.db.configure_monitoring(0)


if __name__ == '__main__':
    unittest.main()

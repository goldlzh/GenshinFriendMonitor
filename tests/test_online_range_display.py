"""Online range labels coalesce scan fragments without inventing accounted time."""
from tests import test_regressions as baseline
from tests.test_presence_monitoring import T0, at

from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.database import Database, _display_online_intervals
from datetime import datetime


class OnlineRangeDisplayTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.db.configure_monitoring(120, at=T0)
        self.fid = self.db.add_friend('123456789', '旅行者', 60, None, 'online', T0)

    def online(self, seconds):
        self.db.update_status(self.fid, 'online', None, None, at(seconds))

    def ranges(self, seconds):
        return self.db.get_today_online_ranges(self.fid, now=at(seconds))

    def test_late_periodic_scans_show_one_start_and_end_without_charging_gaps(self):
        for seconds in (120.3, 240.5, 360.7):
            self.db.expire_presence(now=at(seconds))
            self.online(seconds)
        before = list(self.db._conn.iterdump())
        self.assertEqual(['10:00-进行中'], self.ranges(420))
        self.assertEqual(before, list(self.db._conn.iterdump()))
        self.db.configure_monitoring(None, at=at(480))
        self.assertEqual(['10:00-10:08'], self.ranges(600))
        self.assertAlmostEqual(479.3, self.db.get_friend(self.fid)['cumulative_online_seconds'])
        self.assertEqual(4, self.db._conn.execute('SELECT COUNT(*) FROM online_sessions').fetchone()[0])

    def test_multiple_continuous_groups_keep_separate_start_and_end(self):
        self.online(120.3)
        self.online(400)
        self.online(520.3)
        self.db.configure_monitoring(None, at=at(580))
        self.assertEqual(['10:00-10:04', '10:06-10:09'], self.ranges(600))

    def test_real_offline_within_same_minute_stays_separate(self):
        self.db.update_status(self.fid, 'offline', None, None, at(120))
        self.online(120.5)
        self.assertEqual(['10:00-10:02', '10:02-进行中'], self.ranges(180))

    def test_real_offline_after_expiry_still_breaks_the_range(self):
        self.db.expire_presence(now=at(120.1))
        self.db.update_status(self.fid, 'offline', None, None, at(120.2))
        self.online(120.5)
        self.assertEqual(['10:00-10:02', '10:02-进行中'], self.ranges(180))

    def test_stop_after_expiry_still_breaks_the_range(self):
        self.db.expire_presence(now=at(120.1))
        self.db.configure_monitoring(None, at=at(120.2))
        self.db.configure_monitoring(120, at=at(120.3))
        self.online(120.5)
        self.assertEqual(['10:00-10:02', '10:02-进行中'], self.ranges(180))

    def test_failed_scan_after_expiry_still_breaks_the_range(self):
        self.db.expire_presence(now=at(120.1))
        self.db.mark_scan_failed(at=at(120.2))
        self.online(120.5)
        self.assertEqual(['10:00-10:02', '10:02-进行中'], self.ranges(180))

    def test_unknown_observation_is_not_bridged(self):
        self.db.update_status(self.fid, 'unknown', None, None, at(120.1))
        self.online(120.5)
        self.assertEqual(['10:00-10:02', '10:02-进行中'], self.ranges(180))

    def test_uidless_friend_boundary_does_not_split_another_friend(self):
        other = self.db.add_friend(None, '旅行者', 60, None, 'online', T0)
        self.db.expire_presence(now=at(120.1))
        self.db.update_status(other, 'offline', None, None, at(120.2))
        self.db.update_status(other, 'online', None, None, at(120.5))
        self.online(120.5)
        self.assertEqual(['10:00-进行中'], self.ranges(180))
        self.assertEqual(['10:00-10:02', '10:02-进行中'],
                         self.db.get_today_online_ranges(other, now=at(180)))

    def test_legacy_real_offline_events_remain_boundaries(self):
        self.db.update_status(self.fid, 'offline', None, None, at(120))
        self.online(120.5)
        self.db._conn.execute('DELETE FROM presence_range_breaks')
        last_id = self.db._conn.execute('SELECT MAX(id) FROM status_events').fetchone()[0]
        self.db._set_metadata_locked('presence_legacy_event_id', str(last_id))
        self.db._conn.commit()
        self.assertEqual(['10:00-10:02', '10:02-进行中'], self.ranges(180))

    def test_overlapping_alias_histories_cannot_erase_a_known_break(self):
        clock = lambda seconds: datetime.fromisoformat(at(seconds))
        self.assertEqual([(clock(0), clock(120)), (clock(120), clock(180))],
                         _display_online_intervals([(clock(0), clock(120)), (clock(60), clock(180))],
                                                   [clock(120)]))

    def test_deleting_friends_cleans_their_display_boundaries(self):
        self.db.update_status(self.fid, 'offline', None, None, at(60))
        self.db.delete_friend(self.fid)
        self.assertEqual(0, self.db._conn.execute(
            'SELECT COUNT(*) FROM presence_range_breaks WHERE friend_id=?', (self.fid,)).fetchone()[0])
        self.db.delete_all_friends()
        self.assertEqual(0, self.db._conn.execute('SELECT COUNT(*) FROM presence_range_breaks').fetchone()[0])

    def test_midnight_clipping_still_keeps_history_without_ongoing_label(self):
        start = '2026-09-20T23:59:00'
        self.db.configure_monitoring(120, at=start)
        fid = self.db.add_friend('234567890', '跨日', 60, None, 'online', start)
        self.db.update_status(fid, 'online', None, None, '2026-09-21T00:01:00.500000')
        self.assertEqual(['23:59-24:00'],
                         self.db.get_today_online_ranges(fid, '2026-09-20', now=at(0)))
        self.assertEqual(['00:00-进行中'], self.db.get_today_online_ranges(
            fid, '2026-09-21', now='2026-09-21T00:02:00'))

    def test_upgrade_retains_legacy_ranges_and_creates_backup(self):
        with tempfile.TemporaryDirectory(dir=baseline.WORK) as directory:
            path = Path(directory)/'legacy.db'
            old = Database(str(path))
            old.configure_monitoring(120, at=T0)
            fid = old.add_friend('123456789', '旅行者', 60, None, 'online', T0)
            old.update_status(fid, 'online', None, None, at(120.3))
            old.configure_monitoring(None, at=at(240))
            old._conn.execute('DROP TABLE presence_range_breaks')
            old._conn.execute('PRAGMA user_version=7')
            old._conn.commit()
            old.close()
            upgraded = Database(str(path))
            try:
                self.assertEqual(['10:00-10:04'], upgraded.get_today_online_ranges(fid, now=at(600)))
                self.assertEqual(1, len(list(Path(directory).glob('legacy.db.backup-*'))))
                self.assertAlmostEqual(239.7, upgraded.get_friend(fid)['cumulative_online_seconds'])
            finally:
                upgraded.close()


if __name__ == '__main__':
    unittest.main()

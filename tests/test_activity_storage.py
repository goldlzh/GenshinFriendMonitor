"""Verified activity timing and observed/default status changes stay distinct."""
from tests import test_regressions as baseline
from tests import test_presence_monitoring as presence

from contextlib import closing
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest

from app.database import Database, _SCHEMA


def record(activity='奇域甲', *, uid='123456789', fid=None, status='online',
           mode='qianxing', verified=True, name='旅行者'):
    item = presence.record(uid, status=status, name=name)
    item['id'] = fid
    item['entry'].interface_mode = mode
    item['entry'].qianxing_activity = activity
    item['entry'].qianxing_activity_verified = verified
    return item


class ActivityStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)

    def scan(self, activity='奇域甲', seconds=0, **kwargs):
        item = record(activity, **kwargs)
        return self.db.apply_verified_scan([item], presence.at(seconds), 0., 'isolated activity')[0]

    def stats(self, fid, seconds=0, **kwargs):
        return self.db.get_activity_stats(fid, now=presence.at(seconds), **kwargs)

    def test_single_scan_shows_verified_activity_without_accruing_time(self):
        fid = self.scan()
        self.assertEqual('奇域甲', self.stats(fid, 3600)['current'])
        self.assertEqual(0, self.stats(fid, 3600)['total_seconds'])
        self.scan(seconds=3600)
        row = self.db.get_all_friends(now=presence.at(7200))[0]
        self.assertEqual(('奇域甲', 0, ''),
                         (row['qianxing_activity'], row['qianxing_activity_seconds'], row['qianxing_activity_ranges']))
        self.assertIn('奇域甲', row['qianxing_activity_summary'])

    def test_only_verified_qianxing_online_activity_can_start_timing(self):
        self.db.configure_monitoring(0, at=presence.T0)
        for options in ({'mode': 'normal'}, {'verified': False}, {'verified': 1},
                        {'status': 'offline'}, {'status': 'unknown'}, {'activity': ''}):
            with self.subTest(options=options):
                fid = self.scan(**options)
                self.assertEqual(0, self.stats(fid, 60)['total_seconds'])
                self.assertIsNone(self.stats(fid, 60)['current'])

    def test_activity_switches_are_separate_and_editor_activity_uses_same_accounting(self):
        self.db.configure_monitoring(0, at=presence.T0)
        fid = self.scan()
        self.scan(seconds=60)
        self.scan('编辑内容中', seconds=90)
        self.scan(None, seconds=150, mode='normal')
        stats = self.stats(fid, 900)
        self.assertEqual(150, stats['total_seconds'])
        self.assertEqual(90, stats['by_activity']['奇域甲']['seconds'])
        self.assertEqual(60, stats['by_activity']['编辑内容中']['seconds'])
        self.assertEqual(['10:00-10:01'], stats['by_activity']['奇域甲']['today_ranges'])
        self.assertIsNone(stats['current'])

    def test_green_unknown_offline_and_normal_observations_end_current_activity(self):
        self.db.configure_monitoring(0, at=presence.T0)
        for index, options in enumerate(({'verified': False}, {'status': 'unknown'},
                                         {'status': 'offline'}, {'mode': 'normal'})):
            uid = '%09d' % (123456780+index)
            fid = self.scan(uid=uid, seconds=index*120)
            self.scan(uid=uid, seconds=index*120+60, **options)
            self.assertEqual(60, self.stats(fid, 900)['total_seconds'])
            self.assertIsNone(self.stats(fid, 900)['current'])

    def test_expiry_and_late_observation_do_not_bridge_unobserved_activity_gap(self):
        self.db.configure_monitoring(30, at=presence.T0)
        fid = self.scan()
        projected = self.stats(fid, 120)
        self.assertEqual(30, projected['total_seconds'])
        self.assertIsNone(projected['current'])
        self.assertEqual('奇域甲', self.db.get_friend(fid)['qianxing_activity'])
        self.db.expire_presence(now=presence.at(120))
        self.scan(seconds=180)
        self.db.configure_monitoring(None, at=presence.at(195))
        self.assertEqual(45, self.stats(fid, 3600)['total_seconds'])
        self.assertEqual(['10:00-10:00', '10:03-10:03'],
                         self.stats(fid, 3600)['by_activity']['奇域甲']['today_ranges'])

    def test_failure_stop_and_fractional_frequency_settle_once(self):
        self.db.configure_monitoring(.25, at=presence.T0)
        fid = self.scan()
        self.scan(seconds=.5)
        self.db.mark_scan_failed(at=presence.at(1))
        self.db.mark_scan_failed(at=presence.at(2))
        self.db.configure_monitoring(None, at=presence.at(3))
        self.assertEqual(.5, self.stats(fid, 3600)['total_seconds'])
        self.assertEqual(2, len(list(self.db._conn.execute('SELECT * FROM qianxing_activity_sessions'))))

    def test_cross_midnight_ranges_and_current_projection_are_read_only(self):
        start, end = '2026-09-21T23:59:00', '2026-09-22T00:01:00'
        self.db.configure_monitoring(0, at=start)
        fid = self.db.apply_verified_scan([record()], start, 0., 'isolated')[0]
        before = list(self.db._conn.iterdump())
        current = self.db.get_activity_stats(fid, now=end)
        self.assertEqual(120, current['total_seconds'])
        self.assertEqual(['00:00-进行中'], current['by_activity']['奇域甲']['today_ranges'])
        self.assertEqual(before, list(self.db._conn.iterdump()))
        self.db.apply_verified_scan([record(status='offline')], end, 0., 'isolated')
        yesterday = self.db.get_activity_stats(fid, now=end, date='2026-09-21')
        today = self.db.get_activity_stats(fid, now=end)
        self.assertEqual(['23:59-24:00'], yesterday['by_activity']['奇域甲']['today_ranges'])
        self.assertEqual(['00:00-00:01'], today['by_activity']['奇域甲']['today_ranges'])

    def test_uid_merge_preserves_named_history_without_merging_unrelated_namesake(self):
        self.db.configure_monitoring(0, at=presence.T0)
        owner = self.scan(name='旧名')
        self.scan(seconds=60, status='offline', name='旧名')
        pending = self.scan('奇域乙', uid=None, seconds=120, name='新名')
        unrelated = self.db.add_friend(None, '新名', 60, None, 'offline', presence.at(120))
        self.scan('奇域乙', uid=None, fid=pending, seconds=180, name='新名')
        resolved = self.scan('奇域乙', fid=pending, seconds=240, name='新名')
        self.assertEqual(owner, resolved)
        self.assertIsNone(self.db.get_friend(pending))
        self.assertIsNone(self.db.get_friend(unrelated)['uid'])
        stats = self.stats(owner, 240)
        self.assertEqual((180, 60, 120), (stats['total_seconds'], stats['by_activity']['奇域甲']['seconds'],
                                         stats['by_activity']['奇域乙']['seconds']))

    def test_merge_unions_overlapping_named_activity_and_failed_commit_rolls_back(self):
        self.db.configure_monitoring(0, at=presence.T0)
        first = self.scan()
        second = self.db.add_friend(None, '别名', 60, None, 'online', presence.T0,
                                    qianxing_activity='奇域甲')
        self.db.update_status(second, 'online', None, None, presence.at(60), qianxing_activity='奇域甲')
        before = list(self.db._conn.iterdump())
        bad = record('其他', uid='223456789')
        bad['name_image'] = None
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.apply_verified_scan([record(fid=second), bad], presence.at(120), 0., 'isolated')
        self.assertEqual(before, list(self.db._conn.iterdump()))
        self.scan(fid=second, seconds=120)
        self.assertEqual(120, self.stats(first, 120)['total_seconds'])

    def test_observed_transitions_and_scan_default_offline_have_different_events(self):
        self.db.configure_monitoring(0, at=presence.T0)
        fid = self.scan()
        self.db.mark_scan_failed(at=presence.at(60))
        self.scan(seconds=120)
        self.scan(seconds=180, status='offline')
        self.scan(seconds=240)
        self.db.configure_monitoring(None, at=presence.at(300))
        events = [r[0] for r in self.db._conn.execute('SELECT event_type FROM status_events ORDER BY id')]
        self.assertEqual(['observed_friend', 'observed_online', 'scan_default_offline', 'observed_online',
                          'offline', 'online', 'scan_default_offline'], events)
        self.assertEqual(180, self.stats(fid, 600)['total_seconds'])

    def test_real_offline_after_timeout_still_records_confirmed_offline_transition(self):
        self.db.configure_monitoring(30, at=presence.T0)
        self.scan()
        self.db.expire_presence(now=presence.at(60))
        self.scan(seconds=120, status='offline')
        self.assertEqual(['scan_default_offline', 'offline'], [r[0] for r in self.db._conn.execute(
            "SELECT event_type FROM status_events WHERE event_type LIKE '%offline' ORDER BY id")])


class ActivityMigrationTests(unittest.TestCase):
    def test_upgrade_backups_old_database_and_restart_does_not_charge_unattended_gap(self):
        with tempfile.TemporaryDirectory(dir=baseline.WORK) as folder:
            path = Path(folder)/'old.db'
            schema = re.sub(r'CREATE TABLE IF NOT EXISTS qianxing_activity_sessions \(.*?;\s*'
                            r'CREATE INDEX IF NOT EXISTS idx_qianxing_activity_friend.*?;', '',
                            _SCHEMA, flags=re.S)
            schema = '\n'.join(line for line in schema.splitlines()
                               if not any(key in line for key in ('last_observed_status', 'qianxing_activity')))
            with closing(sqlite3.connect(path)) as raw:
                raw.executescript(schema)
                raw.execute("INSERT INTO friends(uid,nickname,current_status) VALUES ('123456789','旧昵称','offline')")
                raw.commit()
            db = Database(str(path))
            try:
                self.assertEqual(1, len(list(Path(folder).glob('old.db.backup-*'))))
                db.configure_monitoring(0, at=presence.T0)
                fid = db.apply_verified_scan([record()], presence.T0, 0., 'isolated')[0]
                db.apply_verified_scan([record()], presence.at(60), 0., 'isolated')
                db._conn.close()  # Simulate crash: deliberately skip Database.close settlement.
                db._conn = None
                recovered = Database(str(path))
                try:
                    self.assertEqual(60, recovered.get_activity_stats(fid, now=presence.at(3600))['total_seconds'])
                    self.assertIsNone(recovered.get_friend(fid)['qianxing_activity'])
                    self.assertEqual('scan_default_offline', recovered.get_recent_events()[0]['event_type'])
                finally:
                    recovered.close()
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()

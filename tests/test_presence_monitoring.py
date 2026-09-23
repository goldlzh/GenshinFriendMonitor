"""Opt-in presence accounting, expiry, partial online rosters, and image migration."""
from tests import test_regressions as _baseline

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

from app.database import Database, now_str, _time_text


T0 = '2026-09-21T10:00:00'


def at(seconds):
    return (datetime.fromisoformat(T0)+timedelta(seconds=seconds)).isoformat()


def record(uid='123456789', *, status='online', name='旅行者', home=None):
    entry = SimpleNamespace(nickname=name, nickname_pending=False, level=60,
                            qianxing_level=None, tags=None, signature='签名',
                            interface_mode='normal', status=status, location=None, last_seen=None)
    result = dict(uid=uid, entry=entry, name_image=b'name', avatar_image=b'avatar')
    if home is not None:
        result['profile_image'] = home
    return result


class PresenceMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)

    def friend(self, time=T0, uid='123456789'):
        return self.db.add_friend(uid, '旅行者', 60, None, 'online', time)

    def sessions(self):
        return [dict(r) for r in self.db._conn.execute('SELECT * FROM online_sessions ORDER BY id')]

    def test_single_scans_record_status_without_any_time_accounting(self):
        fid = self.friend()
        self.db.update_status(fid, 'online', None, None, at(120))
        projected = self.db.get_display_friends(now=at(3600))[0]
        self.assertEqual(projected['current_status'], 'online')
        self.assertFalse(projected['presence_monitoring'])
        self.assertEqual((projected['total_online_seconds'], projected['cumulative_online_seconds']), (0, 0))
        self.assertIsNone(projected['first_online_at'])
        self.db.update_status(fid, 'offline', None, None, at(4000))
        self.assertEqual(self.sessions(), [])
        self.assertEqual(self.db.get_today_online_ranges(fid, now=at(5000)), [])

    def test_positive_interval_projection_and_expiry_share_exact_cutoff(self):
        self.db.configure_monitoring(120, at=T0)
        fid = self.friend()
        current = self.db.get_display_friends(now=at(60))[0]
        self.assertEqual((current['current_status'], current['total_online_seconds']), ('online', 60))
        self.assertEqual(current['presence_expires_at'], at(120))
        expired = self.db.get_display_friends(now=at(600))[0]
        self.assertEqual((expired['current_status'], expired['cumulative_online_seconds']), ('offline', 120))
        self.assertEqual(expired['total_online_seconds'], 0)
        self.assertEqual(self.db.get_friend(fid)['current_status'], 'online')  # Projection is read-only.
        self.assertEqual(self.db.get_today_online_ranges(fid, now=at(600)), ['10:00-10:02'])
        self.assertEqual(self.db.expire_presence(now=at(600)), 1)
        self.assertEqual(self.db.expire_presence(now=at(900)), 0)
        self.assertEqual(self.db.get_friend(fid)['cumulative_online_seconds'], 120)
        self.assertEqual(self.db.get_friend(fid)['last_scan_at'], T0)
        self.assertEqual(self.sessions()[0]['end_time'], at(120))

    def test_timely_success_renews_deadline_and_delayed_success_does_not_bridge_gap(self):
        self.db.configure_monitoring(120, at=T0)
        fid = self.friend()
        self.db.update_status(fid, 'online', None, None, at(60))
        self.assertEqual(self.db.get_display_friends(now=at(150))[0]['total_online_seconds'], 150)
        self.db.update_status(fid, 'online', None, None, at(300))
        row = self.db.get_friend(fid)
        self.assertEqual((row['first_online_at'], row['cumulative_online_seconds']), (at(300), 180))
        self.assertEqual(self.sessions()[0]['end_time'], at(180))
        self.db.configure_monitoring(None, at=at(360))
        self.assertEqual(self.db.get_friend(fid)['cumulative_online_seconds'], 240)
        self.assertEqual([r['duration_seconds'] for r in self.sessions()], [180, 60])

    def test_continuous_success_failure_and_stop_do_not_charge_unobserved_gap(self):
        self.db.configure_monitoring(0, at=T0)
        fid = self.friend()
        self.db.update_status(fid, 'online', None, None, at(60))
        self.assertEqual(self.db.get_all_friends(now=at(120))[0]['cumulative_online_seconds'], 120)
        self.assertEqual(self.db.mark_scan_failed(at=at(180)), 1)
        self.assertEqual(self.db.mark_scan_failed(at=at(240)), 0)
        self.db.update_status(fid, 'online', None, None, at(300))
        self.db.configure_monitoring(None, at=at(360))
        row = self.db.get_display_friends(now=at(36000))[0]
        self.assertEqual((row['current_status'], row['cumulative_online_seconds']), ('offline', 240))
        self.assertEqual([r['duration_seconds'] for r in self.sessions()], [180, 60])

    def test_failed_periodic_scan_cannot_extend_past_deadline(self):
        self.db.configure_monitoring(60, at=T0)
        fid = self.friend()
        self.db.mark_scan_failed(at=at(600))
        row = self.db.get_friend(fid)
        self.assertEqual(row['cumulative_online_seconds'], 60)
        self.assertEqual(row['status_updated_at'], at(60))

    def test_enabling_monitoring_does_not_backfill_single_snapshot_time(self):
        fid = self.friend()
        self.db.configure_monitoring(0, at=at(300))
        self.db.update_status(fid, 'online', None, None, at(360))
        self.db.configure_monitoring(None, at=at(420))
        self.assertEqual(self.db.get_friend(fid)['cumulative_online_seconds'], 60)
        self.assertEqual(self.sessions()[0]['start_time'], at(360))

    def test_disable_retains_history_and_future_single_scans_add_nothing(self):
        self.db.configure_monitoring(0, at=T0)
        fid = self.friend()
        self.db.configure_monitoring(None, at=at(120))
        self.db.update_status(fid, 'online', None, None, at(300))
        self.db.update_status(fid, 'offline', None, None, at(900))
        self.assertEqual(self.db.get_friend(fid)['cumulative_online_seconds'], 120)
        events_before = len(self.db.export_events())
        self.db.reset_runtime_state()
        self.assertEqual(len(self.db.export_events()), events_before)
        self.assertEqual(self.db.get_today_online_ranges(fid, now=at(3600)), ['10:00-10:02'])

    def test_midnight_expiry_is_clipped_on_both_dates(self):
        start = '2026-09-20T23:59:00'
        self.db.configure_monitoring(120, at=start)
        fid = self.friend(start)
        self.db.expire_presence(now='2026-09-21T03:00:00')
        self.assertEqual(self.db.get_today_online_ranges(fid, '2026-09-20'), ['23:59-24:00'])
        self.assertEqual(self.db.get_today_online_ranges(fid, '2026-09-21'), ['00:00-00:01'])

    def test_invalid_intervals_do_not_change_configuration(self):
        for bad in (-1, float('nan'), float('inf'), True, '60'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.db.configure_monitoring(bad)
        self.assertIsNone(self.db.monitoring_interval_seconds)

    def test_subsecond_interval_expires_using_actual_default_clock(self):
        self.db.configure_monitoring(.06, at=T0)
        fid = self.friend()
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 21, 10, 0, 0, 200000)
        with patch('app.database.datetime', Clock):
            self.assertEqual(self.db.get_display_friends()[0]['current_status'], 'offline')
            self.assertEqual(self.db.expire_presence(), 1)
        self.assertEqual(self.db.get_friend(fid)['cumulative_online_seconds'], .06)
        self.assertEqual(self.sessions()[0]['end_time'], at(.06))

    def test_frequent_subsecond_observations_accumulate_before_display_rounding(self):
        self.db.configure_monitoring(0, at=T0)
        fid = self.friend()
        for index in range(1, 6):
            self.db.update_status(fid, 'online', None, None, at(index*.2))
        row = self.db.get_friend(fid)
        self.assertEqual((row['total_online_seconds'], row['cumulative_online_seconds']), (1., 1.))
        projected = self.db.get_display_friends(now=at(1.25))[0]
        self.assertEqual(projected['cumulative_online_seconds'], 1.25)
        self.db.mark_scan_failed(at=at(1.5))
        self.assertEqual(self.db.get_friend(fid)['cumulative_online_seconds'], 1.5)
        self.assertEqual(self.sessions()[0]['duration_seconds'], 1.5)

    def test_time_helpers_preserve_microseconds_and_fixed_second_samples(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 9, 21, 10, 0, 0, 123456)
        with patch('datetime.datetime', Clock), patch('app.database.datetime', Clock):
            self.assertEqual(now_str(), '2026-09-21T10:00:00.123456')
            self.assertEqual(_time_text(), now_str())
            self.assertEqual(_time_text(Clock.now()), now_str())
        self.assertEqual(_time_text(T0), T0)

    def test_crash_reopen_closes_at_last_success_and_preserves_history(self):
        with tempfile.TemporaryDirectory(dir=_baseline.WORK) as directory:
            path = str(Path(directory)/'presence.db')
            db = Database(path)
            db.configure_monitoring(0, at=T0)
            fid = db.add_friend('123456789', '旅行者', 60, None, 'online', T0)
            db.update_status(fid, 'online', None, None, at(60))
            db._conn.close()  # Simulate process death without close()/stop().
            db._conn = None
            reopened = Database(path)
            try:
                self.assertIsNone(reopened.monitoring_interval_seconds)
                row = reopened.get_friend(fid)
                self.assertEqual((row['current_status'], row['cumulative_online_seconds']), ('offline', 60))
                saved = reopened._conn.execute('SELECT end_time,duration_seconds FROM online_sessions').fetchone()
                self.assertEqual(tuple(saved), (at(60), 60))
            finally:
                reopened.close()

    def test_close_settles_monitoring_at_close_time(self):
        with tempfile.TemporaryDirectory(dir=_baseline.WORK) as directory:
            path = str(Path(directory)/'presence.db')
            db = Database(path)
            db.configure_monitoring(0, at=T0)
            fid = db.add_friend('123456789', '旅行者', 60, None, 'online', T0)
            with patch('app.database._time_text', return_value=at(90)):
                db.close()
            reopened = Database(path)
            try:
                self.assertEqual(reopened.get_friend(fid)['cumulative_online_seconds'], 90)
                self.assertIsNone(reopened.monitoring_interval_seconds)
            finally:
                reopened.close()

    def test_online_scan_preserves_full_baseline_and_offlines_unseen_friends(self):
        self.db.configure_monitoring(0, at=T0)
        ids = self.db.apply_verified_scan([record(), record('234567890')], T0, 10, 'full', identity_complete=True)
        baseline = self.db.get_complete_roster()
        meta = dict(self.db._conn.execute('SELECT * FROM verified_scan').fetchone())
        self.db.apply_online_scan([record(home=b'new home')], at(60), 2, 'online')
        self.assertEqual(self.db.get_complete_roster(), baseline)
        self.assertEqual(dict(self.db._conn.execute('SELECT * FROM verified_scan').fetchone()), meta)
        self.assertEqual([self.db.get_friend(fid)['current_status'] for fid in ids], ['online', 'offline'])
        self.assertEqual(self.db.get_friend(ids[1])['cumulative_online_seconds'], 60)
        self.assertFalse(any(r['event_type'] == 'removed' for r in self.db.export_events()))
        self.assertEqual(self.db.get_profile_captures(ids[0])[0]['image'], b'new home')
        self.assertEqual(self.db._conn.execute("SELECT value FROM account_metadata WHERE key='presence_last_success_at'").fetchone()[0], at(60))

    def test_new_online_uid_is_displayed_without_replacing_complete_identity_list(self):
        self.db.apply_verified_scan([record()], T0, 10, 'full', identity_complete=True)
        baseline = self.db.get_complete_roster()
        self.db.apply_online_scan([record('345678901')], at(60), 2, 'online')
        self.assertEqual({r['uid'] for r in self.db.get_display_friends()}, {'123456789', '345678901'})
        self.assertEqual(self.db.get_complete_roster(), baseline)
        self.db.apply_online_scan([], at(120), 1, 'empty')
        self.assertTrue(all(r['current_status'] == 'offline' for r in self.db.get_display_friends()))
        self.assertEqual(len(self.db.get_all_friends()), 2)
        self.assertEqual(self.db.get_complete_roster(), baseline)

    def test_online_only_first_scan_does_not_create_full_baseline(self):
        self.db.apply_online_scan([record()], T0, 1, 'online')
        self.assertIsNone(self.db.get_complete_roster())
        self.assertEqual(len(self.db.get_display_friends()), 1)

    def test_later_full_scan_closes_absent_online_additions_without_deleting_history(self):
        self.db.configure_monitoring(0, at=T0)
        self.db.apply_verified_scan([record()], T0, 1, 'full', identity_complete=True)
        extra = self.db.apply_online_scan([record('345678901')], at(60), 1, 'online')[0]
        self.db.apply_verified_scan([record()], at(120), 1, 'full again', identity_complete=True)
        self.assertEqual([r['uid'] for r in self.db.get_display_friends(now=at(130))], ['123456789'])
        self.assertEqual(self.db.get_friend(extra)['current_status'], 'offline')
        self.assertEqual(self.db.get_friend(extra)['cumulative_online_seconds'], 60)
        self.assertEqual(len(self.db.get_all_friends()), 2)

    def test_online_scan_without_baseline_also_closes_prior_online_drafts(self):
        self.db.configure_monitoring(0, at=T0)
        draft = self.friend(uid=None)
        self.db.apply_online_scan([record()], at(60), 1, 'online')
        self.assertEqual(self.db.get_friend(draft)['current_status'], 'offline')
        self.assertEqual(self.db.get_friend(draft)['cumulative_online_seconds'], 60)

    def test_invalid_online_identity_or_non_online_status_cannot_mutate(self):
        self.db.apply_verified_scan([record()], T0, 1, 'full', identity_complete=True)
        before = list(self.db._conn.iterdump())
        for batch in ([record(None)], [record(), record()], [record(status='offline')]):
            with self.subTest(batch=batch), self.assertRaises(ValueError):
                self.db.apply_online_scan(batch, at(60), 1, 'invalid')
            self.assertEqual(list(self.db._conn.iterdump()), before)

    def test_online_commit_failure_rolls_back_status_images_and_success_time(self):
        self.db.configure_monitoring(0, at=T0)
        self.db.apply_verified_scan([record(), record('234567890')], T0, 1, 'full', identity_complete=True)
        before = list(self.db._conn.iterdump())
        bad = record('345678901')
        bad['name_image'] = None
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.apply_online_scan([record(home=b'new image'), bad], at(60), 1, 'broken')
        self.assertEqual(list(self.db._conn.iterdump()), before)

    def test_image_migration_is_atomic_and_preserves_capture_metadata(self):
        fid = self.friend()
        self.db.save_profile_capture(fid, b'old a', T0, signature='旧签名')
        self.db.save_profile_capture(fid, b'old b', at(60), signature='变更后的签名')
        original = self.db.get_profile_captures(fid)
        calls = []
        def fail_second(image, uid):
            calls.append((image, uid))
            if len(calls) == 2:
                raise ValueError('decode failure')
            return b'cropped '+image
        with self.assertRaises(ValueError):
            self.db.migrate_profile_captures(fail_second)
        self.assertEqual(self.db.get_profile_captures(fid), original)
        self.assertEqual(self.db.migrate_profile_captures(lambda image, uid: b'cropped '+image), 2)
        changed = self.db.get_profile_captures(fid)
        for old, new in zip(original, changed):
            self.assertEqual({k:v for k,v in old.items() if k != 'image'},
                             {k:v for k,v in new.items() if k != 'image'})
        self.assertEqual(self.db.migrate_profile_captures(lambda image, uid: image), 0)


if __name__ == '__main__':
    unittest.main()

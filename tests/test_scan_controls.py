"""Scan scheduling controls and database-bounded presence display; no game input."""
from tests import test_regressions as baseline

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app import config
from app.config import Config, parse_scan_frequency
from app.ui import main_window as ui


class ScanFrequencyConfigTests(unittest.TestCase):
    def test_blank_zero_and_fractional_minutes_have_distinct_meanings(self):
        for value, expected in ((None, None), ('', None), ('  ', None), ('0', 0.),
                                (0, 0.), ('.25', .25), (' 1.5 ', 1.5)):
            with self.subTest(value=value):
                self.assertEqual(expected, parse_scan_frequency(value))
        self.assertIsNone(Config().scan_frequency_minutes)
        self.assertFalse(Config().online_only)

    def test_invalid_negative_or_nonfinite_frequency_is_rejected(self):
        for value in ('-1', 'nan', 'NaN', 'inf', '-Inf', '1e309', '1e308', 'abc', '1,5', True, []):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_scan_frequency(value)

    def test_old_config_stays_loadable_and_new_options_round_trip(self):
        with tempfile.TemporaryDirectory(dir=baseline.WORK) as directory:
            path = Path(directory)/'config.json'
            path.write_text(json.dumps({'scan_interval_seconds': 30}), encoding='utf-8')
            with patch.object(config, 'CONFIG_PATH', str(path)), patch.object(Config, 'ensure_dirs'):
                old = Config.load()
                self.assertEqual(30, old.scan_interval_seconds)
                self.assertIsNone(old.scan_frequency_minutes)
                self.assertFalse(old.online_only)
                old.scan_frequency_minutes = .25
                old.online_only = True
                old.save()
                loaded = Config.load()
                self.assertEqual((.25, True, 30),
                                 (loaded.scan_frequency_minutes, loaded.online_only, loaded.scan_interval_seconds))


class ScanControlsTests(unittest.TestCase):
    def setUp(self):
        self.harness = baseline.Regressions('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (('scan_frequency_minutes', None), ('online_only', False), ('scan_once_only', False)):
            self.stack.enter_context(patch.object(ui.CONFIG, name, value))
        self.win = self.harness.ui()
        self.win._timer.stop()
        self.win.window_combo.addItem('test game', 123)
        self.yield_to_game = self.stack.enter_context(patch.object(self.win, '_yield_to_game'))

    def test_default_waits_for_manual_quick_scan_with_blank_frequency(self):
        self.assertEqual('快速扫描', self.win.start_btn.text())
        self.assertEqual('', self.win.frequency_edit.text())
        self.assertFalse(self.win.online_only_checkbox.isChecked())
        self.assertFalse(self.win.scanner.is_running)

    def test_start_applies_fractional_frequency_and_online_filter_and_locks_controls(self):
        self.win.frequency_edit.setText('.25')
        self.win.online_only_checkbox.setChecked(True)
        worker = MagicMock()
        worker.is_alive.return_value = True
        self.addCleanup(setattr, self.win.scanner, '_thread', None)

        def start():
            self.assertEqual(.25, ui.CONFIG.scan_frequency_minutes)
            self.assertTrue(ui.CONFIG.online_only)
            self.win.scanner._thread = worker
            return True

        with patch.object(self.win.scanner, 'start', side_effect=start):
            self.win._toggle_scan()
        self.assertEqual('停止扫描', self.win.start_btn.text())
        self.assertFalse(self.win.frequency_edit.isEnabled())
        self.assertFalse(self.win.online_only_checkbox.isEnabled())
        with patch.object(self.win.scanner, 'stop', side_effect=lambda: worker.is_alive.configure_mock(return_value=False)):
            self.win._toggle_scan()
        self.assertEqual('快速扫描', self.win.start_btn.text())
        self.assertTrue(self.win.frequency_edit.isEnabled())
        self.assertTrue(self.win.online_only_checkbox.isEnabled())

    def test_visible_frequency_replaces_legacy_scan_once_flag(self):
        with patch.object(ui.CONFIG, 'scan_once_only', True), \
                patch.object(self.win.scanner, 'start', return_value=True):
            for text, expected in (('', None), ('0', 0.), ('1.5', 1.5)):
                self.win.frequency_edit.setText(text)
                self.win._toggle_scan()
                self.assertEqual(expected, ui.CONFIG.scan_frequency_minutes)
                self.assertFalse(ui.CONFIG.scan_once_only)

    def test_invalid_frequency_never_starts_or_changes_options(self):
        self.win.online_only_checkbox.setChecked(True)
        with patch.object(self.win.scanner, 'start') as start, \
                patch.object(ui.QMessageBox, 'warning') as warning:
            for value in ('-1', 'nan', 'inf', '-Inf', '1e309', '1e308', 'abc'):
                self.win.frequency_edit.setText(value)
                self.win._toggle_scan()
            start.assert_not_called()
            self.assertEqual(7, warning.call_count)
        self.assertIsNone(ui.CONFIG.scan_frequency_minutes)
        self.assertFalse(ui.CONFIG.online_only)
        self.yield_to_game.assert_not_called()

    def test_uid_task_locks_options_and_idle_refresh_restores_quick_label(self):
        worker = MagicMock()
        worker.is_alive.return_value = True
        self.win.scanner._uid_thread = worker
        self.addCleanup(setattr, self.win.scanner, '_uid_thread', None)
        self.win._sync_task_controls()
        self.assertFalse(self.win.frequency_edit.isEnabled())
        self.assertFalse(self.win.online_only_checkbox.isEnabled())
        worker.is_alive.return_value = False
        self.win.start_btn.setText('停止扫描')
        self.win._refresh_durations()
        self.assertEqual('快速扫描', self.win.start_btn.text())
        self.assertTrue(self.win.frequency_edit.isEnabled())
        self.assertTrue(self.win.online_only_checkbox.isEnabled())

    def test_display_uses_database_expiry_and_does_not_add_an_unbounded_tail(self):
        friend = {'id': 1, 'uid': '111111111', 'nickname': 'friend', 'current_status': 'online',
                  'location': '千星奇域', 'total_online_seconds': 7,
                  'cumulative_online_seconds': 37, 'last_scan_at': '2000-01-01T00:00:00',
                  'qianxing_level': 0, 'tags': '探索', 'presence_monitoring': True}
        with patch.object(self.win.db, 'get_display_friends', return_value=[friend]) as rows, \
                patch.object(self.win.db, 'get_today_online_ranges', return_value=[]), \
                patch.object(self.win.db, 'get_status_summary', return_value={
                    'total':1, 'online':0, 'offline':1, 'unknown':0}):
            self.win._load_friends_table()
            self.win.friends_table.selectRow(0)
            self.assertEqual('7秒', self.win.friends_table.item(0, ui._FRIEND_COL['online_duration']).text())
            self.assertEqual('37秒', self.win.friends_table.item(0, ui._FRIEND_COL['cumulative_duration']).text())
            expired = dict(friend, current_status='offline', total_online_seconds=12,
                           cumulative_online_seconds=42, presence_monitoring=False)
            rows.return_value = [expired]
            self.win._refresh_durations()
            for field, text in (('status', '离线'), ('online_duration', '12秒'),
                                ('cumulative_duration', '42秒'), ('qianxing_level', '0'), ('tags', '探索')):
                self.assertEqual(text, self.win.friends_table.item(0, ui._FRIEND_COL[field]).text())
            self.assertEqual('在线: 0', self.win.lbl_online.text())
            self.assertEqual('离线: 1', self.win.lbl_offline.text())
            self.assertEqual(1, len(self.win.friends_table.selectionModel().selectedRows()))
            self.win._refresh_durations()
            self.assertEqual('12秒', self.win.friends_table.item(0, ui._FRIEND_COL['online_duration']).text())


if __name__ == '__main__':
    unittest.main()

"""Entry-point defaults never override the user's visible scan frequency."""
from tests import test_regressions as baseline

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import main
from app import config
from app.config import Config, CONFIG
from app.scanner import ScanResult
from app.ui import main_window as ui


class FrequencyMigrationTests(unittest.TestCase):
    def test_old_persisted_one_shot_flag_does_not_override_new_frequency(self):
        with tempfile.TemporaryDirectory(dir=baseline.WORK) as directory:
            path = Path(directory)/'config.json'
            for minutes in (None, 0., .25):
                with self.subTest(minutes=minutes):
                    path.write_text(json.dumps({'scan_once_only': True,
                        'scan_frequency_minutes': minutes}), encoding='utf-8')
                    with patch.object(config, 'CONFIG_PATH', str(path)), patch.object(Config, 'ensure_dirs'):
                        loaded = Config.load()
                    self.assertEqual(minutes, loaded.scan_frequency_minutes)
                    self.assertFalse(loaded.scan_once_only)
            path.write_text('{"scan_once_only":true}', encoding='utf-8')
            with patch.object(config, 'CONFIG_PATH', str(path)), patch.object(Config, 'ensure_dirs'):
                self.assertIsNone(Config.load().scan_frequency_minutes)


class FrequencyEntryAndUiTests(unittest.TestCase):
    def setUp(self):
        self.harness = baseline.Regressions('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = tempfile.TemporaryDirectory(dir=baseline.WORK)
        self.addCleanup(self.temp.cleanup)
        for name, value in (('scan_frequency_minutes', 0.), ('scan_once_only', True),
                            ('online_only', False), ('log_dir', self.temp.name)):
            self.stack.enter_context(patch.object(CONFIG, name, value))

    def launch(self, verify=True, real_window=True):
        window_type = ui.MainWindow
        self.at_construction = []
        def build():
            self.at_construction.append((CONFIG.scan_frequency_minutes, CONFIG.scan_once_only))
            if real_window:
                with patch.object(ui, 'Database', return_value=self.harness.db):
                    win = window_type()
                win._timer.stop()
                self.harness.addCleanup(win.close)
                self.stack.enter_context(patch.object(win, '_yield_to_game'))
                win.window_combo.addItem('simulated game', 123)
            else:
                win = MagicMock()
            self.win = win
            return win
        application = MagicMock()
        application.exec.return_value = 0
        with patch.object(main, 'enable_dpi_awareness'), \
                patch.object(main.sys, 'argv', ['main.py']+(['--scan-once'] if verify else [])), \
                patch('PySide6.QtWidgets.QApplication', return_value=application), \
                patch.object(ui, 'MainWindow', side_effect=build), \
                patch('PySide6.QtCore.QTimer.singleShot') as startup_timer:
            self.assertEqual(0, main.main())
            startup_timer.assert_not_called()
        return self.win

    def run_manual(self, frequency, expected_rounds, expected_interval):
        win = self.launch()
        self.assertEqual('', win.frequency_edit.text())
        win.frequency_edit.setText(frequency)
        scan = win.scanner
        starts, intervals, deadlines = [], [], []
        # Remove UI delivery from the simulated worker; it never captures or
        # sends game input. The real start/_run scheduling path remains active.
        scan.on_result = lambda result: None
        scan.on_message = lambda message: None
        def once():
            starts.append(len(starts)+1)
            intervals.append(self.harness.db.monitoring_interval_seconds)
            if len(starts) >= expected_rounds:
                scan._stop.set()
            return ScanResult(baseline.T0, 0, 0, 0, complete=True)
        def next_round(deadline):
            deadlines.append(deadline)
            return not scan._stop.is_set()
        with patch.object(scan, '_startup_countdown', return_value=True), \
                patch.object(scan, 'scan_once', side_effect=once), \
                patch.object(scan, '_wait_next_round', side_effect=next_round):
            win._toggle_scan()
            try:
                self.assertIsNotNone(scan._thread)
                scan._thread.join(2)
                self.assertFalse(scan._thread.is_alive(), 'simulated scan did not stop within its bound')
            finally:
                scan._stop.set()
                if scan._thread is not None:
                    scan._thread.join(2)
        self.assertEqual(expected_rounds, len(starts))
        self.assertEqual([expected_interval]*expected_rounds, intervals)
        self.assertFalse(CONFIG.scan_once_only)
        if expected_interval is None:
            self.assertEqual([], deadlines)
        return win

    def test_verify_launch_sets_visible_blank_default_before_window_and_never_autostarts(self):
        win = self.launch(real_window=False)
        self.assertEqual([(None, False)], self.at_construction)
        win._toggle_scan.assert_not_called()
        win.scanner.start.assert_not_called()
        report = json.loads((Path(self.temp.name)/'acceptance-latest.json').read_text(encoding='utf-8'))
        self.assertEqual('waiting_for_manual_start', report['status'])

    def test_normal_launch_preserves_saved_zero_without_autostart(self):
        win = self.launch(verify=False, real_window=False)
        self.assertEqual([(0., False)], self.at_construction)
        win._toggle_scan.assert_not_called()
        win.scanner.start.assert_not_called()

    def test_user_zero_after_verify_entry_runs_three_serial_rounds(self):
        self.run_manual('0', 3, 0.)

    def test_user_fraction_after_verify_entry_enables_periodic_monitoring(self):
        self.run_manual('.25', 2, 15.)

    def test_user_blank_after_verify_entry_is_one_snapshot(self):
        self.run_manual('', 1, None)


if __name__ == '__main__':
    unittest.main()

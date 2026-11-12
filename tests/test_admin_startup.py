"""Privilege startup and UI guards: mocked Windows APIs, no real elevation/input."""
from contextlib import ExitStack
import ctypes
import importlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
RETRY = '--gfm-elevation-retried'


class AdministratorStartupTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('app.privileges'),
                             'Normal startup needs the privilege gate')
        self.privileges = importlib.import_module('app.privileges')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(self.privileges.sys, 'platform', 'win32'))
        self.stack.enter_context(patch.object(self.privileges.sys, 'frozen', False, create=True))
        self.stack.enter_context(patch.object(self.privileges.sys, 'argv', ['main.py']))
        self.stack.enter_context(patch.object(self.privileges.sys, 'executable', r'C:\Python Test\pythonw.exe'))
        self.shell = MagicMock()
        self.shell.IsUserAnAdmin.return_value = 0
        self.user = MagicMock()
        self.user.MessageBoxW.return_value = 1
        self.dll = self.stack.enter_context(patch.object(
            ctypes, 'WinDLL', side_effect=lambda name, **kw: self.shell if name == 'shell32' else self.user))
        self.run_as = self.stack.enter_context(patch('admin_start._run_as', return_value=33))

    def test_token_check_uses_current_process_without_requesting_elevation(self):
        self.assertFalse(self.privileges.is_administrator())
        self.shell.IsUserAnAdmin.return_value = 1
        self.assertTrue(self.privileges.is_administrator())
        self.run_as.assert_not_called()
        self.user.MessageBoxW.assert_not_called()

    def test_token_api_error_fails_closed(self):
        self.shell.IsUserAnAdmin.side_effect = OSError('token unavailable')
        self.assertFalse(self.privileges.is_administrator())

    def test_non_windows_token_check_does_not_load_windows_libraries(self):
        with patch.object(self.privileges.sys, 'platform', 'linux'):
            self.assertFalse(self.privileges.is_administrator())
        self.dll.assert_not_called()

    def test_already_elevated_process_continues_without_uac(self):
        self.shell.IsUserAnAdmin.return_value = 1
        self.assertTrue(self.privileges.ensure_administrator())
        self.run_as.assert_not_called()
        self.user.MessageBoxW.assert_not_called()

    def test_source_relaunch_keeps_current_python_and_software_directory(self):
        with patch.object(self.privileges.sys, 'argv', ['elsewhere.py', '--scan-once']):
            self.assertFalse(self.privileges.ensure_administrator())
        executable, arguments, directory = self.run_as.call_args.args
        self.assertEqual(executable, r'C:\Python Test\pythonw.exe')
        self.assertEqual(directory, str(ROOT))
        self.assertEqual(arguments, f'{ROOT / "main.py"} --scan-once {RETRY}')
        self.user.MessageBoxW.assert_not_called()

    def test_source_relaunch_uses_windows_quoting_for_spaces_quotes_and_trailing_slash(self):
        software = Path(r'C:\Software With Space\原神助手')
        with patch.object(self.privileges, '__file__', str(software / 'app' / 'privileges.py')), \
                patch.object(self.privileges.sys, 'argv', ['main.py', '--label', 'quoted "name"', 'C:\\path with space\\']):
            self.assertFalse(self.privileges.ensure_administrator())
        self.assertEqual(self.run_as.call_args.args, (
            r'C:\Python Test\pythonw.exe',
            '"C:\\Software With Space\\原神助手\\main.py" --label "quoted \\"name\\"" "C:\\path with space\\\\" ' + RETRY,
            str(software)))

    def test_frozen_relaunch_uses_same_exe_without_python_or_extracted_script(self):
        with patch.object(self.privileges.sys, 'frozen', True), \
                patch.object(self.privileges.sys, 'executable', r'C:\App With Space\monitor.exe'), \
                patch.object(self.privileges.sys, 'argv', ['temporary-extracted-script', '--scan-once']):
            self.assertFalse(self.privileges.ensure_administrator())
        self.run_as.assert_called_once_with(
            r'C:\App With Space\monitor.exe', '--scan-once ' + RETRY, r'C:\App With Space')

    def test_cancelled_or_failed_uac_reports_native_error_and_stops(self):
        for code in (0, 5, 32):
            with self.subTest(code=code):
                self.run_as.reset_mock()
                self.user.MessageBoxW.reset_mock()
                self.run_as.return_value = code
                self.assertFalse(self.privileges.ensure_administrator())
                self.run_as.assert_called_once()
                self.assertEqual(self.user.MessageBoxW.call_count, 1)
                _, message, title, flags = self.user.MessageBoxW.call_args.args
                self.assertIn('管理员', title)
                self.assertIn(str(code), message)
                self.assertIn('取消', message)
                self.assertNotEqual(flags & 0x10, 0)

    def test_native_launch_exception_reports_failure_and_stops(self):
        self.run_as.side_effect = OSError('access denied')
        self.assertFalse(self.privileges.ensure_administrator())
        self.run_as.assert_called_once()
        self.assertIn('access denied', self.user.MessageBoxW.call_args.args[1])

    def test_unelevated_retry_never_repeats_uac(self):
        with patch.object(self.privileges.sys, 'argv', ['main.py', '--scan-once', RETRY]):
            self.assertFalse(self.privileges.ensure_administrator())
        self.run_as.assert_not_called()
        self.assertIn('仍未', self.user.MessageBoxW.call_args.args[1])

    def test_elevated_retry_consumes_internal_switch_and_preserves_user_arguments(self):
        self.shell.IsUserAnAdmin.return_value = 1
        with patch.object(self.privileges.sys, 'argv', ['main.py', '--scan-once', RETRY, '--label', 'abc']):
            self.assertTrue(self.privileges.ensure_administrator())
            self.assertEqual(sys.argv, ['main.py', '--scan-once', '--label', 'abc'])
        self.run_as.assert_not_called()

    def test_error_dialog_failure_still_stops_instead_of_continuing(self):
        self.run_as.return_value = 5
        self.user.MessageBoxW.side_effect = OSError('no interactive desktop')
        with patch('sys.stderr') as stderr:
            self.assertFalse(self.privileges.ensure_administrator())
        self.assertTrue(stderr.write.called)


class NonAdministratorUiTests(unittest.TestCase):
    def setUp(self):
        from tests import test_regressions as baseline
        from app.ui import main_window
        from app.database import Database
        self.ui = main_window
        self.database_type = Database
        self.assertTrue(hasattr(main_window, 'is_administrator'),
                        'UI must check privilege for direct construction')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(main_window, 'is_administrator', return_value=False))
        self.db = Database(':memory:')
        self.stack.enter_context(patch.object(main_window, 'Database', return_value=self.db))
        self.win = main_window.MainWindow()
        self.addCleanup(self.win.close)
        self.win.window_combo.addItem('isolated game', 123)
        self.warning = self.stack.enter_context(patch.object(main_window.QMessageBox, 'warning'))
        self.start = self.stack.enter_context(patch.object(self.win.scanner, 'start', return_value=True))
        self.uid_start = self.stack.enter_context(patch.object(self.win.scanner, 'start_uid_scan', return_value=True))
        self.activate = self.stack.enter_context(patch.object(main_window.win32window, 'bring_to_front'))
        self.stack.enter_context(patch.object(main_window.win32window, 'get_window_rect', return_value=(0, 0, 1280, 720)))
        self.thread = self.stack.enter_context(patch.object(main_window.threading, 'Thread'))

    def test_banner_and_disabled_game_actions_survive_ui_refresh(self):
        for refresh in (lambda: None, self.win._sync_task_controls, self.win._check_uid_scan_done):
            refresh()
            self.assertFalse(self.win.admin_warning.isHidden())
            self.assertIn('管理员', self.win.admin_warning.text())
            self.assertIn('导出', self.win.admin_warning.text())
            for button in (self.win.start_btn, self.win.uid_btn, self.win.detect_account_btn,
                           self.win.pause_btn):
                self.assertFalse(button.isEnabled())
            self.assertTrue(self.win.export_btn.isEnabled())
            self.assertTrue(self.win.mysql_btn.isEnabled())
            self.assertTrue(self.win.account_combo.isEnabled())

    def test_direct_scan_and_uid_calls_are_rejected_before_game_actions(self):
        self.win._toggle_scan()
        self.win._toggle_uid_scan()
        self.start.assert_not_called()
        self.uid_start.assert_not_called()
        self.activate.assert_not_called()
        self.assertEqual(self.warning.call_count, 2)
        self.assertIsNone(self.win.scan_started_at)

    def test_direct_account_capture_is_rejected_before_creating_worker(self):
        self.win._detect_account()
        self.thread.assert_not_called()
        self.assertFalse(self.win._account_reading)
        self.warning.assert_called_once()

    def test_direct_pause_and_focus_handoff_cannot_send_game_input(self):
        with patch.object(self.win.scanner, 'toggle_pause') as pause:
            self.win._toggle_pause()
            self.win._yield_to_game()
        pause.assert_not_called()
        self.activate.assert_not_called()

    def test_administrator_ui_keeps_existing_actions_and_hides_warning(self):
        self.win.close()
        self.db = self.database_type(':memory:')
        with patch.object(self.ui, 'is_administrator', return_value=True), \
                patch.object(self.ui, 'Database', return_value=self.db):
            win = self.ui.MainWindow()
        self.addCleanup(win.close)
        self.assertTrue(win.admin_warning.isHidden())
        self.assertTrue(win.start_btn.isEnabled())
        self.assertTrue(win.uid_btn.isEnabled())
        self.assertEqual(win.uid_btn.text(), '初始化档案')

    def test_account_capture_routes_selected_window_handle(self):
        self._check_account_capture_route(None, (0, 0, 1280, 720), 123)

    def test_manual_account_capture_ignores_stale_window_selection(self):
        self._check_account_capture_route((10, 20, 1290, 740), (10, 20, 1290, 740), None)

    def _check_account_capture_route(self, manual_rect, expected_rect, expected_hwnd):
        self.win._manual_rect = manual_rect
        self.thread.return_value.start.side_effect = lambda: self.thread.call_args.kwargs['target']()
        emitted = MagicMock()
        bridge = SimpleNamespace(account_identity=SimpleNamespace(emit=emitted))
        with patch.object(self.win, '_administrator', True), \
                patch.object(self.win, 'bridge', bridge), \
                patch.object(self.win, '_validate_game_view', return_value=True), \
                patch('app.account_identity.read_current_account', return_value='123456789') as read:
            self.win._detect_account()
        read.assert_called_once_with(expected_rect, hwnd=expected_hwnd)
        emitted.assert_called_once_with(('123456789', None))
        # The synchronous worker's signal was isolated from account switching.
        self.win._account_reading = False


if __name__ == '__main__':
    unittest.main()

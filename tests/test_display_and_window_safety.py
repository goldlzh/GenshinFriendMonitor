"""Client aspect failures must be bounded; visible controls reflect retained data."""
from tests import test_regressions as baseline

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app import friend_screen
from app.scanner import Scanner
from app.ui import main_window as ui
from app.excel_export import export_friends_to_excel
from openpyxl import load_workbook

REAL_ENSURE = Scanner._ensure_friend_list


class WindowAndDisplayTests(unittest.TestCase):
    def setUp(self):
        self.harness = baseline.Regressions('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.stack = self.harness.stack
        self.stack.enter_context(patch.object(baseline.CONFIG, 'scan_frequency_minutes', None))
        self.stack.enter_context(patch.object(baseline.CONFIG, 'save_profile_screenshots', True))

    def test_supported_and_unsupported_game_client_sizes(self):
        for width, height in ((1280,720), (1920,1080), (2560,1440), (2547,1431)):
            self.assertEqual('', friend_screen.game_window_problem((10,20,10+width,20+height)))
        for width, height in ((1024,768), (2560,1080), (0,0), (320,180)):
            self.assertIn('16:9', friend_screen.game_window_problem((0,0,width,height)))

    def test_zero_frequency_unsupported_window_stops_after_one_result_without_capture_or_input(self):
        scan = self.harness.scanner()
        scan.set_manual_rect((0,0,1024,768))
        results = []
        scan.on_result = results.append
        with patch.object(baseline.CONFIG, 'scan_frequency_minutes', 0), \
                patch.object(scan, '_startup_countdown', return_value=True), \
                patch.object(scan, '_ensure_friend_list', side_effect=lambda r: REAL_ENSURE(scan,r)), \
                patch.object(baseline.window, 'find_client_window', return_value=None), \
                patch.object(baseline.capture, 'capture_region') as shot, \
                patch.object(scan, '_navigation_key') as keys:
            scan._run()
        self.assertTrue(scan._stop.is_set())
        self.assertEqual(1, len(results))
        self.assertFalse(results[0].complete)
        self.assertIn('16:9', results[0].message)
        shot.assert_not_called()
        keys.assert_not_called()

    def test_aspect_is_checked_again_after_window_changes_during_navigation(self):
        scan = self.harness.scanner()
        with patch.object(scan, '_get_rect', return_value=(0,0,2560,1080)), \
                patch.object(baseline.window, 'find_client_window', return_value=None), \
                patch.object(baseline.capture, 'capture_region') as shot:
            self.assertFalse(REAL_ENSURE(scan, (0,0,1920,1080)))
        self.assertTrue(scan._stop.is_set())
        shot.assert_not_called()

    def window(self):
        win = self.harness.ui()
        win._timer.stop()
        win.window_combo.addItem('fake game', 123)
        return win

    def test_ui_rejects_non_16_9_before_start_or_account_ocr(self):
        win = self.window()
        win._manual_rect = (0,0,1024,768)
        with patch.object(ui.QMessageBox, 'warning') as warning, \
                patch.object(win.scanner, 'start') as start, \
                patch.object(ui.threading, 'Thread') as worker:
            win._toggle_scan()
            win._detect_account()
        self.assertEqual(2, warning.call_count)
        self.assertIn('16:9', win.aspect_label.text())
        self.assertFalse(win._account_reading)
        start.assert_not_called()
        worker.assert_not_called()

    def test_profile_checkbox_changes_only_profile_option_and_is_locked_during_scan(self):
        win = self.window()
        evidence = baseline.CONFIG.save_scan_evidence
        with patch.object(baseline.CONFIG, 'save') as save:
            win.save_profile_checkbox.setChecked(False)
        self.assertFalse(baseline.CONFIG.save_profile_screenshots)
        self.assertEqual(evidence, baseline.CONFIG.save_scan_evidence)
        save.assert_called_once()
        self.assertIn('KiB', win.profile_space_label.text())
        with patch.object(win, '_task_busy', return_value=True):
            win._sync_task_controls()
        self.assertFalse(win.save_profile_checkbox.isEnabled())

    def test_activity_columns_remain_visible_and_system_events_are_hidden(self):
        win = self.window()
        friend = dict(id=1, nickname='好友', qianxing_activity='编辑内容中',
            qianxing_activity_summary='编辑内容中：00:01:00',
            qianxing_activity_ranges='编辑内容中：10:00-10:01', current_status='online')
        with patch.object(win.db, 'get_display_friends', return_value=[friend]), \
                patch.object(win.db, 'get_today_online_ranges', return_value=[]):
            win._load_friends_table()
        for field in ('qianxing_activity','qianxing_activity_summary','qianxing_activity_ranges'):
            self.assertEqual(friend[field], win.friends_table.item(0,ui._FRIEND_COL[field]).text())
        with patch.object(win.db, 'get_recent_events', return_value=[{'event_type':'scan_default_offline'}]):
            win._load_events_table()
        self.assertEqual(0, win.events_table.rowCount())

    def test_signature_pending_does_not_get_confirmed_by_unrelated_manual_edit(self):
        dialog = ui.EditFriendDialog(dict(nickname='名字', signature='旧签名', signature_pending=True))
        self.addCleanup(dialog.close)
        self.assertFalse(dialog.signature_confirmed())
        dialog._nickname.setText('更正名字')
        self.assertFalse(dialog.signature_confirmed())
        dialog.signature_reviewed.setChecked(True)
        self.assertTrue(dialog.signature_confirmed())
        dialog._signature.setText('')
        self.assertIsNone(dialog.signature())

    def test_timer_refreshes_status_without_exposing_default_offline_event(self):
        win = self.window()
        friend = dict(id=1, nickname='好友', current_status='online', status_updated_at='10:00:00')
        with patch.object(win.db, 'get_display_friends', return_value=[friend]), \
                patch.object(win.db, 'get_today_online_ranges', return_value=[]):
            win._load_friends_table()
            friend.update(current_status='offline', status_updated_at='10:01:00')
            event = dict(id=20, event_type='scan_default_offline', event_time='10:01:00')
            with patch.object(win.db, 'get_recent_events', return_value=[event]):
                win._refresh_durations()
        self.assertEqual(0, win.events_table.rowCount())
        self.assertEqual('10:01:00', win.friends_table.item(0,ui._FRIEND_COL['status_updated_at']).text())

    def test_excel_keeps_unknown_status_and_exports_only_real_changes(self):
        with tempfile.TemporaryDirectory(dir=baseline.WORK) as directory:
            path = export_friends_to_excel([{'nickname':'好友','current_status':'unknown',
                'qianxing_activity':'编辑内容中','qianxing_activity_summary':'编辑内容中：00:00:10',
                'qianxing_activity_ranges':'编辑内容中：10:00-10:01'}],
                [{'event_type': kind} for kind in ('scan_default_offline', 'observed_online',
                 'online', 'offline', 'unknown', 'manual_import', 'renamed', 'added', 'removed')], directory)
            wb = load_workbook(path)
            try:
                rows = list(wb['好友信息'].values)
                values = dict(zip(rows[0],rows[1]))
                self.assertEqual('未知', values['当前状态'])
                self.assertEqual('编辑内容中', values['游玩奇域'])
                self.assertEqual(['上线', '下线', '新增', '消失/可能被删'],
                                 [row[3] for row in list(wb['状态变动'].values)[1:]])
            finally:
                wb.close()


if __name__ == '__main__':
    unittest.main()

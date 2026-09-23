"""Account selection and image viewing, using isolated databases and no game input."""
import json
import atexit
import logging
import os
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from tests import test_regressions as _baseline  # Share the suite's isolated configuration.
atexit.register(logging.shutdown)


class AccountUiTests(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        from app.ui import main_window
        from app.database import Database
        self.ui = main_window
        self.qt = QApplication.instance() or QApplication([])
        self.temp = tempfile.TemporaryDirectory(prefix='account-ui-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'friends.db'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(main_window.CONFIG, 'db_path', str(self.path)))
        self.stack.enter_context(patch.object(main_window.CONFIG, 'log_dir', self.temp.name))
        self.stack.enter_context(patch.object(main_window, 'Database', side_effect=lambda path=None: Database(path or str(self.path))))
        self.win = main_window.MainWindow()
        self.addCleanup(self.win.close)

    def choose(self, uid):
        self.win._on_account_identity((uid, None))

    def test_unowned_startup_waits_for_user_and_disables_scan(self):
        self.assertEqual(self.win.account_combo.currentText(), '旧数据（未归属）')
        self.assertFalse(self.win.start_btn.isEnabled())
        self.assertFalse(self.win.uid_btn.isEnabled())
        self.assertFalse(self.win.scanner.is_running)
        self.assertIsNone(self.win.db.account_uid)

    def test_accounts_keep_independent_rosters_and_reset_runtime_only_once(self):
        with patch.object(self.ui.Scanner, 'start') as start:
            self.choose('111111111')
            start.assert_not_called()
        fid = self.win.db.add_friend('123456789', '甲', 60, None, 'online', '2026-09-21T12:00:00')
        first_db = self.win.db
        self.win.db._conn.execute('UPDATE friends SET total_online_seconds=13 WHERE id=?', (fid,))
        self.win.db._conn.commit()
        with patch.object(self.ui.QMessageBox, 'information'):
            self.choose('222222222')
        self.assertEqual(self.win.db.get_all_friends(), [])
        self.win.db.add_friend('987654321', '乙', 60, None, 'offline', '2026-09-21T12:01:00')
        self.win.account_combo.setCurrentIndex(self.win.account_combo.findData('111111111'))
        self.assertEqual(self.win.db.get_all_friends()[0]['nickname'], '甲')
        self.assertEqual(self.win.db.get_friend(fid)['total_online_seconds'], 13)
        self.assertEqual(self.win.db.account_uid, '111111111')
        self.assertEqual(Path(self.ui.CONFIG.db_path), self.win.account_store.path_for('111111111'))
        from PySide6.QtTest import QSignalSpy
        results = QSignalSpy(self.win.bridge.result)
        self.win._dispatch_result((first_db, object()))
        self.assertEqual(results.count(), 0)

    def test_running_scan_blocks_account_switch_and_detection(self):
        self.choose('111111111')
        other = self.win.account_store.open_account('222222222')
        other.close()
        self.win._refresh_account_choices('111111111')
        worker = MagicMock()
        worker.is_alive.return_value = True
        self.win.scanner._thread = worker
        try:
            self.win._sync_task_controls()
            self.assertFalse(self.win.account_combo.isEnabled())
            self.assertFalse(self.win.detect_account_btn.isEnabled())
            self.win.account_combo.setCurrentIndex(self.win.account_combo.findData('222222222'))
            self.assertEqual(self.win._account_uid, '111111111')
            self.assertEqual(self.win.account_combo.currentData(), '111111111')
        finally:
            worker.is_alive.return_value = False

    def test_explicit_import_copies_legacy_to_selected_empty_account(self):
        self.win.db.add_friend('123456789', '旧好友', 60, None, 'offline', '2026-09-21T12:00:00')
        self.choose('111111111')
        with patch.object(self.ui.QMessageBox, 'question', return_value=self.ui.QMessageBox.Yes):
            self.win._import_legacy()
        self.assertEqual(self.win.db.account_uid, '111111111')
        self.assertEqual(self.win.db.get_all_friends()[0]['nickname'], '旧好友')
        self.win.account_combo.setCurrentIndex(0)
        self.assertEqual(self.win.db.get_all_friends()[0]['nickname'], '旧好友')
        self.assertIsNone(self.win.db.account_uid)

    @staticmethod
    def png():
        from PySide6.QtCore import QByteArray, QBuffer, QIODevice
        from PySide6.QtGui import QColor, QImage
        image = QImage(600, 60, QImage.Format_RGB32)
        image.fill(QColor('white'))
        image.setPixelColor(300, 30, QColor('black'))
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, 'PNG')
        return bytes(data)

    def test_nickname_image_preserves_full_region_and_blank_spacing(self):
        pixmap = self.ui.nickname_pixmap(self.png())
        self.assertEqual((pixmap.width(), pixmap.height()), (260, 26))

    def test_profile_viewer_keeps_two_newest_and_displays_empty_state(self):
        from PySide6.QtWidgets import QLabel
        data = self.png()
        viewer = self.ui.ProfileCaptureDialog([
            dict(id=i, observed_at=f'2026-09-2{i}T12:00:00', nickname=f'昵称{i}', image=data)
            for i in (1, 3, 2)
        ], '好友')
        self.addCleanup(viewer.close)
        self.assertEqual([row['id'] for row in viewer.captures], [3, 2])
        self.assertFalse(viewer.preview.pixmap().isNull())
        viewer.selector.setCurrentIndex(1)
        self.assertFalse(viewer.preview.pixmap().isNull())
        viewer.zoom_combo.setCurrentIndex(1)
        self.assertEqual((viewer.preview.pixmap().width(), viewer.preview.pixmap().height()), (600, 60))
        self.assertFalse(viewer.scroll_area.widgetResizable())
        empty = self.ui.ProfileCaptureDialog([], '好友')
        self.addCleanup(empty.close)
        self.assertTrue(any('待采集' in label.text() for label in empty.findChildren(QLabel)))

    def test_scan_once_launch_only_records_waiting_without_start_timer(self):
        import main
        from app.config import CONFIG
        window = MagicMock()
        application = MagicMock()
        application.exec.return_value = 0
        with patch.object(main, 'enable_dpi_awareness'), \
                patch.object(main.sys, 'argv', ['main.py', '--scan-once']), \
                patch('PySide6.QtWidgets.QApplication', return_value=application), \
                patch.object(self.ui, 'MainWindow', return_value=window), \
                patch.object(CONFIG, 'log_dir', self.temp.name), \
                patch.object(CONFIG, 'scan_once_only', False), \
                patch.object(CONFIG, 'scan_frequency_minutes', None), \
                patch('PySide6.QtCore.QTimer.singleShot') as timer:
            self.assertEqual(main.main(), 0)
            timer.assert_not_called()
            window._toggle_scan.assert_not_called()
            status = json.loads((Path(self.temp.name) / 'acceptance-latest.json').read_text(encoding='utf-8'))
            self.assertEqual(status['status'], 'waiting_for_manual_start')


if __name__ == '__main__':
    unittest.main()

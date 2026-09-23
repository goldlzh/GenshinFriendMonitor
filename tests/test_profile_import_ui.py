"""Clipboard and search behavior using synthetic images and isolated accounts."""
from tests import test_regressions as baseline

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QDialog

from app.profile_import import ProfileDraft
from app.ui import main_window as ui
from app.ui import profile_import_dialog as dialogs
from tests import test_account_ui as account_fixture
from tests.test_profile_import_storage import picture


class ProfileImportUiTests(unittest.TestCase):
    def setUp(self):
        self.harness = account_fixture.AccountUiTests('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.harness.choose('111111111')
        self.win = self.harness.win
        self.win._timer.stop()
        self.db = self.win.db
        self.clipboard = MagicMock()
        self.clipboard.image.return_value = QImage()
        self.clipboard_patch = patch.object(ui.QApplication, 'clipboard', return_value=self.clipboard)
        self.clipboard_patch.start()
        self.addCleanup(self.clipboard_patch.stop)

    def test_search_matches_uid_partial_uid_name_and_mixed_keywords(self):
        first = self.db.add_friend('123456789', 'Alpha', 60, '', 'offline', baseline.T0)
        second = self.db.add_friend('223450001', 'Beta', 60, '', 'offline', baseline.T0)
        third = self.db.add_friend(None, 'Gamma', 60, '', 'offline', baseline.T0)
        self.win._load_friends_table()
        for query, expected in (('123456789', [first]), ('22345', [second]),
                                ('ALPHA 0001', [first, second]), ('Gamma', [third])):
            with self.subTest(query=query):
                self.win.search_edit.setText(query)
                self.win._select_matching_friends()
                self.assertEqual(expected, self.win._selected_friend_ids())
        self.db._conn.execute('UPDATE friends SET nickname_pending=1 WHERE id=?', (first,))
        self.db._conn.commit()
        self.win._load_friends_table()
        self.win.search_edit.setText('123456789')
        self.win._select_matching_friends()
        self.assertEqual([first], self.win._selected_friend_ids())

    def mock_dialog(self, accepted=True):
        dialog = MagicMock()
        dialog.exec.return_value = QDialog.Accepted if accepted else QDialog.Rejected
        dialog.values.return_value = dict(uid='123456789', nickname='测试好友', level=60,
                                         signature='签名', image_bytes=picture())
        return dialog

    def test_manual_import_is_visible_in_history_and_can_be_found_by_uid(self):
        old = self.db.add_friend('223456789', '原好友', 60, '', 'offline', baseline.T0)
        self.db.apply_verified_scan([dict(id=old, uid='223456789', entry=baseline.entry('原好友'),
                                          name_image=picture(), avatar_image=picture())],
                                     baseline.T0, 0, 'isolated')
        self.win._load_friends_table()
        before = self.db.get_complete_roster()
        dialog = self.mock_dialog()
        def confirm():
            self.assertTrue(self.win._task_busy())
            self.assertFalse(self.win.account_combo.isEnabled())
            self.assertFalse(self.win.start_btn.isEnabled())
            self.assertFalse(self.win.paste_profile_btn.isEnabled())
            return QDialog.Accepted
        dialog.exec.side_effect = confirm
        with patch.object(ui, 'ProfileImportDialog', return_value=dialog):
            self.win._paste_friend_profile()
        self.assertEqual(before, self.db.get_complete_roster())
        self.assertTrue(self.win.show_history.isChecked())
        self.assertTrue(self.win.paste_profile_btn.isEnabled())
        self.assertEqual('manual_import', self.db.get_recent_events()[0]['event_type'])
        self.assertNotIn('手动录入主页', [self.win.events_table.item(row, 3).text()
                                      for row in range(self.win.events_table.rowCount())])
        self.win.search_edit.setText('123456789')
        self.win._select_matching_friends()
        self.assertEqual([self.db.find_by_uid('123456789')['id']], self.win._selected_friend_ids())

    def test_cancel_and_running_scan_do_not_modify_database(self):
        before = list(self.db._conn.iterdump())
        with patch.object(ui, 'ProfileImportDialog', return_value=self.mock_dialog(False)):
            self.win._paste_friend_profile()
        self.assertEqual(before, list(self.db._conn.iterdump()))
        worker = MagicMock()
        worker.is_alive.return_value = True
        self.win.scanner._thread = worker
        try:
            with patch.object(ui, 'ProfileImportDialog') as dialog:
                self.win._sync_task_controls()
                self.assertFalse(self.win.paste_profile_btn.isEnabled())
                self.win._paste_friend_profile()
                dialog.assert_not_called()
        finally:
            worker.is_alive.return_value = False


class ProfileImportDialogTests(unittest.TestCase):
    def setUp(self):
        self.dialog = dialogs.ProfileImportDialog([], '111111111')
        self.addCleanup(self.cleanup_dialog)

    def cleanup_dialog(self):
        if self.dialog.worker is not None:
            self.dialog.worker.wait(5000)
        self.dialog.close()
        baseline.QT.processEvents()

    def wait_for_read(self):
        deadline = time.monotonic()+5
        while self.dialog.worker.isRunning() and time.monotonic() < deadline:
            baseline.QT.processEvents()
            time.sleep(.01)
        baseline.QT.processEvents()
        self.assertFalse(self.dialog.worker.isRunning())

    @staticmethod
    def image():
        image = QImage(401, 240, QImage.Format_RGB32)
        image.fill(QColor('navy'))
        return image

    def test_clipboard_ocr_produces_editable_draft_without_auto_accepting(self):
        clipboard = MagicMock()
        clipboard.image.return_value = self.image()
        draft = ProfileDraft('123456789', '合成昵称', 60, '合成签名')
        with patch.object(dialogs.QApplication, 'clipboard', return_value=clipboard), \
                patch.object(dialogs, 'recognize_profile', return_value=draft) as recognize:
            self.dialog.paste_clipboard()
            self.wait_for_read()
        self.assertEqual((240, 401, 3), recognize.call_args.args[0].shape)
        self.assertEqual('123456789', self.dialog.uid.text())
        self.assertEqual('合成昵称', self.dialog.nickname.text())
        self.assertEqual(60, self.dialog.level.value())
        self.assertEqual('合成签名', self.dialog.signature.toPlainText())
        self.assertNotEqual(QDialog.Accepted, self.dialog.result())
        self.dialog.accept()
        self.assertEqual(QDialog.Accepted, self.dialog.result())

    def test_replacement_image_clears_old_fields_and_unknown_signature_is_not_applied(self):
        drafts = [ProfileDraft('123456789', '原图', 60, '原签名'), ProfileDraft(missing=('UID','昵称','签名'))]
        with patch.object(dialogs, 'recognize_profile', side_effect=drafts):
            self.dialog.set_image(self.image())
            self.wait_for_read()
            self.dialog.set_image(self.image())
            self.wait_for_read()
        self.assertEqual('', self.dialog.uid.text())
        self.assertEqual('', self.dialog.nickname.text())
        self.assertFalse(self.dialog.update_signature.isChecked())
        self.dialog.accept()
        self.assertNotEqual(QDialog.Accepted, self.dialog.result())
        self.dialog.uid.setText('123456789')
        self.dialog.nickname.setText('人工确认')
        self.dialog.accept()
        self.assertEqual(QDialog.Accepted, self.dialog.result())

    def test_cancel_during_ocr_waits_for_thread_and_never_accepts(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def slow(_):
            release.wait(3)
            return ProfileDraft('123456789','未保存',60,'')
        with patch.object(dialogs, 'recognize_profile', side_effect=slow):
            self.dialog.set_image(self.image())
            self.dialog.reject()
            self.assertTrue(self.dialog._cancel_requested)
            release.set()
            self.wait_for_read()
        self.assertEqual(QDialog.Rejected, self.dialog.result())

    def test_empty_clipboard_and_ocr_failure_allow_safe_manual_recovery(self):
        clipboard = MagicMock()
        clipboard.image.return_value = QImage()
        with patch.object(dialogs.QApplication, 'clipboard', return_value=clipboard):
            self.dialog.paste_clipboard()
        self.assertIsNone(self.dialog.image_bytes)
        with patch.object(dialogs, 'recognize_profile', side_effect=RuntimeError('模型不可用')):
            self.dialog.set_image(self.image())
            self.wait_for_read()
        self.assertIn('手动填写', self.dialog.status.text())
        self.assertTrue(self.dialog.uid.isEnabled())


if __name__ == '__main__':
    unittest.main()

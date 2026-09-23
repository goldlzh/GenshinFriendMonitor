"""Independent adventure/Qianxing fields in the shared UID-based friend UI."""
from tests import test_regressions as _baseline
from tests import test_account_ui as _account_ui
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QDialog


class QianxingUiTests(unittest.TestCase):
    setUp = _account_ui.AccountUiTests.setUp

    def add_friend(self, *, qianxing_level=12, tags='探索 / 联机', status='offline'):
        fid = self.win.db.add_friend('333333333', '旅行者', 60, '一起探索', status, '2026-09-21T12:00:00')
        self.win.db.update_friend_manual(fid, '333333333', '旅行者', 60, '一起探索',
                                         qianxing_level=qianxing_level, tags=tags)
        self.win._load_friends_table()
        return fid

    def text(self, key):
        return self.win.friends_table.item(0, self.ui._FRIEND_COL[key]).text()

    def test_both_levels_and_tags_use_one_friend_row(self):
        self.add_friend()
        self.assertEqual(self.win.friends_table.rowCount(), 1)
        self.assertEqual(self.text('level'), '60')
        self.assertEqual(self.text('qianxing_level'), '12')
        self.assertEqual(self.text('tags'), '探索 / 联机')
        headers = [self.win.friends_table.horizontalHeaderItem(i).text()
                   for i in range(self.win.friends_table.columnCount())]
        self.assertIn('冒险等级', headers)
        self.assertIn('千星等级', headers)
        self.assertIn('标签', headers)
        self.assertEqual(headers[-1], '主页')

    def test_duration_refresh_keeps_qianxing_fields_and_status(self):
        self.add_friend(status='online')
        with patch.object(self.win, '_online_secs', return_value=7), \
                patch.object(self.win, '_cumulative_secs', return_value=37):
            self.win._refresh_durations()
        self.assertEqual(self.text('online_duration'), '7秒')
        self.assertEqual(self.text('cumulative_duration'), '37秒')
        self.assertEqual(self.text('status'), '在线')
        self.assertEqual(self.text('qianxing_level'), '12')
        self.assertEqual(self.text('tags'), '探索 / 联机')

    def test_edit_saves_and_clears_qianxing_fields_without_changing_adventure_level(self):
        fid = self.add_friend()
        dialog = self.ui.EditFriendDialog(self.win.db.get_friend(fid))
        self.addCleanup(dialog.close)
        self.assertEqual((dialog.level(), dialog.qianxing_level(), dialog.tags()), (60, 12, '探索 / 联机'))
        dialog._qianxing_level.setValue(0)
        dialog._tags.setText('创作达人')
        with patch.object(self.ui, 'EditFriendDialog', return_value=dialog), \
                patch.object(dialog, 'exec', return_value=QDialog.Accepted):
            self.win._edit_friend(0)
        saved = self.win.db.get_friend(fid)
        self.assertEqual((saved['level'], saved['qianxing_level'], saved['tags']), (60, 0, '创作达人'))
        self.assertEqual(self.text('qianxing_level'), '0')
        dialog._qianxing_level.setValue(-1)
        dialog._tags.clear()
        with patch.object(self.ui, 'EditFriendDialog', return_value=dialog), \
                patch.object(dialog, 'exec', return_value=QDialog.Accepted):
            self.win._edit_friend(0)
        saved = self.win.db.get_friend(fid)
        self.assertEqual((saved['level'], saved['qianxing_level'], saved['tags']), (60, None, ''))
        self.assertEqual(self.win.friends_table.rowCount(), 1)
        self.assertEqual(self.text('qianxing_level'), '')

    def test_nickname_image_and_profile_button_still_use_correct_columns(self):
        fid = self.add_friend()
        image = _account_ui.AccountUiTests.png()
        with self.win.db._conn:
            self.win.db._conn.execute('UPDATE friends SET nickname=?,nickname_pending=1 WHERE id=?', ('', fid))
            self.win.db._conn.execute('INSERT OR REPLACE INTO visual_profiles '
                                     '(friend_id,name_image,avatar_image,observed_at) VALUES (?,?,?,?)',
                                      (fid, image, image, '2026-09-21T12:00:00'))
        self.win._load_friends_table()
        nickname_col = self.ui._FRIEND_COL['nickname']
        nickname = self.win.friends_table.item(0, nickname_col)
        self.assertEqual(nickname.text(), '')
        self.assertFalse(nickname.icon().isNull())
        with patch.object(self.win, '_show_profile_captures') as show:
            self.win.friends_table.cellWidget(0, self.ui._FRIEND_COL['profile']).click()
            show.assert_called_once_with(fid)
        with patch.object(self.win, '_edit_friend') as edit:
            self.win.friends_table.cellDoubleClicked.emit(0, nickname_col)
            edit.assert_called_once_with(0)

    def test_unknown_fields_and_narrow_window_preserve_readable_headers(self):
        self.add_friend(qianxing_level=None, tags=None)
        self.assertEqual(self.text('qianxing_level'), '')
        self.assertEqual(self.text('tags'), '')
        dialog = self.ui.EditFriendDialog(self.win.db.get_all_friends()[0])
        self.addCleanup(dialog.close)
        self.assertIsNone(dialog.qianxing_level())
        self.assertIsNone(dialog.tags())
        self.win.resize(1040, 700)
        self.win.show()
        self.qt.processEvents()
        header = self.win.friends_table.horizontalHeader()
        for key in ('level', 'qianxing_level', 'tags', 'status'):
            index = self.ui._FRIEND_COL[key]
            label = self.win.friends_table.horizontalHeaderItem(index).text()
            self.assertGreaterEqual(header.sectionSize(index), header.fontMetrics().horizontalAdvance(label) + 10)
        self.assertGreater(self.win.friends_table.horizontalScrollBar().maximum(), 0)


if __name__ == '__main__':
    unittest.main()

"""Display-only trimming and compact columns; no game input or player data."""
from tests import test_account_ui as _account_ui
from tests import test_regressions as _baseline

import unittest
from unittest.mock import patch

from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QLabel


def nickname_image(*, accent=False, blank=False, wide=False):
    image = QImage(640, 64, QImage.Format_RGB32)
    image.fill(QColor(215, 215, 218))
    if not blank:
        rectangles = ((16, 30), (300, 314), (600, 614)) if wide else ((16, 30), (55, 69), (90, 104))
        for left, right in rectangles:
            for x in range(left, right):
                for y in range(20, 43):
                    color = QColor(0, 80, 200) if wide and left == 600 else QColor(40, 55, 75)
                    image.setPixelColor(x, y, color)
        if accent:
            image.setPixelColor(123, 14, QColor(30, 40, 60))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, 'PNG')
    return bytes(data)


class CompactFriendTableTests(unittest.TestCase):
    setUp = _account_ui.AccountUiTests.setUp

    def test_preview_removes_long_blank_tail_without_upscaling(self):
        data = nickname_image()
        preview = self.ui.nickname_pixmap(data, 640, 80, trim=True)
        self.assertLess(preview.width(), 140)
        self.assertEqual(64, preview.height())
        self.assertEqual(data, nickname_image())

    def test_preview_preserves_internal_spacing_and_detached_accent(self):
        preview = self.ui.nickname_pixmap(nickname_image(accent=True), 640, 80, trim=True)
        image = preview.toImage()
        self.assertGreaterEqual(preview.width(), 114)
        self.assertLess(preview.width(), 150)
        self.assertEqual(QColor(215, 215, 218), image.pixelColor(30, 30))
        self.assertEqual(QColor(30, 40, 60), image.pixelColor(preview.width()-7, 14))

    def test_blank_preview_keeps_source_dimensions(self):
        preview = self.ui.nickname_pixmap(nickname_image(blank=True), 640, 80, trim=True)
        self.assertEqual((640, 64), (preview.width(), preview.height()))

    def test_invalid_preview_stays_null(self):
        self.assertTrue(self.ui.nickname_pixmap(b'not a PNG', trim=True).isNull())

    def test_decorated_background_is_not_mistaken_for_blank_tail(self):
        image = QImage(640, 64, QImage.Format_RGB32)
        for x in range(640):
            color = QColor(20, 40, 80) if x % 2 else QColor(230, 190, 90)
            for y in range(64):
                image.setPixelColor(x, y, color)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, 'PNG')
        preview = self.ui.nickname_pixmap(bytes(data), 640, 80, trim=True)
        self.assertEqual((640, 64), (preview.width(), preview.height()))

    def add_friend(self):
        return self.win.db.add_friend('333333333', '测试昵称', 60, '合成签名',
                                      'offline', '2026-11-12T12:00:00')

    def test_empty_columns_are_compact_and_long_text_is_bounded(self):
        fid = self.add_friend()
        signature = '很长的合成签名' * 100
        self.win.db.update_friend_manual(fid, '333333333', '测试昵称', 60, signature)
        self.win._load_friends_table()
        for key in ('tags', 'qianxing_activity', 'qianxing_activity_ranges'):
            self.assertLessEqual(self.win.friends_table.columnWidth(self.ui._FRIEND_COL[key]), 88)
        for key, limit in (('nickname', 220), ('signature', 280)):
            self.assertLessEqual(self.win.friends_table.columnWidth(self.ui._FRIEND_COL[key]), limit)
        self.assertEqual(signature, self.win.friends_table.item(0, self.ui._FRIEND_COL['signature']).toolTip())
        self.assertEqual(1, self.win.friends_table.rowCount())

    def test_manual_column_width_is_kept_on_reload(self):
        self.add_friend()
        self.win._load_friends_table()
        column = self.ui._FRIEND_COL['nickname']
        self.win.friends_table.setColumnWidth(column, 325)
        self.win._load_friends_table()
        self.assertEqual(325, self.win.friends_table.columnWidth(column))
        self.assertEqual('测试昵称', self.win.friends_table.item(0, column).toolTip())

    def test_refreshed_cells_keep_full_text_tooltips(self):
        self.add_friend()
        intervals = '10:00-10:20, 10:30-10:50, 11:00-11:30'
        with patch.object(self.win, '_today_ranges', return_value=intervals):
            self.win._load_friends_table()
            self.win._refresh_durations()
        item = self.win.friends_table.item(0, self.ui._FRIEND_COL['today_ranges'])
        self.assertEqual(intervals, item.text())
        self.assertEqual(intervals, item.toolTip())

    def test_wide_nickname_image_fits_its_column_and_keeps_rightmost_mark_visible(self):
        fid = self.add_friend()
        self.win.db._conn.execute('UPDATE friends SET nickname_pending=1 WHERE id=?', (fid,))
        with patch.object(self.win.db, 'get_nickname_image', return_value=nickname_image(wide=True)):
            self.win._load_friends_table()
        table = self.win.friends_table
        column = self.ui._FRIEND_COL['nickname']
        item = table.item(0, column)
        size = item.icon().actualSize(table.iconSize())
        self.assertLessEqual(size.width()+20, table.columnWidth(column))
        self.win.show()
        self.qt.processEvents()
        table.clearSelection()
        image = table.viewport().grab(table.visualItemRect(item)).toImage()
        blue_pixels = sum(image.pixelColor(x, y).red() < 50
                          and image.pixelColor(x, y).green() < 130
                          and image.pixelColor(x, y).blue() > 150
                          for x in range(image.width()) for y in range(image.height()))
        self.assertGreater(blue_pixels, 5)

    def test_previously_empty_column_expands_when_data_arrives(self):
        fid = self.add_friend()
        column = self.ui._FRIEND_COL['tags']
        self.win._load_friends_table()
        narrow = self.win.friends_table.columnWidth(column)
        self.win.db.update_friend_manual(fid, '333333333', '测试昵称', 60, '合成签名', tags='探索 / 联机 / 合成测试')
        self.win._load_friends_table()
        self.assertGreater(self.win.friends_table.columnWidth(column), narrow)
        self.assertLessEqual(self.win.friends_table.columnWidth(column), 140)

    def test_pending_image_compacts_list_without_writing_database(self):
        fid = self.add_friend()
        self.win.db._conn.execute('UPDATE friends SET nickname_pending=1 WHERE id=?', (fid,))
        before = self.win.db._conn.total_changes
        data = nickname_image()
        with patch.object(self.win.db, 'get_nickname_image', return_value=data):
            self.win._load_friends_table()
        item = self.win.friends_table.item(0, self.ui._FRIEND_COL['nickname'])
        self.assertFalse(item.icon().isNull())
        self.assertLess(self.win.friends_table.columnWidth(self.ui._FRIEND_COL['nickname']), 150)
        self.assertEqual(before, self.win.db._conn.total_changes)
        self.assertEqual(1, len(self.win.db.get_all_friends()))

    def test_edit_dialog_uses_trimmed_preview_and_keeps_capture_bytes(self):
        data = nickname_image()
        friend = dict(uid='333333333', nickname='', nickname_pending=True,
                      nickname_image=data, nickname_captures=[dict(image=data, observed_at='test')])
        dialog = self.ui.EditFriendDialog(friend)
        self.addCleanup(dialog.close)
        for name in ('nicknameReviewImage', 'nicknameHistoryImage'):
            image = dialog.findChild(QLabel, name).pixmap()
            self.assertLess(image.width(), 140)
        self.assertEqual(data, friend['nickname_captures'][0]['image'])


if __name__ == '__main__':
    unittest.main()

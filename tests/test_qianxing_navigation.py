"""HUD mode and navigation checks with isolated storage and mocked game surfaces."""
from tests import test_regressions as _baseline
import atexit
import logging
from contextlib import ExitStack
import unittest
from unittest.mock import patch

import numpy as np

from app import capture, friend_screen, input as game_input, ocr_engine, window
from app.database import Database
from app.scanner import Scanner


RECT = (0, 0, 1920, 1080)
CONFIG = _baseline.CONFIG
atexit.register(logging.shutdown)


def hint(text, x=76, y=150, width=72, height=28, confidence=.99):
    return _baseline.text_box(text, x=x, y=y, w=width, h=height, score=confidence)


class QianxingNavigationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in dict(ensure_friend_list=True, bring_to_front=False,
                               navigation_max_esc=3, friend_open_attempts=2,
                               navigation_wait_seconds=0, startup_delay_seconds=0,
                               focus_wait_seconds=0, ocr_confidence=.6).items():
            self.stack.enter_context(patch.object(CONFIG, key, value))
        self.stack.enter_context(patch('win32api.GetAsyncKeyState', return_value=0))
        self.stack.enter_context(patch.object(window, 'get_window_rect', return_value=RECT))
        self.stack.enter_context(patch.object(window, 'find_windows', return_value=[]))
        self.stack.enter_context(patch.object(window, 'is_foreground', return_value=True))
        self.stack.enter_context(patch.object(window, 'input_permission_error', return_value=None))
        self.frame = np.full((1080, 1920, 3), 40, dtype=np.uint8)
        self.shot = self.stack.enter_context(patch.object(capture, 'capture_region', return_value=self.frame))
        self.ocr = self.stack.enter_context(patch.object(ocr_engine, 'recognize', return_value=[]))
        self.keys = []
        self.press = self.stack.enter_context(patch.object(game_input, 'press_key',
                                                          side_effect=lambda key, **kw: self.keys.append(chr(key))))
        self.esc = self.stack.enter_context(patch.object(game_input, 'press_esc',
                                                        side_effect=lambda **kw: self.keys.append('ESC')))
        self.stack.enter_context(patch.object(game_input, 'click'))
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.scan = Scanner(self.db)
        self.scan.set_window(123)
        self.stack.enter_context(patch.object(self.scan, '_wait', return_value=True))

    def test_hud_enter_alone_is_normal_and_same_row_alt_n_is_qianxing(self):
        self.assertEqual(friend_screen.interface_mode_from_hud([hint('Enter')]), 'normal')
        self.assertEqual(friend_screen.interface_mode_from_hud([hint('Enter'), hint('Alt + N', x=250, width=90)]), 'qianxing')
        self.assertEqual(friend_screen.interface_mode_from_hud([
            hint('Enter'), hint('Alt', x=250, width=36), hint('+', x=288, width=14),
            hint('N', x=304, width=22)]), 'qianxing')

    def test_low_confidence_or_truncated_alt_never_becomes_normal(self):
        for text, confidence in [('Alt+N', .5), ('Alt', .99), ('Alt+', .99), ('A1t+N', .99), ('N', .99)]:
            with self.subTest(text=text, confidence=confidence):
                self.assertIsNone(friend_screen.interface_mode_from_hud([
                    hint('Enter'), hint(text, x=250, width=90, confidence=confidence)]))
        for items in ([hint('Enter', confidence=.8)], [hint('Enter聊天')],
                      [hint('Enter'), hint('Enter', x=240)], []):
            with self.subTest(items=items):
                self.assertIsNone(friend_screen.interface_mode_from_hud(items))

    def test_alt_n_on_another_row_does_not_trigger_qianxing(self):
        items = [hint('Enter'), hint('Alt+N', x=250, y=80, width=90)]
        self.assertNotEqual(friend_screen.interface_mode_from_hud(items), 'qianxing')
        self.assertIsNone(friend_screen.interface_mode_from_hud([
            hint('Enter'), hint('Alt', x=250, width=36), hint('+N', x=288, y=80, width=42)]))

    def test_outside_hud_crop_cannot_supply_enter_anchor(self):
        for x, y in [(750, 150), (-90, 150), (76, -35), (76, 240)]:
            with self.subTest(x=x, y=y):
                self.assertIsNone(friend_screen.interface_mode_from_hud([hint('Enter', x=x, y=y)]))

    def test_gameplay_mode_ocr_receives_only_bottom_left_crop(self):
        frame = self.frame.copy()
        frame[:300, 1000:1500] = 255
        x1, y1, x2, y2 = friend_screen.HUD_MODE_RECT
        frame[y1:y2, x1:x2] = 71
        self.ocr.return_value = [hint('Enter'), hint('Alt+N', x=250, width=90)]
        self.assertEqual(friend_screen.gameplay_interface_mode(frame), 'qianxing')
        observed = self.ocr.call_args.args[0]
        self.assertEqual(observed.shape, (216, 720, 3))
        self.assertTrue(np.all(observed == 71))
        self.ocr.return_value = []
        self.assertIsNone(friend_screen.gameplay_interface_mode(frame))
        self.ocr.reset_mock()
        self.assertIsNone(friend_screen.gameplay_interface_mode(np.zeros((600, 800, 3), np.uint8)))
        self.ocr.assert_not_called()

    def test_mode_requires_two_consecutive_reads(self):
        initial = self.frame.copy()
        with patch.object(friend_screen, 'gameplay_interface_mode',
                          side_effect=['normal', 'qianxing', 'qianxing']) as read:
            self.assertEqual(self.scan._read_interface_mode(initial, RECT), 'qianxing')
        self.assertIs(read.call_args_list[0].args[0], initial)
        self.assertEqual(read.call_count, 3)
        self.assertEqual(self.shot.call_count, 2)
        self.assertEqual(self.keys, [])

    def test_four_read_budget_includes_initial_frame_and_unknown_breaks_agreement(self):
        with patch.object(friend_screen, 'gameplay_interface_mode',
                          side_effect=['normal', None, 'normal', 'qianxing']) as read:
            self.assertIsNone(self.scan._read_interface_mode(self.frame, RECT))
        self.assertEqual(read.call_count, 4)
        self.assertEqual(self.shot.call_count, 3)

    def test_mode_read_stops_when_foreground_is_lost(self):
        with patch.object(friend_screen, 'gameplay_interface_mode', return_value='normal') as read, \
                patch.object(self.scan, '_wait_for_game_foreground', return_value=False):
            self.assertIsNone(self.scan._read_interface_mode(self.frame, RECT))
        read.assert_called_once()
        self.shot.assert_not_called()
        self.assertEqual(self.keys, [])

    def test_open_friend_list_returns_to_hud_confirms_mode_then_reopens(self):
        for mode in ('normal', 'qianxing'):
            with self.subTest(mode=mode):
                self.keys.clear()
                self.shot.reset_mock()
                self.scan.interface_mode = 'stale'
                with patch.object(friend_screen, 'is_friend_list', side_effect=[True, False, True]), \
                        patch.object(friend_screen, 'is_gameplay_hud', return_value=True), \
                        patch.object(friend_screen, 'gameplay_interface_mode', side_effect=[mode, mode]) as read:
                    self.assertTrue(self.scan._ensure_friend_list(RECT))
                self.assertEqual(self.scan.interface_mode, mode)
                self.assertEqual(self.keys, ['ESC', 'O'])
                self.assertEqual(read.call_count, 2)
                self.assertEqual(self.shot.call_count, 4)

    def test_unknown_mode_sends_no_o_and_never_scans_or_writes(self):
        with patch.object(friend_screen, 'is_friend_list', return_value=False), \
                patch.object(friend_screen, 'is_gameplay_hud', return_value=True), \
                patch.object(friend_screen, 'gameplay_interface_mode', return_value=None) as read, \
                patch.object(friend_screen, 'read_page') as page, \
                patch('app.fast_scan.FastScan.run') as fast, \
                patch.object(self.db, 'apply_verified_scan') as commit:
            result = self.scan.scan_once()
        self.assertFalse(result.complete)
        self.assertIsNone(self.scan.interface_mode)
        self.assertEqual(self.keys, [])
        self.assertEqual(read.call_count, 4)
        page.assert_not_called()
        fast.assert_not_called()
        commit.assert_not_called()
        self.assertEqual(self.db.get_all_friends(), [])

    def test_manual_full_client_binds_unique_window_before_keys(self):
        self.scan.set_manual_rect(RECT)
        with patch.object(window, 'find_client_window', return_value=456) as match, \
                patch.object(friend_screen, 'is_friend_list', side_effect=[False, True]), \
                patch.object(friend_screen, 'is_gameplay_hud', return_value=True), \
                patch.object(friend_screen, 'gameplay_interface_mode', side_effect=['qianxing', 'qianxing']):
            self.assertTrue(self.scan._ensure_friend_list(RECT))
        match.assert_called_once_with(RECT, CONFIG.window_title_keyword)
        self.assertEqual(self.scan._hwnd, 456)
        self.assertEqual(self.scan._manual_rect, RECT)
        self.assertEqual(self.keys, ['O'])
        self.assertEqual(self.press.call_args.kwargs['hwnd'], 456)

    def test_unmatched_manual_region_cannot_send_navigation_keys(self):
        self.scan.set_manual_rect(RECT)
        with patch.object(window, 'find_client_window', return_value=None), \
                patch.object(friend_screen, 'is_friend_list', return_value=False), \
                patch.object(friend_screen, 'gameplay_interface_mode') as mode:
            self.assertFalse(self.scan._ensure_friend_list(RECT))
        self.assertIsNone(self.scan._hwnd)
        self.assertEqual(self.keys, [])
        mode.assert_not_called()
        self.assertIn('无法唯一对应', self.scan._navigation_error)

    def test_find_client_window_requires_unique_near_exact_client_match(self):
        candidates = [(101, '原神', (0, 0, 1930, 1110)), (202, '原神', (300, 0, 2230, 1110))]
        with patch.object(window, 'find_windows', return_value=candidates), \
                patch.object(window, 'get_window_rect', side_effect=lambda hwnd: {
                    101: (2, 3, 1922, 1083), 202: (300, 0, 2220, 1080)}[hwnd]):
            self.assertEqual(window.find_client_window(RECT, '原神'), 101)
            self.assertIsNone(window.find_client_window((0, 0, 960, 1080), '原神'))
        with patch.object(window, 'find_windows', return_value=candidates), \
                patch.object(window, 'get_window_rect', return_value=RECT):
            self.assertIsNone(window.find_client_window(RECT, '原神'))
        with patch.object(window, 'find_windows') as find:
            self.assertIsNone(window.find_client_window(RECT, ''))
            find.assert_not_called()


if __name__ == '__main__':
    unittest.main()

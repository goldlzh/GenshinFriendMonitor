"""No real input: a foreground game can still have an unrelated mouse target."""
from tests import test_regressions as baseline

from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import win32api
import win32gui
from app import input as game_input, scroller, window


class PointTargetTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(win32gui, 'IsWindow', return_value=True))
        self.hit = self.stack.enter_context(patch.object(win32gui, 'WindowFromPoint', return_value=123))
        self.root = self.stack.enter_context(patch.object(win32gui, 'GetAncestor', side_effect=lambda h, flag: h))
        self.cursor = self.stack.enter_context(patch.object(win32api, 'GetCursorPos', return_value=(300,400)))

    def test_foreground_does_not_allow_noactivate_overlay(self):
        self.hit.return_value = 999
        self.assertIsNotNone(window.point_input_error(123, 300, 400))

    def test_game_child_is_allowed_but_owned_popup_root_is_rejected(self):
        self.hit.return_value = 456
        self.root.side_effect = lambda hwnd, flag: 123
        self.assertIsNone(window.point_input_error(123, 300, 400))
        self.root.side_effect = lambda hwnd, flag: hwnd
        self.assertIsNotNone(window.point_input_error(123, 300, 400))

    def test_actual_cursor_is_read_inside_target_check_and_native_errors_fail_closed(self):
        self.assertIsNone(window.point_input_error(123))
        self.hit.assert_called_once_with((300,400))
        self.hit.side_effect = RuntimeError('invalid native hit test')
        self.assertIsNotNone(window.point_input_error(123, 300, 400))

    def test_null_hit_and_closed_target_are_rejected(self):
        self.hit.return_value = 0
        self.assertIsNotNone(window.point_input_error(123,300,400))
        with patch.object(win32gui,'IsWindow',return_value=False):
            self.assertIsNotNone(window.point_input_error(123,300,400))

    def test_manual_region_is_explicit_and_does_not_query_a_native_target(self):
        self.assertIsNone(window.point_input_error(None,300,400))
        self.hit.assert_not_called()


class InputOcclusionTests(unittest.TestCase):
    RECT = (0,0,1920,1080)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(window,'is_foreground',return_value=True))
        self.stack.enter_context(patch.object(window,'input_permission_error',return_value=None))
        self.stack.enter_context(patch.object(window,'get_window_rect',return_value=self.RECT))
        self.input_guard = self.stack.enter_context(patch.object(game_input,'point_input_error',return_value=None,create=True))
        self.scroll_guard = self.stack.enter_context(patch.object(scroller,'point_input_error',return_value=None,create=True))
        self.move = self.stack.enter_context(patch.object(game_input,'move_mouse'))
        self.mouse = self.stack.enter_context(patch.object(game_input,'mouse_event'))
        self.native_move = self.stack.enter_context(patch.object(win32api,'SetCursorPos'))
        self.native_mouse = self.stack.enter_context(patch.object(win32api,'mouse_event'))
        self.stack.enter_context(patch.object(win32api,'GetCursorPos',return_value=(300,400)))
        self.scroll_move = self.stack.enter_context(patch.object(scroller,'_move_to',return_value=True))
        self.post = self.stack.enter_context(patch.object(scroller,'_post_wheel',return_value=True))
        self.stack.enter_context(patch.object(game_input.time,'sleep'))
        self.stack.enter_context(patch.object(baseline.CONFIG,'drag_hold_seconds',0))
        self.stack.enter_context(patch.object(baseline.CONFIG,'fast_settle_seconds',0))

    def test_click_rejects_overlay_before_cursor_or_button_input(self):
        self.input_guard.return_value = 'point belongs to overlay'
        self.assertFalse(game_input.click_client(self.RECT,(300,400),hwnd=123))
        self.move.assert_not_called()
        self.mouse.assert_not_called()

    def test_click_rechecks_actual_cursor_before_down_after_moving(self):
        self.input_guard.side_effect = [None,'overlay appeared']
        self.assertFalse(game_input.click_client(self.RECT,(300,400),hwnd=123))
        self.move.assert_called_once()
        self.mouse.assert_not_called()

    def test_move_only_rejects_covered_target(self):
        self.input_guard.return_value = 'overlay'
        self.assertFalse(game_input.move_client(self.RECT,(300,400),hwnd=123))
        self.move.assert_not_called()

    def test_drag_loses_hit_target_after_down_and_always_releases_with_retry(self):
        pressed = [False]
        releases = [0]
        def event(flag):
            if flag == 2:
                pressed[0] = True
            if flag == 4:
                releases[0] += 1
                if releases[0] == 1:
                    raise game_input.InputError('first release rejected')
        self.mouse.side_effect = event
        self.scroll_guard.side_effect = lambda *a: 'overlay appeared' if pressed[0] else None
        self.assertFalse(scroller.drag_client(self.RECT,(800,925),(800,200),hwnd=123))
        self.assertEqual([2,4,4],[c.args[0] for c in self.mouse.call_args_list])

    def test_drag_rejects_overlay_at_destination_before_moving_held_cursor(self):
        self.scroll_guard.side_effect = [None,None,None,'destination covered']
        self.assertFalse(scroller.drag_client(self.RECT,(800,925),(800,200),hwnd=123))
        self.assertEqual([2,4],[c.args[0] for c in self.mouse.call_args_list])
        self.move.assert_called_once_with(800,925)

    def test_wheel_does_not_fall_back_to_post_message_for_covered_point(self):
        self.scroll_guard.return_value = 'overlay'
        self.assertFalse(scroller.scroll_clicks(self.RECT,-1,hwnd=123))
        self.scroll_move.assert_not_called()
        self.native_mouse.assert_not_called()
        self.post.assert_not_called()

    def test_wheel_rechecks_hit_target_after_cursor_movement(self):
        self.scroll_guard.side_effect = [None,'overlay appeared']
        self.assertFalse(scroller.scroll_clicks(self.RECT,-1,hwnd=123))
        self.native_mouse.assert_not_called()
        self.post.assert_not_called()

    def test_safe_wheel_post_fallback_is_retained_for_cursor_movement_failure(self):
        self.scroll_move.return_value = False
        self.assertTrue(scroller.scroll_clicks(self.RECT,-1,hwnd=123))
        self.post.assert_called_once()
        self.native_mouse.assert_not_called()

    def test_legacy_click_and_drag_obey_same_target_guard(self):
        self.input_guard.return_value = self.scroll_guard.return_value = 'overlay'
        with self.assertRaises(game_input.InputError):
            game_input.click_window(123,300,400)
        self.assertFalse(scroller.drag(self.RECT,24,hwnd=123))
        self.native_move.assert_not_called()
        self.native_mouse.assert_not_called()

    def test_legacy_click_checks_actual_cursor_again_before_down(self):
        self.input_guard.side_effect = [None, 'overlay after movement']
        with self.assertRaises(game_input.InputError):
            game_input.click_window(123,300,400)
        self.native_move.assert_called_once_with((300,400))
        self.native_mouse.assert_not_called()

    def test_legacy_drag_releases_even_when_overlay_appears_while_pressed(self):
        pressed = [False]
        def event(flag,*args):
            if flag == 2:
                pressed[0] = True
        self.native_mouse.side_effect = event
        self.scroll_guard.side_effect = lambda *args: 'overlay' if pressed[0] else None
        self.assertFalse(scroller.drag(self.RECT,24,hwnd=123))
        self.assertEqual([2,4],[call.args[0] for call in self.native_mouse.call_args_list])

    def test_click_release_is_not_blocked_if_covered_while_pressed(self):
        states = []
        self.input_guard.side_effect = lambda *args: None if not states else 'overlay'
        def event(flag):
            states.append(flag)
        self.mouse.side_effect = event
        self.assertTrue(game_input.click_client(self.RECT,(300,400),hwnd=123))
        self.assertEqual([2,4],states)

    def test_wheel_send_failure_cannot_use_post_fallback_after_point_becomes_covered(self):
        self.scroll_guard.side_effect = [None,None,'overlay appeared']
        self.native_mouse.side_effect = RuntimeError('mouse wheel failed')
        self.assertFalse(scroller.scroll_clicks(self.RECT,-1,hwnd=123))
        self.post.assert_not_called()

    def test_legacy_drag_routes_hwnd_from_both_scroll_entrypoints(self):
        targets = []
        with patch.object(baseline.CONFIG,'scroll_mode','drag'), \
                patch.object(scroller,'drag',side_effect=lambda *a,**kw: targets.append(kw.get('hwnd')) or True):
            self.assertTrue(scroller.scroll_down(self.RECT,hwnd=123))
            self.assertTrue(scroller.scroll_to_top(self.RECT,times=1,hwnd=123))
        self.assertEqual([123,123],targets)

    def test_legacy_scanner_both_clicks_keep_hwnd_scope_for_existing_injections(self):
        from app.scanner import Scanner
        from app.database import Database
        from app import parser, ocr_engine
        db = Database(':memory:')
        self.addCleanup(db.close)
        scanner = Scanner(db)
        scanner.set_window(123)
        scanner._verify_account_frame = lambda image: None
        targets = []
        label = SimpleNamespace(text=baseline.CONFIG.view_profile_words[0],center=(300,400))
        entry = SimpleNamespace(click_x=200,click_y=300)
        with patch.object(game_input,'click',side_effect=lambda x,y:targets.append(game_input._target.hwnd)), \
                patch.object(game_input,'press_esc'), \
                patch.object(scanner,'_wait',return_value=True), \
                patch.object(baseline.capture,'capture_region',return_value=baseline.np.ones((2,2,3))), \
                patch.object(ocr_engine,'recognize',return_value=[label]), \
                patch.object(parser,'extract_uid_from_results',return_value='111111111'):
            self.assertEqual('111111111',scanner._read_uid_via_menu(entry,self.RECT))
        self.assertEqual([123,123],targets)

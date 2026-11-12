"""Synthetic native geometry and pixels; never captures or inputs a live game."""
from tests import test_regressions as _baseline

from contextlib import ExitStack
import os
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from app import capture, window


class CapturePreflightTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {'GFM_EXPERIMENTAL_WINDOW_CAPTURE': ''}))
        self.stack.enter_context(patch('win32gui.IsWindow', return_value=True))
        self.stack.enter_context(patch('win32gui.IsWindowVisible', return_value=True))
        self.stack.enter_context(patch('win32gui.IsIconic', return_value=False))
        self.stack.enter_context(patch('win32api.EnumDisplayMonitors', return_value=[(99, None, None)]))
        self.monitor = self.stack.enter_context(patch('win32api.GetMonitorInfo',
                                                    return_value={'Monitor': (0, 0, 3840, 2160)}))
        self.rect = self.stack.enter_context(patch.object(window, 'get_window_rect',
                                                        return_value=(1268, 639, 3828, 2079)))
        self.native = self.stack.enter_context(patch.object(capture, '_capture_window_frame',
                                                          return_value=np.full((2, 3, 3), 70, np.uint8)))
        self.desktop = self.stack.enter_context(patch('mss.mss'))
        self.desktop.return_value.__enter__.return_value.grab.return_value = np.full((2, 3, 4), 55, np.uint8)

    def test_default_selected_window_uses_desktop_pixels(self):
        image = capture.capture_window(123, 1268, 639, 3828, 2079)
        self.native.assert_not_called()
        self.desktop.return_value.__enter__.return_value.grab.assert_called_once_with(
            {'left': 1268, 'top': 639, 'width': 2560, 'height': 1440})
        self.assertEqual((2, 3, 3), image.shape)
        self.assertTrue(np.all(image == 55))

    def test_default_startup_does_not_construct_native_capture(self):
        with patch.object(capture, '_window_broker', None), \
                patch('app.window_capture.WindowCaptureBroker') as broker:
            self.assertIsNone(capture.initialize_window_capture(MagicMock()))
            broker.assert_not_called()

    def assert_rejected(self, rect, pattern):
        with self.assertRaisesRegex(RuntimeError, pattern):
            capture.capture_window(123, *rect)
        self.native.assert_not_called()
        self.desktop.assert_not_called()

    def test_known_failed_client_outside_right_and_bottom_is_rejected(self):
        rect = (1616, 892, 4176, 2332)
        self.rect.return_value = rect
        self.assert_rejected(rect, '完整.*屏幕|屏幕.*完整')

    def test_negative_coordinate_monitor_is_supported(self):
        self.monitor.return_value = {'Monitor': (-2560, 0, 0, 1440)}
        self.rect.return_value = (-2500, 20, -100, 1370)
        self.assertIsNotNone(capture.capture_window(123, *self.rect.return_value))
        self.native.assert_not_called()
        self.desktop.assert_called_once_with()

    def test_spanning_monitors_is_rejected_without_treating_desktop_as_one_rect(self):
        self.stack.enter_context(patch('win32api.EnumDisplayMonitors',
                                      return_value=[(99, None, None), (100, None, None)]))
        self.monitor.side_effect = lambda handle: {'Monitor':
            ((0, 0, 3840, 2160) if handle == 99 else (-2560, 0, 0, 1440))}
        self.rect.return_value = (-100, 20, 2300, 1370)
        self.assert_rejected(self.rect.return_value, '完整.*屏幕|屏幕.*完整')

    def test_moved_window_does_not_capture_old_rectangle(self):
        self.assert_rejected((1200, 600, 3760, 2040), '位置|大小')

    def test_minimized_window_does_not_capture(self):
        self.stack.enter_context(patch('win32gui.IsIconic', return_value=True))
        self.assert_rejected(self.rect.return_value, '最小化')

    def test_monitor_query_error_fails_closed(self):
        self.monitor.side_effect = OSError('synthetic query failure')
        self.assert_rejected(self.rect.return_value, '核实.*屏幕')

    def test_window_moving_during_desktop_grab_discards_pixels(self):
        self.rect.side_effect = [(1268, 639, 3828, 2079), (1200, 600, 3760, 2040)]
        with self.assertRaisesRegex(RuntimeError, '位置|大小'):
            capture.capture_window(123, 1268, 639, 3828, 2079)
        self.desktop.return_value.__enter__.return_value.grab.assert_called_once()

    def test_explicit_experiment_uses_native_and_never_falls_back(self):
        with patch.dict(os.environ, {'GFM_EXPERIMENTAL_WINDOW_CAPTURE': '1'}):
            self.native.return_value = None
            self.assertIsNone(capture.capture_window(123, *self.rect.return_value))
        self.native.assert_called_once_with(123, self.rect.return_value)
        self.desktop.assert_not_called()

    def test_offscreen_client_is_rejected_in_experiment_too(self):
        self.rect.return_value = (1616, 892, 4176, 2332)
        with patch.dict(os.environ, {'GFM_EXPERIMENTAL_WINDOW_CAPTURE': '1'}):
            self.assert_rejected(self.rect.return_value, '完整.*屏幕|屏幕.*完整')

    def test_experimental_capture_rechecks_geometry_after_native_request(self):
        self.rect.side_effect = [(1268, 639, 3828, 2079), (1200, 600, 3900, 2120)]
        with patch.dict(os.environ, {'GFM_EXPERIMENTAL_WINDOW_CAPTURE': '1'}), \
                self.assertRaisesRegex(RuntimeError, '位置|大小'):
            capture.capture_window(123, 1268, 639, 3828, 2079)
        self.native.assert_called_once()
        self.desktop.assert_not_called()


class CaptureFailureStopsTests(unittest.TestCase):
    setUp = _baseline.FastScanRegressions.setUp

    def test_capture_error_stops_continuous_and_periodic_tasks_without_roster_change(self):
        from app.config import CONFIG
        self.db.add_friend('111111111', 'synthetic-kept', 60, None, 'offline', _baseline.T0)
        before = [(r['id'], r['uid'], r['nickname']) for r in self.db.get_all_friends()]
        for frequency in (0, 2):
            with self.subTest(frequency=frequency):
                self.scan._stop.clear()
                results = []
                self.scan.on_result = results.append
                with patch.object(CONFIG, 'scan_frequency_minutes', frequency), \
                        patch.object(self.scan, '_startup_countdown', return_value=True), \
                        patch.object(self.scan, 'scan_once', side_effect=capture.CaptureError('synthetic screen boundary')) as scan, \
                        patch.object(self.scan, '_wait_next_round', return_value=False):
                    self.scan._run()
                scan.assert_called_once()
                self.assertTrue(self.scan._stop.is_set())
                self.assertEqual(1, len(results))
                self.assertFalse(results[0].complete)
                self.assertIn('synthetic screen boundary', results[0].message)
                self.assertEqual(before, [(r['id'], r['uid'], r['nickname']) for r in self.db.get_all_friends()])

    def test_fast_capture_error_requires_manual_correction_and_preserves_archive(self):
        run = self.fast.FastScan(self.scan)
        with patch.object(capture, 'capture_window', side_effect=capture.CaptureError('synthetic screen boundary')), \
                self.assertRaisesRegex((capture.CaptureError, self.fast.ScanIncomplete), 'synthetic screen boundary'):
            run.shot()
        self.assertTrue(self.scan._stop.is_set())
        self.assertEqual([], self.db.get_all_friends())


if __name__ == '__main__':
    unittest.main()

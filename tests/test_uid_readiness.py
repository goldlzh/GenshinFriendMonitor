"""Bounded readiness polling keeps contradictory UID evidence fail closed."""
from tests import test_regressions as baseline
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app import friend_screen, ocr_engine, profile_layout as profile, scan_workflow as flow
from app.fast_scan import ScanIncomplete
from app.config import CONFIG


class UidReadinessTests(unittest.TestCase):
    setUp = baseline.UidTraversalRegressions.setUp
    simulation = baseline.FastScanRegressions.simulation
    menu_case = baseline.UidMenuRecoveryRegressions.menu_case

    def test_ready_menu_and_profile_skip_former_fixed_waits_and_archive_only_book(self):
        scan, rect, frame, shots, close, click = self.menu_case()
        with patch.object(scan, 'profile_targets', return_value=[baseline.text_box('查看资料')]), \
             patch.object(scan, 'wait') as wait, \
             patch.object(CONFIG, 'menu_wait_seconds', .8), \
             patch.object(CONFIG, 'profile_wait_seconds', 1.2):
            uid = scan.read_uid(rect, frame, friend_screen.CardBand(116, 231), 1)
        self.assertEqual('123456789', uid)
        self.assertEqual(4, shots.call_count)  # Menu OCR, fresh target check, two UID frames.
        self.assertEqual([.15], [call.args[0] for call in wait.call_args_list])
        self.assertEqual(2, click.call_count)
        saved = cv2.imdecode(np.frombuffer(scan.profile_images[uid], np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual((721, 1196, 3), saved.shape)
        close.assert_called_once()

    def test_slow_menu_remains_pollable_without_a_second_avatar_click(self):
        scan, rect, frame, shots, close, click = self.menu_case()
        with patch.object(scan, 'profile_targets', side_effect=[[], [], [], [baseline.text_box('查看资料')]]), \
             patch.object(CONFIG, 'menu_wait_seconds', .8), patch.object(scan, 'wait'):
            self.assertEqual('123456789', scan.read_uid(rect, frame, friend_screen.CardBand(116, 231), 1))
        self.assertEqual(2, click.call_count)
        self.assertEqual(10, shots.call_count)  # Each of four menu reads gets a fresh check.

    def test_unchanged_menu_frames_skip_ocr_and_never_click_a_profile_button(self):
        scan, rect, frame, shots, close, click = self.menu_case()
        with patch.object(CONFIG, 'menu_wait_seconds', 0), patch.object(scan, 'wait'), \
             patch.object(profile, 'read_menu') as read:
            with self.assertRaises(ScanIncomplete):
                scan.read_uid(rect, frame, friend_screen.CardBand(116, 231), 1)
        read.assert_not_called()
        self.assertEqual(2, click.call_count)  # One guarded avatar retry only.
        self.assertEqual(12, shots.call_count)  # Six observations plus their fresh checks.
        close.assert_not_called()

    def test_profile_fade_requires_two_later_consecutive_readable_frames(self):
        scan, rect, frame, shots, close, click = self.menu_case()
        visible = profile.ProfileRead(True, '123456789', [])
        fading = profile.ProfileRead(False, None, [])
        with patch.object(scan, 'profile_targets', return_value=[baseline.text_box('查看资料')]), \
             patch.object(CONFIG, 'profile_wait_seconds', 1.2), patch.object(scan, 'wait'), \
             patch.object(profile, 'read', side_effect=[fading, visible, fading, visible, visible]) as read:
            self.assertEqual('123456789', scan.read_uid(rect, frame, friend_screen.CardBand(116, 231), 1))
        self.assertEqual(5, read.call_count)
        self.assertEqual(7, shots.call_count)

    def test_different_readable_uids_never_become_a_majority_vote(self):
        scan, rect, frame, shots, close, click = self.menu_case()
        with patch.object(scan, 'profile_targets', return_value=[baseline.text_box('查看资料')]), \
             patch.object(CONFIG, 'profile_wait_seconds', 1.2), patch.object(scan, 'wait'), \
             patch.object(profile, 'read', side_effect=[profile.ProfileRead(True, '123456789', []),
                 profile.ProfileRead(False, None, []), profile.ProfileRead(True, '223456789', []),
                 profile.ProfileRead(True, '223456789', [])]) as read:
            with self.assertRaises(ScanIncomplete):
                scan.read_uid(rect, frame, friend_screen.CardBand(116, 231), 1)
        self.assertEqual(3, read.call_count)
        self.assertEqual({}, scan.profile_images)
        close.assert_called_once()

    def test_never_ready_profile_has_bounded_attempts_and_no_archive(self):
        scan, rect, frame, shots, close, click = self.menu_case()
        with patch.object(scan, 'profile_targets', return_value=[baseline.text_box('查看资料')]), \
             patch.object(CONFIG, 'profile_wait_seconds', 0), patch.object(scan, 'wait'), \
             patch.object(profile, 'read', return_value=profile.ProfileRead(False, None, [])) as read:
            with self.assertRaises(ScanIncomplete):
                scan.read_uid(rect, frame, friend_screen.CardBand(116, 231), 1)
        self.assertEqual(3, read.call_count)
        self.assertEqual({}, scan.profile_images)
        self.assertEqual(2, len(list(scan.evidence.glob('uid-001-crop-*.png'))))

    def test_restoring_checks_immediately_and_retains_slow_recovery_budget(self):
        scan, rect, frame, _, _, _ = self.menu_case()
        with patch.object(scan, 'await_visible') as ready, patch.object(scan, 'wait') as wait, \
             patch.object(CONFIG, 'profile_close_seconds', .6), \
             patch.object(flow.game_input, 'press_esc', return_value=True) as escape:
            flow.UidPass.restore_list(scan, frame)
        wait.assert_not_called()
        escape.assert_called_once()
        self.assertEqual(1.2, ready.call_args.kwargs['timeout_seconds'])


class ProfileArchiveTests(unittest.TestCase):
    def test_calibrated_book_preserves_uid_showcase_and_bottom_button(self):
        frame = np.zeros((1080, 1920, 3), np.uint8)
        for x, y in ((576, 188), (1450, 243), (1370, 850), (1558, 890)):
            frame[y, x] = (35, 100, 230)
        book = profile.crop_profile_book(frame)
        self.assertEqual((721, 1196, 3), book.shape)
        self.assertEqual(4, np.count_nonzero(book[:, :, 0]))
        self.assertFalse(np.shares_memory(frame, book))
        np.testing.assert_array_equal(book, profile.crop_profile_book(book))

    def test_old_archive_requires_its_actual_profile_uid_before_cropping(self):
        source = cv2.imencode('.jpg', np.zeros((1080, 1920, 3), np.uint8))[1].tobytes()
        with patch.object(profile, 'read', return_value=profile.ProfileRead(True, '123456789', [])):
            result = profile.crop_profile_capture(source, '123456789')
            with self.assertRaises(ValueError):
                profile.crop_profile_capture(source, '223456789')
        self.assertEqual((721, 1196, 3), cv2.imdecode(np.frombuffer(result, np.uint8), cv2.IMREAD_COLOR).shape)
        with patch.object(profile, 'read') as read:
            self.assertEqual(result, profile.crop_profile_capture(result, '123456789'))
            read.assert_not_called()

    def test_unknown_or_invalid_archive_is_never_cropped(self):
        unknown = cv2.imencode('.jpg', np.zeros((300, 400, 3), np.uint8))[1].tobytes()
        for blob in (unknown, b'invalid image'):
            with self.subTest(size=len(blob)), self.assertRaises(ValueError):
                profile.crop_profile_capture(blob, '123456789')
        with self.assertRaises(ValueError):
            profile.crop_profile_book(np.zeros((600, 800, 3), np.uint8))

    def test_menu_detection_restores_full_frame_coordinates(self):
        frame = np.zeros((1080, 1920, 3), np.uint8)
        found = ocr_engine.OcrResult('查看资料', .99, [[10, 20], [110, 20], [110, 50], [10, 50]])
        with patch.object(ocr_engine, 'recognize', return_value=[found]) as recognize:
            output = profile.read_menu(frame)
        self.assertEqual((860, 448, 3), recognize.call_args.args[0].shape)
        self.assertEqual([[242, 120], [342, 120], [342, 150], [242, 150]], output[0].box)


if __name__ == '__main__':
    unittest.main()

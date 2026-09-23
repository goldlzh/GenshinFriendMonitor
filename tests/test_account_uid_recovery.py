"""Synthetic HUD recovery never uses a saved account to guess unread digits."""
from tests import test_regressions as _baseline

import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from app import account_identity as identity


OWNER, OTHER = '123456789', '987654321'


def hud_frame():
    image = np.full((1080, 1920, 3), 90, np.uint8)
    cv2.putText(image, OWNER, (1737, 1071), cv2.FONT_HERSHEY_SIMPLEX,
                .65, (255, 255, 255), 1, cv2.LINE_AA)
    return image


class AccountUidRecoveryTests(unittest.TestCase):
    def test_clear_labelled_read_keeps_single_fast_ocr(self):
        with patch.object(identity.ocr_engine, 'recognize_lines', return_value=[('UID: '+OWNER, .99)]) as read:
            self.assertEqual(identity.read_account_uid(hud_frame()), OWNER)
        read.assert_called_once()

    def test_missing_label_recovers_only_two_matching_numeric_crops(self):
        image = hud_frame()
        with patch.object(identity.ocr_engine, 'recognize_lines', side_effect=[
                [('U1D: '+OWNER, .99)], [(OWNER, .99), (OWNER, .99)]]) as read:
            self.assertEqual(identity.read_account_uid(image), OWNER)
        self.assertEqual(read.call_count, 2)
        color, inverse = read.call_args.args[0]
        x1, y1, x2, y2 = identity.ACCOUNT_UID_DIGITS_RECT
        self.assertTrue(np.array_equal(color, image[y1:y2, x1:x2]))
        self.assertTrue(np.array_equal(inverse[:, :, 0], 255-cv2.cvtColor(color, cv2.COLOR_RGB2GRAY)))
        self.assertEqual((31, 137, 3), color.shape)

    def test_ambiguous_incomplete_or_low_confidence_fallback_stays_unreadable(self):
        for reads in ([(OWNER, .99), (OTHER, .99)], [(OWNER, .99), (OWNER, .89)],
                      [(OWNER[:-1], .99)]*2, [('12345678O', .99)]*2,
                      [(OWNER+'!', .99)]*2, [(OWNER, float('nan'))]*2,
                      [(OWNER, .99)]):
            with self.subTest(reads=reads), patch.object(identity.ocr_engine, 'recognize_lines',
                    side_effect=[[(OWNER, .99)], reads]):
                self.assertIsNone(identity.read_account_uid(hud_frame()))

    def test_covered_hud_does_not_trigger_numeric_hallucination(self):
        with patch.object(identity.ocr_engine, 'recognize_lines', return_value=[('', .0)]) as read:
            self.assertIsNone(identity.read_account_uid(np.full((1080, 1920, 3), 100, np.uint8)))
        read.assert_called_once()

    def test_expected_account_does_not_replace_a_reliable_different_read(self):
        with patch.object(identity.ocr_engine, 'recognize_lines', return_value=[('UID '+OTHER, .99)]) as read:
            with self.assertRaisesRegex(identity.AccountIdentityError, '不符'):
                identity.verify_account_frame(hud_frame(), OWNER)
        read.assert_called_once()

    def test_recovered_different_digits_also_reject_immediately(self):
        with patch.object(identity.ocr_engine, 'recognize_lines', side_effect=[
                [('', .0)], [(OTHER, .99), (OTHER, .99), ('UID: '+OTHER, .99)]]):
            with self.assertRaisesRegex(identity.AccountIdentityError, '不符'):
                identity.verify_account_frame(hud_frame(), OWNER)

    def test_single_frame_verification_recovers_original_frame_without_recapture(self):
        with patch.object(identity.ocr_engine, 'recognize_lines', side_effect=[
                [('UID', .99)], [(OWNER, .99), (OWNER, .99), ('UID: '+OWNER, .99)]]), \
                patch.object(identity.capture, 'capture_region') as capture:
            self.assertEqual(identity.verify_account_frame(hud_frame(), OWNER), OWNER)
        capture.assert_not_called()

    def test_full_line_and_numeric_fallback_conflict_is_never_overridden(self):
        for primary, fallback in (
                ((OTHER, .99), [(OWNER, .99), (OWNER, .99)]),
                (('U1D: '+OTHER, .99), [(OWNER, .99), (OWNER, .99)]),
                (('', .0), [(OWNER, .99), (OWNER, .99), ('UID: '+OTHER, .99)])):
            with self.subTest(primary=primary), patch.object(identity.ocr_engine, 'recognize_lines',
                    side_effect=[[primary], fallback]):
                self.assertIsNone(identity.read_account_uid(hud_frame()))

    def test_ten_digits_cannot_be_truncated_to_the_middle_nine(self):
        for text in ('UID: 1234567890', '1123456789', '123 456 7890', 'UID １２３４５６７８９０'):
            with self.subTest(text=text), patch.object(identity.ocr_engine, 'recognize_lines',
                    side_effect=[[(text, .99)], [(OWNER, .99), (OWNER, .99)]]) as read:
                self.assertIsNone(identity.read_account_uid(hud_frame()))
            read.assert_called_once()

    def test_unknown_primary_requires_complete_full_width_fallback_witness(self):
        for full_text in ('UID 1234567890', 'UID 12345678', '', 'UID 011111111'):
            with self.subTest(full_text=full_text), patch.object(identity.ocr_engine, 'recognize_lines',
                    side_effect=[[('', .0)], [(OWNER, .99), (OWNER, .99), (full_text, .99)]]):
                self.assertIsNone(identity.read_account_uid(hud_frame()))

    def test_damaged_primary_needs_both_numeric_crops_and_full_width_agreement(self):
        with patch.object(identity.ocr_engine, 'recognize_lines', side_effect=[
                [('UID 12345678', .75)], [(OWNER, .99), (OWNER, .99), ('UID: '+OWNER, .99)]]) as read:
            self.assertEqual(identity.read_account_uid(hud_frame()), OWNER)
        self.assertEqual(len(read.call_args.args[0]), 3)
        self.assertEqual(read.call_args.args[0][2].shape, (44, 298, 3))

    def test_confirm_existing_account_can_skip_initial_unreadable_frame(self):
        capture = MagicMock(return_value=hud_frame())
        with patch.object(identity, 'read_account_uid', side_effect=[None, OWNER, OWNER]), \
                patch.object(identity.time, 'sleep'):
            self.assertEqual(identity.read_current_account((0, 0, 1920, 1080), capture,
                                                          expected_uid=OWNER), OWNER)
        self.assertEqual(capture.call_count, 3)

    def test_unreadable_middle_resets_consecutive_confirmation(self):
        for values, succeeds in (([OWNER, None, OWNER, OWNER], True),
                                 ([OWNER, None, OWNER, None], False)):
            capture = MagicMock(return_value=hud_frame())
            with self.subTest(values=values), patch.object(identity, 'read_account_uid', side_effect=values), \
                    patch.object(identity.time, 'sleep'):
                if succeeds:
                    self.assertEqual(identity.read_current_account((0, 0, 1920, 1080), capture,
                                                                  expected_uid=OWNER), OWNER)
                else:
                    with self.assertRaises(identity.AccountIdentityError):
                        identity.read_current_account((0, 0, 1920, 1080), capture, expected_uid=OWNER)
            self.assertEqual(capture.call_count, 4)

    def test_reliable_other_account_is_not_outvoted_by_following_frames(self):
        capture = MagicMock(return_value=hud_frame())
        with patch.object(identity, 'read_account_uid', side_effect=[OTHER, OWNER, OWNER]), \
                patch.object(identity.time, 'sleep'):
            with self.assertRaisesRegex(identity.AccountIdentityError, '不符'):
                identity.read_current_account((0, 0, 1920, 1080), capture, expected_uid=OWNER)
        self.assertEqual(capture.call_count, 1)

    def test_stop_during_retry_wait_prevents_another_capture(self):
        capture = MagicMock(return_value=hud_frame())
        keep_scanning = MagicMock(side_effect=[True, True, False])
        with patch.object(identity, 'read_account_uid', return_value=None), patch.object(identity.time, 'sleep'):
            with self.assertRaisesRegex(identity.AccountIdentityError, '取消'):
                identity.read_current_account((0, 0, 1920, 1080), capture,
                                              expected_uid=OWNER, should_continue=keep_scanning)
        self.assertEqual(capture.call_count, 1)


if __name__ == '__main__':
    unittest.main()

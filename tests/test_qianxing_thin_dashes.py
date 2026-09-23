"""Real one-pixel Lv.-- strokes remain explicit unavailable-level evidence."""
from tests import test_regressions as _baseline
from tests.private_fixtures import private_fixture
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from app import fast_layout as layout, friend_screen, ocr_engine


class QianxingThinDashTests(unittest.TestCase):
    def setUp(self):
        with np.load(private_fixture('qianxing-thin-dashes.npz'),
                     allow_pickle=False) as data:
            self.images = data['images'].copy()

    def frame(self, index=2):
        band = friend_screen.CardBand(200, 315)
        frame = np.full((1080, 1920, 3), 180, np.uint8)
        frame[260:306, 280:395] = self.images[index]
        return frame, band

    def test_two_quick_captures_and_failed_uid_capture_confirm_two_real_dashes(self):
        for index in range(3):
            with self.subTest(capture=index):
                frame, band = self.frame(index)
                audit = layout.qianxing_level_region(frame, band)
                self.assertEqual('unset', audit['kind'])
                self.assertEqual(2, len(audit['dash_rects']))
                with patch.object(ocr_engine, 'recognize_lines') as ocr:
                    values, _ = layout.read_qianxing_levels(frame, [band])
                self.assertEqual([('--', 1.)], values)
                ocr.assert_not_called()
        self.assertEqual([(8, 1), (7, 1)], [tuple(r[2:]) for r in audit['dash_rects']])

    def test_thin_dashes_survive_small_brightness_and_pixel_position_changes(self):
        frame, band = self.frame()
        variants = [('brightness_%d' % delta,
                     np.clip(frame.astype(np.int16)+delta, 0, 255).astype(np.uint8))
                    for delta in (-5, 5)]
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            image = cv2.warpAffine(frame, np.float32([[1, 0, dx], [0, 1, dy]]),
                                  (1920, 1080), borderMode=cv2.BORDER_REPLICATE)
            variants.append(('shift_%d_%d' % (dx, dy), image))
        for name, image in variants:
            with self.subTest(variant=name):
                self.assertEqual('unset', layout.qianxing_level_region(image, band)['kind'])

    def test_missing_dash_or_prefix_or_misaligned_dash_remains_unreadable(self):
        for fault in ('missing_dash', 'missing_prefix', 'misaligned_dash', 'third_dash'):
            with self.subTest(fault=fault):
                frame, band = self.frame()
                if fault in ('missing_dash', 'misaligned_dash'):
                    frame[band.top+82:band.top+87, 351:360] = (30, 60, 220)
                    if fault == 'misaligned_dash':
                        frame[band.top+87:band.top+88, 352:359] = 245
                elif fault == 'missing_prefix':
                    frame[band.top+75:band.top+92, 318:341] = (30, 60, 220)
                else:
                    frame[band.top+82:band.top+87, 361:369] = (30, 60, 220)
                    frame[band.top+84:band.top+85, 362:368] = 245
                self.assertEqual('unreadable', layout.qianxing_level_region(frame, band)['kind'])
                with patch.object(ocr_engine, 'recognize_lines') as ocr:
                    values, _ = layout.read_qianxing_levels(frame, [band])
                self.assertEqual([('', 0.)], values)
                ocr.assert_not_called()

    def test_digits_on_real_blue_purple_gold_badges_do_not_become_unset(self):
        with np.load(private_fixture('qianxing-level-badges.npz'),
                     allow_pickle=False) as data:
            for image, value in zip(data['images'], data['values']):
                if value < 0:
                    continue
                with self.subTest(value=int(value)):
                    frame, band = self.frame()
                    frame[260:306, 280:395] = image
                    audit = layout.qianxing_level_region(frame, band)
                    self.assertEqual('digits', audit['kind'])
                    self.assertEqual(len(str(value)), audit['glyph_count'])


if __name__ == '__main__':
    unittest.main()

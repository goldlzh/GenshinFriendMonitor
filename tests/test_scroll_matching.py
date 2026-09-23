"""Offline motion regressions from real cards with repeated character avatars."""
from tests import test_regressions as _baseline
from tests.private_fixtures import private_fixture

import atexit
import logging
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app import fast_layout as layout
from app.friend_screen import CardBand

atexit.register(logging.shutdown)



def saved_frames(name):
    """Restore client coordinates without storing the account UID or title."""
    with np.load(private_fixture(name), allow_pickle=False) as saved:
        left, top, right, bottom = saved['rect'].tolist()
        frames = {}
        for key in saved.files:
            if not key.startswith('page_'):
                continue
            frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
            frame[top:bottom, left:right] = saved[key]
            frames[int(key[5:])] = frame
        return frames


class ScrollMatchingTests(unittest.TestCase):
    def test_saved_movement_is_recovered_when_scrollbar_is_unavailable(self):
        # These ordinary downward movements were misreported as 14 / 2 / 25px
        # by the viewport-fixed bottom strip when no thumb bound was available.
        cases = (
            ('scroll-083722.npz', 17, 18, 638),
            ('scroll-083916.npz', 16, 17, 625),
            ('scroll-083916.npz', 17, 18, 649),
        )
        for filename, first, last, actual_pixels in cases:
            with self.subTest(fixture=filename, before=first, after=last):
                frames = saved_frames(filename)
                self.assertEqual(6, len(layout.bands(frames[first], 'qianxing')))
                self.assertEqual(6, len(layout.bands(frames[last], 'qianxing')))
                pixels, score = layout.scroll_displacement(
                    frames[first], frames[last], expected=None, interface_mode='qianxing')
                self.assertAlmostEqual(actual_pixels, pixels, delta=2)
                self.assertGreaterEqual(score, .85)

    def synthetic_pair(self, *, duplicate_name=False):
        """One old card, a real shared card and an unrelated repeated avatar."""
        before = np.zeros((1080, 1920, 3), dtype=np.uint8)
        after = np.zeros_like(before)
        rng = np.random.default_rng(921)
        name = rng.integers(20, 235, (35, 120, 3), dtype=np.uint8)
        avatar = rng.integers(20, 235, (90, 74, 3), dtype=np.uint8)
        before[710:745, 270:390] = name
        before[710:800, 158:232] = avatar
        after[210:245, 270:390] = name
        # The correct person's avatar is slightly changed by the hover effect.
        # Another row has the exact original avatar and wins global matching.
        after[210:300, 158:232] = np.clip(avatar.astype(np.int16) + 2, 0, 255)
        after[460:550, 158:232] = avatar
        if duplicate_name:
            after[460:495, 270:390] = name
        return before, after

    def measure_synthetic_pair(self, before, after):
        with patch.object(layout, 'bands', return_value=[CardBand(700, 815)]), \
                patch.object(layout, 'nickname_rect', return_value=(270, 710, 390, 745)):
            return layout.scroll_displacement(before, after, expected=None)

    def test_unique_nickname_locates_person_despite_duplicate_avatar(self):
        before, after = self.synthetic_pair()
        # Prove that the fixture actually contains the regression trigger.
        a = cv2.cvtColor(before[710:800, 158:232], cv2.COLOR_RGB2GRAY)
        b = cv2.cvtColor(after[112:958, 148:242], cv2.COLOR_RGB2GRAY)
        _, _, _, point = cv2.minMaxLoc(cv2.matchTemplate(b, a, cv2.TM_CCOEFF_NORMED))
        self.assertEqual(250, 710 - 112 - point[1])
        pixels, score = self.measure_synthetic_pair(before, after)
        self.assertAlmostEqual(500, pixels, delta=2)
        self.assertGreaterEqual(score, .85)

    def test_two_nickname_peaks_are_not_resolved_only_by_matching_avatar(self):
        before, after = self.synthetic_pair(duplicate_name=True)
        after[460:550, 158:232] = np.random.default_rng(922).integers(
            20, 235, (90, 74, 3), dtype=np.uint8)
        # Both names match. Only one avatar matches, but a common character
        # portrait cannot establish which duplicate name is the shared friend.
        _, score = self.measure_synthetic_pair(before, after)
        self.assertLess(score, .85)


if __name__ == '__main__':
    unittest.main()

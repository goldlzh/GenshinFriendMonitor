"""Disambiguate repeated nickname/default-avatar rows using independent pixels.

The fixture keeps only the visible friend-list rectangle from a real failed
scroll. Its title and account HUD/UID are excluded. The expected 624 px motion
is independently visible in the pink namecard and signature of the shared row.
"""
from tests import test_regressions as _baseline
from tests.private_fixtures import private_fixture

import atexit
import logging
from pathlib import Path
import unittest

import numpy as np

from app import fast_layout as layout

atexit.register(logging.shutdown)


class DuplicateScrollTests(unittest.TestCase):
    def setUp(self):
        path = private_fixture('scroll-duplicate-leaf.npz')
        with np.load(path, allow_pickle=False) as stored:
            x0, y0, x1, y1 = map(int, stored['crop'])
            self.before = np.full((1080, 1920, 3), 60, dtype=np.uint8)
            self.after = self.before.copy()
            self.before[y0:y1, x0:x1] = stored['before']
            self.after[y0:y1, x0:x1] = stored['after']

    def measure(self, before, after):
        return layout.scroll_displacement(before, after, expected=None, interface_mode='qianxing')

    def assert_recovers_real_motion(self, before, after):
        pixels, confidence = self.measure(before, after)
        self.assertAlmostEqual(624., pixels, delta=2.)
        self.assertGreaterEqual(confidence, .85)

    def assert_rejects_ambiguous_motion(self, before, after):
        pixels, confidence = self.measure(before, after)
        self.assertFalse(abs(pixels) >= 20 and confidence >= .67,
                         'Unproven displacement accepted: pixels=%s confidence=%s' % (pixels, confidence))

    def without_bottom_fragments(self, image):
        result = image.copy()
        result[900:958, 136:1788] = 205
        return result

    def without_signatures(self, image):
        result = image.copy()
        for band in layout.bands(image, interface_mode='qianxing'):
            result[band.top + 70:band.top + 105, 914:1360] = 205
        return result

    def test_real_duplicate_nickname_and_avatar_pair_recovers_624_pixels(self):
        self.assert_recovers_real_motion(self.before, self.after)

    def test_unique_signature_recovers_motion_when_bottom_partial_row_is_hidden(self):
        self.assert_recovers_real_motion(self.without_bottom_fragments(self.before),
                                         self.without_bottom_fragments(self.after))

    def test_two_partial_text_fragments_recover_motion_without_any_signature(self):
        self.assert_recovers_real_motion(self.without_signatures(self.before),
                                         self.without_signatures(self.after))

    def test_signature_from_another_nickname_row_cannot_confirm_its_displacement(self):
        before = self.without_bottom_fragments(self.before)
        after = self.without_bottom_fragments(self.after)
        signature = before[780 + 70:780 + 105, 914:1360].copy()
        after = self.without_signatures(after)
        # The real shared row is at y=156. Plant its signature on a different row at
        # y=406 instead; its nickname must contradict the candidate at 374 px.
        after[406 + 70:406 + 105, 914:1360] = signature
        self.assert_rejects_ambiguous_motion(before, after)

    def test_matching_signature_and_nickname_still_require_the_same_avatar(self):
        before = self.without_bottom_fragments(self.before)
        after = self.without_bottom_fragments(self.after)
        after[156 + 10:156 + 100, 158:232] = np.random.default_rng(817).integers(
            0, 256, (90, 74, 3), dtype=np.uint8)
        self.assert_rejects_ambiguous_motion(before, after)

    def test_repeated_nickname_signature_avatar_and_namecard_cannot_determine_motion(self):
        tile = self.before[780:895, 136:1788].copy()

        def repeated(offset):
            frame = np.full((1080, 1920, 3), 60, dtype=np.uint8)
            for index in range(-1, 9):
                top = 156 + index * 125 - offset
                start, end = max(112, top), min(958, top + len(tile))
                if end > start:
                    frame[start:end, 136:1788] = tile[start - top:end - top]
            return frame

        # The row phase changes, but every whole-row translation is equally
        # plausible. This cannot be resolved by choosing a background peak.
        self.assert_rejects_ambiguous_motion(repeated(0), repeated(53))

    def test_blank_signature_with_duplicate_names_is_not_unique_identity_evidence(self):
        before = self.without_signatures(self.without_bottom_fragments(self.before))
        after = self.without_signatures(self.without_bottom_fragments(self.after))
        self.assert_rejects_ambiguous_motion(before, after)

    def test_nontext_signature_background_cannot_replace_signature_foreground(self):
        before = self.without_signatures(self.without_bottom_fragments(self.before))
        after = self.without_signatures(self.without_bottom_fragments(self.after))
        background = np.full((35, 446, 3), 205, dtype=np.uint8)
        # Lots of dark pixels and strong correlation, but a single wide block
        # has no independent text components and must not identify this row.
        background[8:28, 20:420] = 40
        before[850:885, 914:1360] = background
        after[226:261, 914:1360] = background
        self.assert_rejects_ambiguous_motion(before, after)

    def test_one_usable_lower_text_fragment_does_not_confirm_movement_alone(self):
        before = self.without_signatures(self.before)
        after = self.without_signatures(self.after)
        # Corrupt only the first fragment's extra top strip. The second crop
        # (916:944) remains readable, so it must not be accepted by itself.
        before[906:916, 275:830] = np.random.default_rng(209).integers(
            0, 256, (10, 555, 3), dtype=np.uint8)
        self.assert_rejects_ambiguous_motion(before, after)


if __name__ == '__main__':
    unittest.main()

"""Consecutive overlap survives viewport fade without hiding changed strokes."""
from tests import test_regressions as _baseline
from tests.private_fixtures import private_fixture

from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from app import fast_layout as layout, nickname_ocr as names
from app.friend_screen import CardBand
from app.parser import FriendEntry


FIXTURE = 'overlap-fade.npz'


class OverlapFadeTests(unittest.TestCase):
    def setUp(self):
        with np.load(private_fixture(FIXTURE), allow_pickle=False) as saved:
            self.images = {key: saved[key].copy() for key in saved.files}

    def observation(self, key, pending=False):
        top = 780 if key == 'faded' else 156
        entry = FriendEntry('' if pending else '示例乙', 'offline', '31天前', None,
                            194, top+57, .99, nickname_pending=pending,
                            interface_mode='qianxing', qianxing_level=None, tags='')
        return layout.CardObservation(CardBand(top, top+115), entry,
                                      self.images[key+'_name'].copy(),
                                      self.images[key+'_avatar'].copy())

    def test_real_complete_card_keeps_overlap_when_leaving_bottom_fade(self):
        for a_pending, b_pending in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(a_pending=a_pending, b_pending=b_pending):
                a, b = self.observation('faded', a_pending), self.observation('clear', b_pending)
                self.assertLess(layout.visual_similarity(a.name_image, b.name_image), .91)
                self.assertGreater(layout.nickname_ink_similarity(a.name_image, b.name_image), .99)
                self.assertGreater(layout.visual_similarity(a.avatar_image, b.avatar_image), .95)
                self.assertTrue(layout.same_person(a, b))
                self.assertTrue(layout.same_person(b, a))

    def test_added_or_deleted_stroke_is_rejected_despite_high_ink_score(self):
        a, b = self.observation('faded'), self.observation('clear', True)
        # An isolated two-by-two mark inside the glyph's existing bounds keeps
        # both its aspect ratio and global correlation, but is real new ink.
        b.name_image[47:49, 29:31] = 40
        self.assertLess(layout.visual_similarity(a.name_image, b.name_image), .91)
        self.assertGreater(layout.nickname_ink_similarity(a.name_image, b.name_image), .97)
        self.assertFalse(names.whole_nickname_foreground_agrees(a.name_image, b.name_image))
        self.assertFalse(layout.same_person(a, b))
        self.assertFalse(layout.same_person(b, a))

    def test_wrong_avatar_cannot_use_matching_nickname_ink(self):
        a, b = self.observation('faded'), self.observation('clear', True)
        b.avatar_image = np.full_like(b.avatar_image, 220)
        self.assertFalse(layout.same_person(a, b))

    def test_mode_level_and_tag_conflicts_still_reject_before_fallback(self):
        a = self.observation('faded')
        for changes in ({'interface_mode': 'normal'}, {'qianxing_level': 1}, {'tags': 'new tag'}):
            with self.subTest(changes=changes):
                b = self.observation('clear', True)
                b.entry = replace(b.entry, **changes)
                self.assertFalse(layout.same_person(a, b))

    def test_different_tag_images_still_reject_before_fallback(self):
        a, b = self.observation('faded'), self.observation('clear')
        a.tag_images = [a.name_image.copy()]
        b.tag_images = [np.full_like(a.name_image, 220)]
        self.assertFalse(layout.same_person(a, b))

    def test_original_raw_image_path_does_not_need_foreground_fallback(self):
        a, b = self.observation('clear'), self.observation('clear')
        with patch.object(layout, 'nickname_ink_similarity') as foreground:
            self.assertTrue(layout.same_person(a, b))
        foreground.assert_not_called()


if __name__ == '__main__':
    unittest.main()

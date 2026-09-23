"""Real variable-width tag crops must preserve complete nickname geometry."""
from tests import test_regressions as _baseline
from tests.private_fixtures import private_fixture
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app import fast_layout as layout, friend_screen, nickname_ocr, parser
from app.scan_workflow import UidPass


class QianxingHoverMatchingTests(unittest.TestCase):
    def setUp(self):
        with np.load(private_fixture('uid-qianxing-tag-hover.npz'),
                     allow_pickle=False) as data:
            self.images = {key: data[key].copy() for key in data.files}

    def visible(self, current_name, current_avatar, index=0):
        band = friend_screen.CardBand(298, 413)
        entry = parser.FriendEntry('样例甲', 'offline', '2天前', None, 194, 355, .99,
                                   interface_mode='qianxing', qianxing_level=30)
        reference = layout.CardObservation(band, entry,
                    self.images[f'before_name_{index}'], self.images[f'before_avatar_{index}'])
        with patch.object(layout, 'nickname_crop', return_value=current_name), \
             patch.object(layout, 'crop', return_value=current_avatar):
            return UidPass.visible_scores(None, [band], [reference], 'qianxing')

    def test_two_actual_tag_hover_failures_pass_without_stretching_the_name(self):
        for index in range(2):
            with self.subTest(index=index):
                before, after = [self.images[f'{side}_name_{index}'] for side in ('before', 'after')]
                self.assertEqual((before.shape[1], after.shape[1]), (88, 94))
                self.assertLess(layout.visual_similarity(after, before), .88)
                self.assertGreaterEqual(layout.nickname_ink_similarity(after, before), .95)
                self.assertGreaterEqual(layout.nickname_ink_similarity(before, after), .95)
                passed, rows = self.visible(after, self.images[f'after_avatar_{index}'], index)
                self.assertTrue(passed, rows)
                self.assertGreaterEqual(rows[0]['name_ink_similarity'], .95)

    def test_different_name_cannot_match_even_with_the_original_avatar(self):
        name = self.images['different_name']
        self.assertLess(layout.nickname_ink_similarity(name, self.images['before_name_0']), .95)
        passed, rows = self.visible(name, self.images['after_avatar_0'])
        self.assertFalse(passed, rows)

    def test_full_name_with_a_different_avatar_is_rejected(self):
        passed, rows = self.visible(self.images['after_name_0'], self.images['different_avatar'])
        self.assertFalse(passed, rows)
        self.assertLess(rows[0]['avatar_similarity'], .80)

    def test_remaining_fragment_cannot_stand_for_a_complete_name(self):
        fragment = self.images['after_name_0'][:, :48]
        self.assertEqual(layout.nickname_ink_similarity(fragment, self.images['before_name_0']), 0.)
        passed, rows = self.visible(fragment, self.images['after_avatar_0'])
        self.assertFalse(passed, rows)

    def test_occluded_middle_is_rejected_even_when_the_outer_ink_span_remains(self):
        name = self.images['after_name_0'].copy()
        original_rect = nickname_ocr.tight_rect(nickname_ocr.ink_mask(name))
        name[25:42, 31:61] = name[25:42, 86:87]
        self.assertEqual(nickname_ocr.tight_rect(nickname_ocr.ink_mask(name)), original_rect)
        self.assertLess(layout.nickname_ink_similarity(name, self.images['before_name_0']), .95)
        passed, rows = self.visible(name, self.images['after_avatar_0'])
        self.assertFalse(passed, rows)

    def test_padding_does_not_allow_a_large_horizontal_displacement(self):
        shifted = np.pad(self.images['after_name_0'], ((0, 0), (16, 0), (0, 0)), mode='edge')
        self.assertEqual(layout.nickname_ink_similarity(shifted, self.images['before_name_0']), 0.)
        passed, rows = self.visible(shifted, self.images['after_avatar_0'])
        self.assertFalse(passed, rows)

    def test_blank_image_cannot_use_foreground_matching(self):
        blank = np.full_like(self.images['after_name_0'], 180)
        self.assertEqual(layout.nickname_ink_similarity(blank, self.images['before_name_0']), 0.)
        passed, rows = self.visible(blank, self.images['after_avatar_0'])
        self.assertFalse(passed, rows)


if __name__ == '__main__':
    unittest.main()

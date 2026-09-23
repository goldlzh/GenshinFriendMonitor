"""Namecard artwork must not suppress real Qianxing labels or invent absence."""
from tests import test_regressions as _baseline
from tests.private_fixtures import private_fixture
from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app import fast_layout as layout, friend_screen, ocr_engine


class QianxingTagRegionTests(unittest.TestCase):
    band = friend_screen.CardBand(115, 230)

    def blank(self):
        return np.full((1080, 1920, 3), 220, np.uint8)

    def real_row(self, position):
        # Nine original name/tag strips only: no UID, title, or other rows.
        path = private_fixture('qianxing-tag-namecards.npz')
        with np.load(path) as saved:
            frame = self.blank()
            frame[120:177, 266:1440] = saved[f'row_{position:03d}']
        return frame

    def region(self, frame):
        return layout.card_field_regions(frame, self.band, 'qianxing')

    def test_real_labels_33_and_50_survive_distant_cyan_artwork(self):
        for position, left, right in ((33, 334, 783), (50, 365, 768)):
            with self.subTest(position=position):
                frame = self.real_row(position)
                name, tags, known = self.region(frame)
                self.assertTrue(known)
                self.assertEqual(tags, [(left, 140, right, 172)])
                self.assertEqual(name[2], left-5)
                with patch.object(ocr_engine, 'recognize_lines',
                                  return_value=[('聊天/探索', .99)]) as read:
                    self.assertEqual(layout.read_tags(frame, [(name, tags, known)], 'qianxing'),
                                     ['聊天/探索'])
                images = read.call_args.args[0]
                self.assertEqual(len(images), 1)
                np.testing.assert_array_equal(images[0], frame[140:172, left:right])

    def test_seven_real_unlabeled_namecards_are_confirmed_absent(self):
        with patch.object(ocr_engine, 'recognize_lines') as read:
            for position in (5, 11, 15, 38, 91, 98, 149):
                with self.subTest(position=position):
                    frame = self.real_row(position)
                    region = self.region(frame)
                    self.assertEqual(region, ((266, 120, 830, 177), [], True))
                    self.assertEqual(layout.read_tags(frame, [region], 'qianxing'), [''])
            read.assert_not_called()

    def test_uncertain_region_near_nickname_stays_unknown(self):
        frame = self.blank()
        frame[120:164, 500:540] = (75, 186, 224)
        name, tags, known = self.region(frame)
        self.assertFalse(known)
        self.assertEqual(tags, [])
        self.assertEqual(name[2], 495)

    def test_actual_pill_clipped_at_right_search_edge_stays_unknown(self):
        frame = self.blank()
        frame[140:172, 1320:1500] = (75, 186, 224)
        region = self.region(frame)
        self.assertFalse(region[2])
        self.assertEqual(layout.read_tags(frame, [region], 'qianxing'), [None])

    def test_nearby_vertically_truncated_label_stays_unknown(self):
        frame = self.blank()
        frame[106:137, 420:700] = (75, 186, 224)
        region = self.region(frame)
        self.assertFalse(region[2])
        self.assertEqual(region[0][2], 415)

    def test_faded_distant_pill_stays_unknown(self):
        frame = self.blank()
        frame[140:172, 1000:1300] = (66, 118, 136)
        region = self.region(frame)
        self.assertFalse(region[2])
        self.assertEqual(layout.read_tags(frame, [region], 'qianxing'), [None])

    def test_malformed_piece_adjoining_real_pill_stays_unknown(self):
        frame = self.blank()
        frame[140:172, 600:950] = (75, 186, 224)
        frame[130:176, 960:1000] = (75, 186, 224)
        region = self.region(frame)
        self.assertEqual(region[1], [(600, 140, 950, 172)])
        self.assertFalse(region[2])

    def test_low_confidence_real_label_stays_unknown_after_artwork_filter(self):
        frame = self.real_row(33)
        region = self.region(frame)
        with patch.object(ocr_engine, 'recognize_lines', return_value=[('unclear', .7)]):
            self.assertEqual(layout.read_tags(frame, [region], 'qianxing'), [None])


if __name__ == '__main__':
    unittest.main()

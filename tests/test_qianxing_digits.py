"""Original badge pixels prove numeric crops exclude Lv and badge ornaments."""
from tests import test_regressions as _baseline
from tests.private_fixtures import private_fixture
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app import fast_layout as layout, friend_screen, ocr_engine, parser


def field(text, left, top, width, height):
    return ocr_engine.OcrResult(text, .99,
                               [[left, top], [left+width, top], [left+width, top+height], [left, top+height]])


def fields(level, status='空闲中'):
    return [field('旅行者', 10, 14, 120, 38), field(level, 49, 65, 64, 38),
            field(status, 144, 64, 400, 41)]


class QianxingDigitTests(unittest.TestCase):
    def samples(self):
        with np.load(private_fixture('qianxing-level-badges.npz'), allow_pickle=False) as data:
            return data['images'].copy(), data['values'].tolist()

    @property
    def images(self):
        return self.samples()[0]

    @property
    def values(self):
        return self.samples()[1]

    def frame(self, index):
        frame = np.full((1080, 1920, 3), 210, np.uint8)
        band = friend_screen.CardBand(200, 315)
        frame[260:306, 280:395] = self.images[index]
        return frame, band

    def test_original_blue_purple_gold_and_hover_badges_have_digits_only_regions(self):
        for index, value in enumerate(self.values[:-1]):
            with self.subTest(value=value):
                frame, band = self.frame(index)
                region = layout.qianxing_level_region(frame, band)
                self.assertEqual(region['kind'], 'digits')
                self.assertEqual(region['glyph_count'], len(str(value)))
                left, top, right, bottom = region['rect']
                dx, dy, dw, dh = region['prefix_dot']
                self.assertGreaterEqual(left, dx+dw)
                self.assertLessEqual(right, 375)
                self.assertGreater(top, band.top+65)
                self.assertLess(bottom, band.top+100)

    def test_strict_digit_reads_agree_with_independent_glyph_count(self):
        for index, value in enumerate(self.values[:-1]):
            frame, band = self.frame(index)
            with patch.object(ocr_engine, 'recognize_lines', return_value=[(str(value), .99)]*2):
                values, audits = layout.read_qianxing_levels(frame, [band])
            self.assertEqual(values, [(str(value), .99)])
            self.assertEqual(audits[0]['glyph_count'], len(str(value)))

    def test_ornament_text_and_high_score_extra_digits_are_not_salvaged(self):
        frame, band = self.frame(1)  # True level 6, with its real right star still present.
        for reads in ([('6+', .999)]*2, [('Lv.6', .999)]*2, [('116', .999)]*2,
                      [('6', .999), ('8', .999)], [(' 6', .999)]*2,
                      [('6', .84), ('6', .999)]):
            with self.subTest(reads=reads), patch.object(ocr_engine, 'recognize_lines', return_value=reads):
                values, _ = layout.read_qianxing_levels(frame, [band])
            self.assertEqual(values, [('', 0.)])

    def test_matching_digit_count_alone_does_not_accept_non_digit_ornament(self):
        frame, band = self.frame(4)  # Three genuine glyph regions in the 116 reference.
        with patch.object(ocr_engine, 'recognize_lines', return_value=[('11+', .999)]*2):
            values, audit = layout.read_qianxing_levels(frame, [band])
        self.assertEqual(audit[0]['glyph_count'], 3)
        self.assertEqual(values, [('', 0.)])

    def test_actual_double_dash_is_known_unavailable_without_ocr(self):
        frame, band = self.frame(len(self.values)-1)
        with patch.object(ocr_engine, 'recognize_lines') as ocr:
            values, audits = layout.read_qianxing_levels(frame, [band])
        self.assertEqual(values, [('--', 1.)])
        self.assertEqual(audits[0]['kind'], 'unset')
        ocr.assert_not_called()
        entry = parser.parse_card(fields('--'), 115, 1, 2, interface_mode='qianxing',
                                  qianxing_level_absent=True)
        self.assertIsNone(entry.qianxing_level)
        self.assertEqual(entry.status, 'online')
        self.assertIsNone(parser.parse_card(fields('--'), 115, 1, 2, interface_mode='qianxing'))

    def test_weak_binary_view_needs_matching_high_score_rescaled_color_read(self):
        frame, band = self.frame(6)  # True level 22 from the failed page-007 read.
        with patch.object(ocr_engine, 'recognize_lines', side_effect=[
                [('22', .999), ('22', .831)], [('22', .999)]]) as ocr:
            values, audit = layout.read_qianxing_levels(frame, [band])
        self.assertEqual(values, [('22', .999)])
        self.assertEqual(audit[0]['method'], 'rescaled_color_confirmation')
        original_view = ocr.call_args_list[0].args[0][0]
        extra_view = ocr.call_args_list[1].args[0][0]
        self.assertNotEqual(original_view.shape, extra_view.shape)
        with patch.object(ocr_engine, 'recognize_lines', side_effect=[
                [('22', .838), ('22', 1.)], [('22', .999)]]) as ocr:
            values, audit = layout.read_qianxing_levels(frame, [band])
        self.assertEqual(values, [('22', .999)])
        self.assertEqual(ocr.call_count, 2)
        self.assertEqual(audit[0]['method'], 'rescaled_color_confirmation')
        for third in ('27', '22+'):
            with patch.object(ocr_engine, 'recognize_lines', side_effect=[
                    [('22', .999), ('22', .831)], [(third, .999)]]):
                values, _ = layout.read_qianxing_levels(frame, [band])
            self.assertEqual(values, [('', 0.)])
        with patch.object(ocr_engine, 'recognize_lines', return_value=[('22', .999), ('27', .831)]) as ocr:
            values, _ = layout.read_qianxing_levels(frame, [band])
        self.assertEqual(values, [('', 0.)])
        self.assertEqual(ocr.call_count, 1)
        for reads in ([('22', 1.1), ('22', .831)], [('22', .999), ('22', -.1)]):
            with patch.object(ocr_engine, 'recognize_lines', return_value=reads) as ocr:
                values, _ = layout.read_qianxing_levels(frame, [band])
            self.assertEqual(values, [('', 0.)])
            self.assertEqual(ocr.call_count, 1)

    def test_featureless_region_cannot_be_mistaken_for_unavailable_level(self):
        frame = np.full((1080, 1920, 3), 220, np.uint8)
        band = friend_screen.CardBand(200, 315)
        with patch.object(ocr_engine, 'recognize_lines') as ocr:
            values, audits = layout.read_qianxing_levels(frame, [band])
        self.assertEqual(values, [('', 0.)])
        self.assertEqual(audits[0]['kind'], 'unreadable')
        ocr.assert_not_called()

    def test_parser_never_extracts_qianxing_digits_from_mixed_strings(self):
        for text in ('Lv.6', 'Lv.6+', '200)', 'v.116)', '11+', '6+1', '6 / 8'):
            with self.subTest(text=text):
                self.assertIsNone(parser.parse_card(fields(text), 115, 1, 2, interface_mode='qianxing'))
        for text in ('0', '6', '34', '116', '200', '999'):
            entry = parser.parse_card(fields(text), 115, 1, 2, interface_mode='qianxing')
            self.assertEqual(entry.qianxing_level, int(text))
            self.assertIsNone(entry.level)
        entry = parser.parse_card(fields('Lv.60', '在线'), 115, 1, 2)
        self.assertEqual(entry.level, 60)
        self.assertIsNone(entry.qianxing_level)
        self.assertIsNone(parser.parse_card(fields('Lv.200', '在线'), 115, 1, 2))

    def test_top_109_requires_real_edges_and_bottom_partial_row_is_excluded(self):
        frame = np.full((1080, 1920, 3), 60, np.uint8)
        for top in (109, 234, 359, 484, 609, 734, 859):
            frame[top:top+115, 136:1788] = 220
        qianxing = layout.bands(frame, interface_mode='qianxing')
        self.assertEqual(len(qianxing), 6)
        self.assertLessEqual(abs(qianxing[0].top-109), 1)
        self.assertEqual(len(layout.bands(frame)), 5)
        frame[:114, 136:1788] = 220  # First card's top boundary is no longer visible.
        qianxing = layout.bands(frame, interface_mode='qianxing')
        self.assertTrue(all(b.top >= 112 for b in qianxing))
        self.assertEqual(len(qianxing), 5)


if __name__ == '__main__':
    unittest.main()

"""Signature isolation and image-only nickname behavior without game input."""
from tests import test_regressions as _baseline  # Initialize the shared isolated runtime first.
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from app import fast_layout as layout, friend_screen, nickname_ocr, ocr_engine, parser


def field(text, x, y, width, height, score=.99):
    return ocr_engine.OcrResult(text, score,
                               [[x, y], [x+width, y], [x+width, y+height], [x, y+height]])


def fields(name):
    return [field(name, 10, 14, 260, 38), field('Lv.60', 10, 71, 72, 23),
            field('在线', 120, 71, 150, 23)]


def name_image(text):
    image = np.full((60, 280, 3), 220, np.uint8)
    cv2.putText(image, text, (12, 42), cv2.FONT_HERSHEY_SIMPLEX, 1, (40, 40, 40), 2)
    return image


class SignatureFieldTests(unittest.TestCase):
    def test_ocr_adapter_preserves_original_whitespace_for_nickname_check(self):
        item = field(' A ', 10, 14, 260, 38)
        self.assertEqual((item.text, item.raw_text), ('A', ' A '))
        recognizer = MagicMock()
        recognizer.rec_image_shape = [3, 48, 160]
        recognizer.return_value = SimpleNamespace(txts=[' A '], scores=[.99])
        with patch.object(ocr_engine, 'get_engine', return_value=SimpleNamespace(text_rec=recognizer)):
            self.assertEqual(ocr_engine.recognize_lines([name_image('A')]), [(' A ', .99)])

    def test_entry_serialization_preserves_optional_signature(self):
        entry = parser.parse_card(fields('旅行者'), 115, 1, 2)
        self.assertIsNone(entry.to_dict()['signature'])
        entry.signature, entry.signature_confidence = '一起看星星', .97
        self.assertEqual(entry.to_dict()['signature'], '一起看星星')
        self.assertEqual(entry.to_dict()['signature_confidence'], .97)

    def test_separate_signature_batch_is_optional_and_cannot_supply_status(self):
        bands = [friend_screen.CardBand(116, 231), friend_screen.CardBand(241, 356)]
        frame = np.zeros((1080, 1920, 3), np.uint8)
        with patch.object(ocr_engine, 'recognize_lines', return_value=[('星星', .95), ('', .99)]*2) as read:
            self.assertEqual(layout.read_signatures(frame, bands), [('星星', .95), (None, 0.)])
        self.assertEqual(len(read.call_args.args[0]), 4)
        self.assertEqual(read.call_args.args[0][0].shape, (35, 776, 3))
        self.assertEqual(read.call_args.args[0][2].shape, (59, 776, 3))
        for values in ([('模糊', .89), (' ', 1.)]*2, [('错批次', .99)],
                       [('残\ufffd缺', .99), ('坏分数', float('nan'))]*2,
                       [('短句', .999), ('长句…', .99), ('短句末字', .999), ('长句…', .99)]):
            with self.subTest(values=values), patch.object(ocr_engine, 'recognize_lines', return_value=values):
                self.assertEqual(layout.read_signatures(frame, bands), [(None, 0.), (None, 0.)])
        with patch.object(ocr_engine, 'recognize_lines', side_effect=RuntimeError('optional OCR')):
            self.assertEqual(layout.read_signatures(frame, bands), [(None, 0.), (None, 0.)])

    def test_real_page_keeps_signature_separate_from_identity_fields(self):
        band = friend_screen.CardBand(116, 231)
        frame = np.zeros((1080, 1920, 3), np.uint8)
        values = [('好友/我的好友', .99), ('1/200', .99), ('旅行者', .99),
                  ('Lv.60', .99), ('今日内', .99)]
        with patch.object(layout, 'bands', return_value=[band]), \
             patch.object(nickname_ocr, 'refine', return_value=([('旅行者', .99)], [{}])), \
             patch.object(ocr_engine, 'recognize_lines', side_effect=[values, [('在线 Lv.99', .98)]*2]):
            page = layout.read_page(frame)
        self.assertTrue(page.reliable)
        self.assertEqual(len(page.entries), 1)
        entry = page.entries[0]
        self.assertEqual((entry.nickname, entry.level, entry.status), ('旅行者', 60, 'offline'))
        self.assertEqual((entry.signature, entry.signature_confidence), ('在线 Lv.99', .98))

    def test_blank_name_requires_real_card_and_valid_level_and_status(self):
        band = friend_screen.CardBand(116, 231)
        values = [('好友/我的好友', .99), ('1/200', .99), ('', 0.), ('Lv.60', .99), ('在线', .99)]
        for card_visible in (False, True):
            frame = np.full((1080, 1920, 3), 60, np.uint8)
            if card_visible:
                frame[116:231, 136:1788] = 220
            with self.subTest(card_visible=card_visible), \
                 patch.object(layout, 'bands', return_value=[band]), \
                 patch.object(nickname_ocr, 'refine', return_value=([('', 0.)], [{'method': 'rejected'}])), \
                 patch.object(ocr_engine, 'recognize_lines', side_effect=[values, [('', 0.)]*2]):
                page = layout.read_page(frame)
            self.assertEqual(len(page.entries), int(card_visible))
            if card_visible:
                self.assertTrue(page.entries[0].nickname_pending)
                self.assertEqual(page.entries[0].nickname, '')
        self.assertIsNone(parser.parse_card([field('Lv.99', 10, 71, 72, 23),
                                            field('在线', 120, 71, 150, 23)],
                                           115, 1, 2, allow_pending_name=True))

    def test_whitespace_or_unsupported_codepoints_are_image_only(self):
        for text in ('A B', ' A', 'A ', 'A\u3000B', '\t', '\u200b', 'A\ue001', 'A\ufffd', '\ufffc'):
            with self.subTest(text=repr(text)):
                entry = parser.parse_card(fields(text), 115, 1, 2)
                self.assertTrue(entry.nickname_pending)
                self.assertEqual(entry.nickname, '')
                with patch.object(nickname_ocr, 'get_matcher') as matcher, \
                     patch.object(ocr_engine, 'recognize_lines') as read:
                    values, audit = nickname_ocr.refine([name_image('A B')], [(text, .999)])
                self.assertEqual(values, [('', 0.)])
                self.assertTrue(audit[0]['image_only'])
                matcher.assert_not_called()
                read.assert_not_called()

    def test_high_score_does_not_override_whole_image_disagreement(self):
        matcher = MagicMock()
        matcher.line_score.return_value = 0.
        matcher.recognize.return_value = None
        with patch.object(nickname_ocr, 'reviewed_name', return_value=None), \
             patch.object(nickname_ocr, 'get_matcher', return_value=matcher), \
             patch.object(ocr_engine, 'recognize_lines', return_value=[('A', .999), ('A', .999)]):
            values, audit = nickname_ocr.refine([name_image('ABC')], [('A', .999)])
        self.assertEqual(values, [('', 0.)])
        self.assertEqual(audit[0]['method'], 'rejected')

    def test_large_internal_gap_keeps_image_without_flagging_regular_spacing(self):
        self.assertFalse(nickname_ocr.has_large_ink_gap(nickname_ocr.ink_mask(name_image('ABC'))))
        with patch.object(ocr_engine, 'recognize_lines') as read:
            values, audit = nickname_ocr.refine([name_image('A      B')], [('AB', .999)])
        self.assertEqual(values, [('', 0.)])
        self.assertTrue(audit[0]['image_only'])
        read.assert_not_called()

    def test_blank_pending_overlap_still_requires_avatar_and_level(self):
        band = friend_screen.CardBand(116, 231)
        blank = np.full((60, 280, 3), 220, np.uint8)
        avatar = np.random.default_rng(18).integers(0, 255, (95, 74, 3), dtype=np.uint8)
        a = layout.CardObservation(band, parser.parse_card(fields(' '), 115, 1, 2), blank, avatar)
        b = layout.CardObservation(band, parser.parse_card(fields(' '), 115, 1, 2), blank.copy(), avatar.copy())
        self.assertTrue(layout.same_person(a, b))
        b.entry.level = 59
        self.assertFalse(layout.same_person(a, b))
        b.entry.level = 60
        b.avatar_image = np.full_like(avatar, 200)
        self.assertFalse(layout.same_person(a, b))


if __name__ == '__main__':
    unittest.main()

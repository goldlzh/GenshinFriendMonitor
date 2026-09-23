"""Qianxing fields stay separate from ordinary adventure levels and names."""
from tests import test_regressions as _baseline
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app import fast_layout as layout, friend_screen, nickname_ocr, ocr_engine, parser


def box(text, x, y, width, height):
    return ocr_engine.OcrResult(text, .99,
                               [[x, y], [x+width, y], [x+width, y+height], [x, y+height]])


def fields(level=41, status='提瓦特冒险中', name='旅行者'):
    return [box(name, 10, 14, 240, 38), box(str(level), 49, 68, 64, 35),
            box(status, 144, 64, 400, 41)]


def labeled_frame(tag_left=366, tag_width=608):
    frame = np.full((1080, 1920, 3), 60, np.uint8)
    frame[115:230, 136:1788] = 220
    cv2.rectangle(frame, (tag_left, 140), (tag_left+tag_width-1, 171), (75, 186, 224), -1)
    cv2.putText(frame, 'TEST/TAG', (tag_left+10, 162), cv2.FONT_HERSHEY_SIMPLEX,
                .55, (255, 255, 255), 1)
    cv2.putText(frame, 'NAME', (280, 162), cv2.FONT_HERSHEY_SIMPLEX, .6, (40, 40, 50), 1)
    return frame


class QianxingFieldTests(unittest.TestCase):
    def test_custom_activity_requires_complete_quotes_and_matching_text_and_icon_color(self):
        band = friend_screen.CardBand(115, 230)
        title = '「自定义奇域」'
        for color in ((235, 130, 5), (140, 175, 40)):
            frame = np.full((1080, 1920, 3), 220, np.uint8)
            cv2.putText(frame, 'CUSTOM ACTIVITY', (415, band.top+89), cv2.FONT_HERSHEY_SIMPLEX,
                        .6, color, 2)
            cv2.circle(frame, (396, band.top+76), 4, color, -1)
            cv2.ellipse(frame, (396, band.top+87), (5, 7), 0, 0, 360, color, -1)
            evidence = layout.qianxing_activity_evidence(frame, band, title, .99)
            self.assertTrue(evidence['verified'])
            entry = parser.parse_card(fields(status=title), 115, 1, 2, interface_mode='qianxing',
                                      qianxing_activity_verified=evidence['verified'])
            self.assertEqual((entry.status, entry.location), ('online', '自定义奇域'))
            for incomplete in ('「自定义奇域', '自定义奇域」', '自定义奇域', '「」'):
                self.assertFalse(layout.qianxing_activity_evidence(frame, band, incomplete, .99)['verified'])
            self.assertFalse(layout.qianxing_activity_evidence(frame, band, title, .89)['verified'])
            without_icon = frame.copy()
            without_icon[band.top+67:band.top+98, 381:405] = 220
            self.assertFalse(layout.qianxing_activity_evidence(without_icon, band, title, .99)['verified'])
            gray = cv2.cvtColor(cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY), cv2.COLOR_GRAY2RGB)
            self.assertFalse(layout.qianxing_activity_evidence(gray, band, title, .99)['verified'])
        self.assertIsNone(parser.parse_card(fields(status=title), 115, 1, 2, interface_mode='qianxing'))
        self.assertIsNone(parser.parse_card(fields(status='未知文字'), 115, 1, 2, interface_mode='qianxing',
                                            qianxing_activity_verified=True))
        self.assertIsNone(parser.parse_card(fields(status='「不完整'), 115, 1, 2, interface_mode='qianxing',
                                            qianxing_activity_verified=True))

    def test_explicit_mode_keeps_two_level_types_separate(self):
        for mode in ('normal', 'qianxing'):
            entry = parser.parse_card(fields(), 115, 1, 2, interface_mode=mode)
            self.assertEqual(entry.interface_mode, mode)
            self.assertEqual(entry.level, 41 if mode == 'normal' else None)
            self.assertEqual(entry.qianxing_level, 41 if mode == 'qianxing' else None)
            self.assertIsNone(entry.tags)
            self.assertEqual(entry.to_dict()['interface_mode'], mode)
            self.assertIn('qianxing_level', entry.to_dict())
            self.assertIn('tags', entry.to_dict())
        self.assertIsNone(parser.parse_card(fields(), 115, 1, 2, interface_mode='guess'))

    def test_qianxing_level_bound_is_independent_of_adventure_limit(self):
        for level in (0, 1, 41, 61, 100, 999):
            with self.subTest(level=level):
                entry = parser.parse_card(fields(level), 115, 1, 2, interface_mode='qianxing')
                self.assertEqual(entry.qianxing_level, level)
                self.assertIsNone(entry.level)
        for level in (-1, 1000):
            self.assertIsNone(parser.parse_card(fields(level), 115, 1, 2, interface_mode='qianxing'))
        self.assertIsNone(parser.parse_card(fields(61), 115, 1, 2))
        self.assertIsNone(parser.parse_card(fields(0), 115, 1, 2))

    def test_team_and_last_login_states_require_complete_consistent_values(self):
        for state in ('2/8 组队中', '2／8组队中', '提瓦特冒险中'):
            entry = parser.parse_card(fields(status=state), 115, 1, 2, interface_mode='qianxing')
            self.assertEqual(entry.status, 'online')
        for state in ('0/8组队中', '9/8组队中', '2/0组队中', '2/8', '不明状态'):
            self.assertIsNone(parser.parse_card(fields(status=state), 115, 1, 2, interface_mode='qianxing'))
        self.assertIsNone(parser.parse_card(fields(status='2/8组队中'), 115, 1, 2))
        entry = parser.parse_card(fields(status='1天前最后登录'), 115, 1, 2, interface_mode='qianxing')
        self.assertEqual((entry.status, entry.last_seen), ('offline', '1天前'))

    def test_labels_move_with_name_and_are_never_in_name_crop(self):
        band = friend_screen.CardBand(115, 230)
        for left, width in ((366, 608), (570, 430)):
            frame = labeled_frame(left, width)
            name_rect, tag_rects, known = layout.card_field_regions(frame, band, 'qianxing')
            self.assertTrue(known)
            self.assertEqual(name_rect, (266, 120, left-5, 177))
            self.assertEqual(tag_rects, [(left, 140, left+width, 172)])
            self.assertEqual(layout.nickname_crop(frame, band, 'qianxing').shape[1], left-5-266)
            np.testing.assert_array_equal(layout.nickname_crop(frame, band, 'qianxing'),
                                          layout.crop(frame, name_rect))
            self.assertEqual(layout.nickname_rect(frame, band), layout.field_rect(band, layout.NAME))

    def test_missing_unreadable_and_absent_labels_remain_distinct(self):
        band = friend_screen.CardBand(115, 230)
        frame = labeled_frame()
        region = layout.card_field_regions(frame, band, 'qianxing')
        absent = (region[0], [], True)
        unknown = (region[0], [], False)
        with patch.object(ocr_engine, 'recognize_lines', return_value=[('聊天/探索', .99)]):
            self.assertEqual(layout.read_tags(frame, [region, absent, unknown], 'qianxing'),
                             ['聊天/探索', '', None])
        with patch.object(ocr_engine, 'recognize_lines', return_value=[('猜测', .5)]):
            self.assertEqual(layout.read_tags(frame, [region], 'qianxing'), [None])
        with patch.object(ocr_engine, 'recognize_lines', side_effect=RuntimeError('optional OCR')):
            self.assertEqual(layout.read_tags(frame, [region, absent], 'qianxing'), [None, ''])
        self.assertEqual(layout.read_tags(frame, [region, absent], 'normal'), [None, None])

    def test_faded_label_cannot_be_misreported_as_absent_or_mixed_into_name(self):
        frame = labeled_frame()
        frame[140:172, 366:974] = (66, 118, 136)
        cv2.putText(frame, 'FADED/TAG', (380, 162), cv2.FONT_HERSHEY_SIMPLEX, .55, (210, 210, 210), 1)
        band = friend_screen.CardBand(115, 230)
        name_rect, tags, known = layout.card_field_regions(frame, band, 'qianxing')
        self.assertFalse(known)
        self.assertEqual(tags, [])
        self.assertLess(name_rect[2], 366)
        self.assertEqual(layout.read_tags(frame, [(name_rect, tags, known)], 'qianxing'), [None])
        values = [('好友/我的好友', .99), ('1/200', .99), ('NAMEFADEDTAG', .99),
                  ('2/8组队中', .99)]
        with patch.object(layout, 'bands', return_value=[band]), \
             patch.object(layout, 'read_qianxing_levels', return_value=([('41', .99)], [{'kind': 'digits'}])), \
             patch.object(layout, 'read_signatures', return_value=[(None, 0.)]), \
             patch.object(nickname_ocr, 'refine', return_value=([('NAMEFADEDTAG', .99)], [{}])), \
             patch.object(ocr_engine, 'recognize_lines', return_value=values):
            page = layout.read_page(frame, interface_mode='qianxing')
        self.assertTrue(page.reliable)
        self.assertTrue(page.entries[0].nickname_pending)
        self.assertEqual(page.entries[0].nickname, '')
        self.assertIsNone(page.entries[0].tags)

    def test_page_passes_isolated_name_tag_and_qianxing_fields(self):
        frame = labeled_frame()
        band = friend_screen.CardBand(115, 230)
        values = [('好友/我的好友', .99), ('1/200', .99), ('旅行者', .99),
                  ('2/8组队中', .99)]
        with patch.object(layout, 'bands', return_value=[band]), \
             patch.object(layout, 'read_qianxing_levels', return_value=([('41', .99)], [{'kind': 'digits'}])), \
             patch.object(layout, 'read_signatures', return_value=[(None, 0.)]), \
             patch.object(nickname_ocr, 'refine', return_value=([('旅行者', .99)], [{}])), \
             patch.object(ocr_engine, 'recognize_lines', side_effect=[values, [('聊天/探索', .99)]]) as ocr:
            page = layout.read_page(frame, interface_mode='qianxing')
        self.assertTrue(page.reliable, page.issues)
        entry = page.entries[0]
        self.assertEqual((entry.nickname, entry.qianxing_level, entry.level, entry.tags),
                         ('旅行者', 41, None, '聊天/探索'))
        self.assertEqual(len(page.observations[0].tag_images), 1)
        self.assertEqual(page.observations[0].name_image.shape, (57, 95, 3))
        self.assertEqual(ocr.call_args_list[1].args[0][0].shape, (32, 608, 3))

    def test_same_person_uses_mode_level_and_separate_tag_images(self):
        rng = np.random.default_rng(101)
        name = rng.integers(0, 255, (57, 95, 3), dtype=np.uint8)
        avatar = rng.integers(0, 255, (95, 74, 3), dtype=np.uint8)
        tag = rng.integers(0, 255, (32, 608, 3), dtype=np.uint8)
        band = friend_screen.CardBand(115, 230)
        a = layout.CardObservation(band, parser.parse_card(fields(), 115, 1, 2, interface_mode='qianxing'),
                                   name, avatar, tag_images=[tag])
        b = layout.CardObservation(band, parser.parse_card(fields(), 115, 1, 2, interface_mode='qianxing'),
                                   name.copy(), avatar.copy(), tag_images=[tag.copy()])
        self.assertTrue(layout.same_person(a, b))
        b.entry.qianxing_level = 11
        self.assertFalse(layout.same_person(a, b))
        b.entry.qianxing_level = 41
        b.entry.interface_mode = 'normal'
        self.assertFalse(layout.same_person(a, b))
        b.entry.interface_mode = 'qianxing'
        b.tag_images = [np.full_like(tag, 200)]
        self.assertFalse(layout.same_person(a, b))


if __name__ == '__main__':
    unittest.main()

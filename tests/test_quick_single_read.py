"""Quick fields take one OCR pass and keep uncertainty without guessing."""
from tests import test_regressions as _baseline

from contextlib import ExitStack
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app import fast_layout as layout, nickname_ocr as names
from app.friend_screen import CardBand


class QuickSingleReadTests(unittest.TestCase):
    def setUp(self):
        self.band = CardBand(120, 235)
        self.frame = np.full((1080, 1920, 3), 220, np.uint8)
        cv2.putText(self.frame, 'ReviewName', (282, 162), cv2.FONT_HERSHEY_SIMPLEX,
                    .7, (35, 35, 35), 2, cv2.LINE_AA)

    def read(self, *, name=('ReviewName', .99), state=('在线', .99),
             level=('Lv.60', .99), signature=('a signature', .99),
             mode='normal', level_kind='digits', count=('1/200', .99),
             title=('好友 / 我的好友', .99), correction=None, region=None):
        values = [title, count, name, state]
        if mode == 'normal' or level_kind == 'digits':
            values.append(level)
        values.append(signature)
        with ExitStack() as stack:
            stack.enter_context(patch.object(layout, 'bands', return_value=[self.band]))
            if mode == 'qianxing':
                stack.enter_context(patch.object(layout, 'qianxing_level_region', return_value={
                    'kind': level_kind, 'rect': (339, 189, 364, 209), 'glyph_count': 2}))
                stack.enter_context(patch.object(layout, 'card_field_regions', return_value=
                    region or (layout.field_rect(self.band, layout.NAME), [], True)))
            stack.enter_context(patch.object(names, 'reviewed_name', return_value=correction))
            font = stack.enter_context(patch.object(names, 'get_matcher'))
            read = stack.enter_context(patch.object(layout.ocr_engine, 'recognize_lines', return_value=values))
            result = layout.read_page(self.frame, interface_mode=mode, quick=True)
        self.assertEqual(1, read.call_count)
        self.assertEqual(len(values), len(read.call_args.args[0]))
        font.assert_not_called()
        return result

    def test_high_confidence_name_and_optional_fields_are_read_once(self):
        page = self.read()
        self.assertTrue(page.confirmed)
        self.assertEqual(1, page.expected_total)
        self.assertEqual(1, len(page.entries))
        entry = page.entries[0]
        self.assertEqual(('ReviewName', 60, 'online', 'a signature'),
                         (entry.nickname, entry.level, entry.status, entry.signature))
        self.assertFalse(entry.nickname_pending)

    def test_weak_nickname_retains_image_and_entry_without_retry(self):
        page = self.read(name=('wrong guess', .4))
        obs = page.observations[0]
        self.assertIsNotNone(obs.entry)
        self.assertEqual('', obs.entry.nickname)
        self.assertTrue(obs.entry.nickname_pending)
        self.assertEqual('rejected', obs.name_audit['method'])
        self.assertGreater(obs.name_image.size, 0)
        self.assertNotIn('retries', obs.name_audit)

    def test_reviewed_image_can_supply_text_after_weak_first_read(self):
        page = self.read(name=('WrongOCR', .4), correction=('人工确认昵称', .99, 'review.png'))
        self.assertEqual('人工确认昵称', page.entries[0].nickname)
        self.assertFalse(page.entries[0].nickname_pending)
        self.assertEqual('local_review', page.observations[0].name_audit['method'])

    def test_unreadable_level_and_signature_do_not_discard_known_status(self):
        page = self.read(level=('Lv.6O', .99), signature=('uncertain', .7))
        entry = page.entries[0]
        self.assertIsNone(entry.level)
        self.assertIsNone(entry.signature)
        self.assertEqual('online', entry.status)

    def test_unrecognized_or_conflicting_status_stays_unknown(self):
        for state in (('正在加载', .99), ('在线', .1), ('在线 离线', .99), ('', 0.)):
            with self.subTest(state=state):
                entry = self.read(state=state).entries[0]
                self.assertEqual('unknown', entry.status)
                self.assertIsNone(entry.last_seen)
                self.assertIsNone(entry.location)

    def test_known_offline_state_keeps_its_last_seen(self):
        entry = self.read(state=('3天前最后登录', .99)).entries[0]
        self.assertEqual(('offline', '3天前'), (entry.status, entry.last_seen))

    def test_qianxing_digits_require_exact_count_and_strong_single_read(self):
        for value, expected in ((('25', .99), 25), (('25', .9), None),
                                (('5', .99), None), (('Lv.25', .99), None)):
            with self.subTest(value=value):
                page = self.read(mode='qianxing', level=value, state=('空闲中', .99))
                entry = page.entries[0]
                self.assertEqual(expected, entry.qianxing_level)
                self.assertIsNone(entry.level)
                self.assertEqual('online', entry.status)
                self.assertEqual([value], page.observations[0].level_audit['reads'])

    def test_qianxing_missing_or_unset_badge_remains_optional(self):
        for kind in ('unreadable', 'unset'):
            with self.subTest(kind=kind):
                page = self.read(mode='qianxing', level_kind=kind, state=('空闲中', .99))
                self.assertIsNone(page.entries[0].qianxing_level)
                self.assertEqual('online', page.entries[0].status)
                self.assertEqual([], page.observations[0].level_audit['reads'])

    def test_unconfirmed_label_boundary_keeps_pending_nickname(self):
        page = self.read(mode='qianxing', level=('25', .99), state=('空闲中', .99),
                         region=((266, 125, 400, 182), [], False))
        self.assertTrue(page.entries[0].nickname_pending)
        self.assertEqual('', page.entries[0].nickname)
        self.assertIsNone(page.entries[0].tags)

    def test_title_and_count_validation_are_not_relaxed(self):
        page = self.read(title=('其他界面', .99))
        self.assertFalse(page.confirmed)
        self.assertEqual([], page.entries)
        for value in (('152/151', .99), ('1/200', .1), ('unknown', .99)):
            with self.subTest(value=value):
                page = self.read(count=value)
                self.assertTrue(page.confirmed)
                self.assertIsNone(page.expected_total)

    def test_absent_complete_cards_are_not_synthesized_from_header(self):
        with patch.object(layout, 'bands', return_value=[]), \
             patch.object(layout.ocr_engine, 'recognize_lines', return_value=[
                 ('好友 / 我的好友', .99), ('1/200', .99)]) as read:
            page = layout.read_page(self.frame, quick=True)
        read.assert_called_once()
        self.assertEqual([], page.entries)
        self.assertIn('未检测到完整的好友横条', page.issues)

    def test_no_ink_and_no_actual_card_edges_cannot_create_pending_friend(self):
        self.frame[:] = 220
        with patch('app.friend_screen.detect_card_bands', return_value=[]):
            page = self.read(name=('', 0.))
        self.assertEqual([], page.entries)
        self.assertIsNone(page.observations[0].entry)


if __name__ == '__main__':
    unittest.main()

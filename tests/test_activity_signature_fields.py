"""Activity clocks require orange evidence; uncertain signatures retain pixels."""
from tests import test_regressions as _baseline
from tests.private_fixtures import private_fixture

from pathlib import Path
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app import fast_layout as layout, friend_screen, nickname_ocr, parser
from app.fast_scan import FastScan, _decode
from app.quick_scan import QuickIdentities


class ActivitySignatureFieldsTests(unittest.TestCase):
    def setUp(self):
        self.band = friend_screen.CardBand(120, 235)
        self.frame = np.full((1080, 1920, 3), 220, np.uint8)
        cv2.putText(self.frame, 'NAME', (280, 160), cv2.FONT_HERSHEY_SIMPLEX,
                    .7, (40, 40, 40), 2)

    def quick(self, state=('在线', .99), signature=('', 0.), mode='normal'):
        values = [('好友 / 我的好友', .99), ('1/200', .99), ('NAME', .99), state,
                  ('1' if mode == 'qianxing' else 'Lv.60', .99), signature]
        with patch.object(layout, 'bands', return_value=[self.band]), \
             patch.object(layout, 'card_field_regions', return_value=(layout.field_rect(self.band, layout.NAME), [], True)), \
             patch.object(layout, 'qianxing_level_region', return_value={
                 'kind': 'digits', 'rect': (340, 190, 363, 211), 'glyph_count': 1}), \
             patch.object(nickname_ocr, 'reviewed_name', return_value=None), \
             patch.object(layout.ocr_engine, 'recognize_lines', return_value=values) as read:
            page = layout.read_page(self.frame, interface_mode=mode, quick=True)
        read.assert_called_once()
        return page.observations[0]

    def activity(self, color):
        cv2.putText(self.frame, 'CUSTOM ACTIVITY', (415, self.band.top+89), cv2.FONT_HERSHEY_SIMPLEX,
                    .6, color, 2)
        cv2.circle(self.frame, (396, self.band.top+76), 4, color, -1)
        cv2.ellipse(self.frame, (396, self.band.top+87), (5, 7), 0, 0, 360, color, -1)

    def test_orange_world_and_editor_are_recorded_in_same_activity_fields(self):
        self.activity((235, 130, 5))
        for text, expected in (('「测试奇域2-可存档」', '测试奇域2-可存档'), ('编辑内容中', '编辑内容中')):
            with self.subTest(text=text):
                entry = self.quick(state=(text, .99), mode='qianxing').entry
                self.assertEqual(('online', expected, True),
                                 (entry.status, entry.qianxing_activity, entry.qianxing_activity_verified))
                self.assertTrue(entry.to_dict()['qianxing_activity_verified'])

    def test_green_activity_and_normal_interface_never_enable_activity_clock(self):
        self.activity((140, 175, 40))
        for mode, state in (('qianxing', '「自定义奇域」'), ('qianxing', '空闲中'),
                            ('qianxing', '编辑内容中'), ('normal', '千星奇域游玩中')):
            with self.subTest(mode=mode, state=state):
                entry = self.quick(state=(state, .99), mode=mode).entry
                self.assertEqual('online', entry.status)
                self.assertIsNone(entry.qianxing_activity)
                self.assertFalse(entry.qianxing_activity_verified)

    def test_unknown_incomplete_and_low_confidence_cannot_start_activity_clock(self):
        self.activity((235, 130, 5))
        for value in (('「不完整', .99), ('未知状态', .99), ('编辑内容中', .89)):
            with self.subTest(value=value):
                entry = self.quick(state=value, mode='qianxing').entry
                self.assertEqual('unknown', entry.status)
                self.assertIsNone(entry.qianxing_activity)
                self.assertFalse(entry.qianxing_activity_verified)

    def test_orange_text_without_matching_icon_is_not_activity_evidence(self):
        self.activity((235, 130, 5))
        self.frame[self.band.top+67:self.band.top+98, 381:405] = 220
        self.assertEqual('unknown', self.quick(state=('编辑内容中', .99), mode='qianxing').entry.status)

    def test_activity_outer_whitespace_is_ignored_without_changing_internal_text_or_gates(self):
        self.activity((235, 130, 5))
        for text, expected in ((' 编辑内容中 \t', '编辑内容中'), (' \t「模拟 城市  2」 ', '模拟 城市  2')):
            with self.subTest(text=text):
                entry = self.quick(state=(text, .99), mode='qianxing').entry
                self.assertEqual(expected, entry.qianxing_activity)
                self.assertTrue(entry.qianxing_activity_verified)
        self.assertFalse(self.quick(state=(' 编辑内容中 ', .89), mode='qianxing').entry.qianxing_activity_verified)
        self.assertIsNone(parser.qianxing_activity_name('编辑 内容中'))
        self.assertIsNone(parser.qianxing_activity_name(None))
        self.frame[self.band.top+67:self.band.top+98, 381:405] = 220
        self.assertFalse(self.quick(state=(' 编辑内容中 ', .99), mode='qianxing').entry.qianxing_activity_verified)

    def test_known_signature_preserves_exact_original_roi(self):
        self.frame[self.band.top+82:self.band.top+87, 934:964] = (91, 82, 74)
        roi = layout.crop(self.frame, layout.field_rect(self.band, layout.SIGNATURE))
        obs = self.quick(signature=('确定签名', .99))
        self.assertEqual('确定签名', obs.entry.signature)
        self.assertFalse(obs.entry.signature_pending)
        np.testing.assert_array_equal(roi, obs.signature_image)

    def test_uncertain_signature_keeps_pending_without_retry(self):
        for value in (('模糊', .7), ('被截断…', .99), ('坏\ufffd字符', .99)):
            with self.subTest(value=value):
                obs = self.quick(signature=value)
                self.assertIsNone(obs.entry.signature)
                self.assertTrue(obs.entry.signature_pending)
                self.assertEqual((35, 776, 3), obs.signature_image.shape)

    def test_only_visibly_empty_signature_is_not_pending(self):
        self.assertFalse(self.quick(signature=('', 0.)).entry.signature_pending)
        cv2.putText(self.frame, '??', (930, self.band.top+94), cv2.FONT_HERSHEY_SIMPLEX,
                    .6, (105, 91, 70), 1)
        self.assertTrue(self.quick(signature=('', 0.)).entry.signature_pending)

    def test_strict_page_also_keeps_failed_signature_pixels(self):
        cv2.putText(self.frame, '??', (930, self.band.top+94), cv2.FONT_HERSHEY_SIMPLEX,
                    .6, (105, 91, 70), 1)
        values = [('好友/我的好友', .99), ('1/200', .99), ('NAME', .99),
                  ('Lv.60', .99), ('在线', .99)]
        with patch.object(layout, 'bands', return_value=[self.band]), \
             patch.object(nickname_ocr, 'refine', return_value=([('NAME', .99)], [{}])), \
             patch.object(layout.ocr_engine, 'recognize_lines', side_effect=[values, [('模糊', .7)]*2]) as read:
            obs = layout.read_page(self.frame).observations[0]
        self.assertEqual(2, read.call_count)
        self.assertTrue(obs.entry.signature_pending)
        np.testing.assert_array_equal(layout.crop(self.frame, layout.field_rect(self.band, layout.SIGNATURE)),
                                      obs.signature_image)

    def test_quick_and_full_records_bridge_signature_png_and_pending(self):
        obs = self.quick(signature=('模糊', .7))
        quick_record = QuickIdentities([], []).resolve([(0, obs)])[0]
        db = SimpleNamespace(get_visual_profiles=lambda: [], find_by_nickname=lambda name: None,
                             find_friend_by_alias=lambda name: (None, None))
        scan = object.__new__(FastScan)
        scan.scanner, scan.slots, scan.expected = SimpleNamespace(db=db), {0: obs}, 1
        scan.check, scan.interface_mode = lambda: None, 'normal'
        for record in (quick_record, scan.prepare_records()[0]):
            self.assertTrue(record['entry'].signature_pending)
            np.testing.assert_array_equal(obs.signature_image, _decode(record['signature_image']))

    def test_real_attachment_orange_and_saved_green_status_crops(self):
        root = private_fixture('activity-status.json').parent
        metadata = json.loads((root/'activity-status.json').read_text(encoding='utf-8'))
        with np.load(root/'activity-status.npz') as images:
            for item in metadata['samples']:
                with self.subTest(sample=item['key']):
                    self.frame[:] = 220
                    cv2.putText(self.frame, 'NAME', (280, 160), cv2.FONT_HERSHEY_SIMPLEX,
                                .7, (40, 40, 40), 2)
                    self.frame[self.band.top+64:self.band.top+105, 381:830] = images[item['key']]
                    entry = self.quick(state=(item['text'], item['confidence']), mode='qianxing').entry
                    self.assertEqual(item['activity'], entry.qianxing_activity)
                    self.assertEqual(item['verified'], entry.qianxing_activity_verified)
                    self.assertEqual(item['status'], entry.status)


if __name__ == '__main__':
    unittest.main()

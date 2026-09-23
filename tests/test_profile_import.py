"""Synthetic profile layouts; public tests never contain personal screenshots."""
from tests import test_regressions as baseline

import unittest
from unittest.mock import patch

import numpy as np

from app.ocr_engine import OcrResult
from app.profile_import import parse_profile_fields, recognize_profile


def text(value, x, y, width=150, height=30, score=.99):
    return OcrResult(value, score, [[x,y],[x+width,y],[x+width,y+height],[x,y+height]])


def fields():
    return [text('UID123456789', 290, 40, 220), text('测试好友', 240, 250, 310, 60),
            text('冒险等阶', 100, 380, 120, 36), text('60', 650, 380, 50, 36),
            text('世界等级', 100, 435, 120, 36), text('9', 670, 435, 30, 36),
            text('第一行 保留空格', 100, 500, 400), text('第二行签名', 100, 545, 400),
            text('成就总数', 125, 645), text('1489', 650, 645, 60),
            text('角色展柜', 970, 55), text('Lv.95', 900, 300, 80),
            text('Lv.90', 900, 515, 80), text('UID987654321', 1320, 920, 220)]


class ProfileImportParsingTests(unittest.TestCase):
    def test_cropped_book_ignores_world_showcase_and_account_uid(self):
        result = parse_profile_fields(fields(), 1600)
        self.assertEqual(('123456789', '测试好友', 60, '第一行 保留空格\n第二行签名'),
                         (result.uid, result.nickname, result.level, result.signature))
        self.assertEqual((), result.missing)

    def test_full_game_frame_and_scaled_crops_use_anchor_coordinates(self):
        for scale, offset, width in ((.75, (360, 170), 1920), (.5, (0, 0), 800)):
            shifted = [OcrResult(item.text, item.confidence,
                        [[x*scale+offset[0], y*scale+offset[1]] for x,y in item.box]) for item in fields()]
            with self.subTest(scale=scale):
                result = parse_profile_fields(shifted, width)
                self.assertEqual(('123456789', '测试好友', 60), (result.uid, result.nickname, result.level))

    def test_split_uid_and_inline_adventure_level_are_supported(self):
        items = fields()
        items[0:1] = [text('UID', 290, 40, 55), text('123456789', 353, 40, 150)]
        items = [item for item in items if item.text != '60']
        next(item for item in items if item.text == '冒险等阶').text = '冒险等阶60'
        result = parse_profile_fields(items, 1600)
        self.assertEqual(('123456789', 60), (result.uid, result.level))

    def test_conflicting_uids_missing_labels_and_weak_fields_stay_unconfirmed(self):
        result = parse_profile_fields(fields()+[text('UID223456789', 290, 100, 220)], 1600)
        self.assertEqual('', result.uid)
        self.assertEqual('', result.nickname)
        self.assertEqual('', parse_profile_fields(fields()[4:], 1600).uid)
        items = fields()
        items[6].confidence = .65
        self.assertIsNone(parse_profile_fields(items, 1600).signature)

    def test_blank_signature_is_unknown_until_explicitly_confirmed(self):
        items = [item for item in fields() if not item.text.startswith(('第一行', '第二行'))]
        self.assertIsNone(parse_profile_fields(items, 1600).signature)

    def test_read_uses_local_ocr_and_rejects_invalid_images(self):
        with patch('app.profile_import.ocr_engine.recognize', return_value=fields()) as read:
            self.assertEqual('123456789', recognize_profile(np.zeros((960,1600,3),np.uint8)).uid)
            read.assert_called_once()
        for invalid in (None, np.zeros((10,10,3),np.uint8), np.zeros((100,100),np.uint8)):
            with self.assertRaises(ValueError):
                recognize_profile(invalid)


if __name__ == '__main__':
    unittest.main()

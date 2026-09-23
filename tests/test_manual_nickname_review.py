"""Reviewed nicknames survive rasterization without hiding real stroke changes."""
from tests import test_regressions as baseline
from tests.private_fixtures import private_fixture

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app import fast_layout as layout, nickname_ocr as names
from app.config import CONFIG


FIXTURE = 'manual-nickname-review.npz'


class ManualNicknameReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        config = patch.object(CONFIG, 'db_path', str(self.directory/'friends.db'))
        config.start()
        self.addCleanup(config.stop)
        self.records = json.loads(private_fixture(FIXTURE).with_suffix('.json').read_text(encoding='utf-8'))
        with np.load(private_fixture(FIXTURE), allow_pickle=False) as saved:
            self.images = {key: saved[key].copy() for key in saved.files}
        for row in self.records:
            if row['key'].startswith('reference_'):
                names.add_correction(self.images[row['key']], row['text'],
                                     'UID '+row['uid'], 'test-only manual review')

    def test_five_reviews_match_all_three_real_scans_including_overlaps(self):
        images = [self.images[row['key']] for row in self.records]
        outputs = [tuple(row.get('raw', ('wrong OCR', .5))) for row in self.records]
        # The original artificial output for a reference must not itself invoke
        # the separate whitespace/image-only policy.
        outputs = [('wrongOCR', .5) if row['key'].startswith('reference_') else value
                   for row, value in zip(self.records, outputs)]
        with patch.object(names, 'get_matcher', return_value=None), \
             patch.object(names.ocr_engine, 'recognize_lines') as retry:
            refined, audit = names.refine(images, outputs)
        self.assertEqual([row['text'] for row in self.records], [value[0] for value in refined])
        self.assertTrue(all(item['method'] == 'local_review' for item in audit))
        retry.assert_not_called()
        # Prove this fixture exercises the fallback, not only exact PNGs.
        fallback = 0
        for row in self.records:
            if 'reference' not in row:
                continue
            actual = names._feature(names.ink_mask(self.images[row['key']]))
            reference = names._feature(names.ink_mask(self.images[row['reference']]))
            if float(actual[0]@reference[0]) < .975:
                fallback += 1
        self.assertEqual(9, fallback)

    def test_real_sample_brightness_and_one_pixel_translation_keep_review(self):
        row = next(row for row in self.records if row.get('index') == 40)
        image = self.images[row['key']]
        for delta, dx, dy in ((-5, 0, 0), (5, 0, 0), (0, 1, 0), (0, 0, 1)):
            with self.subTest(delta=delta, dx=dx, dy=dy):
                changed = np.clip(image.astype(np.int16)+delta, 0, 255).astype(np.uint8)
                changed = cv2.warpAffine(changed, np.float32([[1, 0, dx], [0, 1, dy]]),
                                         (image.shape[1], image.shape[0]), borderMode=cv2.BORDER_REPLICATE)
                result = names.reviewed_name(names.ink_mask(changed), image=changed)
                self.assertIsNotNone(result)
                self.assertEqual(row['text'], result[0])

    def test_added_visible_mark_is_rejected_even_when_whole_score_is_high(self):
        image = self.images['reference_2'].copy()
        mask = names.ink_mask(image)
        x1, y1, x2, y2 = names.tight_rect(mask)
        # A new isolated 3x3 mark inside the existing ink bounds leaves the
        # aspect ratio unchanged, so shape/average-score gates alone cannot help.
        safe = cv2.dilate(mask, np.ones((9, 9), np.uint8))
        point = next((x, y) for y in range(y1+2, y2-4) for x in range(x1+2, x2-4)
                     if not np.any(safe[y:y+3, x:x+3]))
        changed = image.copy()
        x, y = point
        changed[y:y+3, x:x+3] = 40
        self.assertGreater(float(names._feature(mask)[0]@
                                 names._feature(names.ink_mask(changed))[0]), .975)
        self.assertGreater(layout.nickname_ink_similarity(image, changed), .975)
        self.assertFalse(names.whole_nickname_foreground_agrees(image, changed))
        self.assertIsNone(names.reviewed_name(names.ink_mask(changed), image=changed))

    def test_fragment_blank_and_different_name_do_not_reuse_review(self):
        image = self.images['reference_2']
        fragment = image[:, :image.shape[1]//2].copy()
        blank = np.full_like(image, 220)
        different = blank.copy()
        cv2.putText(different, 'ChangedName', (12, 38), cv2.FONT_HERSHEY_SIMPLEX,
                    .8, (35, 35, 35), 2, cv2.LINE_AA)
        for current in (fragment, blank, different):
            self.assertIsNone(names.reviewed_name(names.ink_mask(current), image=current))

    def test_conflicting_reviewed_texts_never_choose_the_highest_score(self):
        directory = self.directory/'nickname-corrections'
        record_path = next(directory.glob('*.json'))
        record = json.loads(record_path.read_text(encoding='utf-8'))
        image = cv2.cvtColor(cv2.imdecode(np.frombuffer((directory/record['image']).read_bytes(),
                                                      np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
        record['text'] = 'ContradictoryReview'
        (directory/'conflicting.json').write_text(json.dumps(record), encoding='utf-8')
        self.assertIsNone(names.reviewed_name(names.ink_mask(image), image=image))

    def test_existing_whitespace_and_large_gap_policy_stays_image_only(self):
        image = self.images['reference_2']
        for text in ('wrong name', '\ufffd', '\u200bname'):
            with self.subTest(text=text), patch.object(names, 'reviewed_name') as review:
                result, audit = names.refine([image], [(text, .99)])
                self.assertEqual([('', 0.)], result)
                self.assertTrue(audit[0]['image_only'])
                review.assert_not_called()
        with patch.object(names, 'has_large_ink_gap', return_value=True), \
             patch.object(names, 'reviewed_name') as review:
            self.assertEqual([('', 0.)], names.refine([image], [('name', .99)])[0])
            review.assert_not_called()


if __name__ == '__main__':
    unittest.main()

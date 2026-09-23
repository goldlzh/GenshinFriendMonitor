"""Geometry caching skips impossible names without weakening image identity."""
from tests import test_regressions as _baseline
from tests.private_fixtures import private_fixture

from pathlib import Path
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from app import fast_layout as layout, nickname_ocr as names
from app.fast_scan import _png
from app.friend_screen import CardBand
from app.parser import FriendEntry
from app.quick_scan import QuickIdentities


def old_geometry_gate(a, b):
    if a['rect'] is None or b['rect'] is None:
        return False
    (ax, ay, ar, ab), (bx, by, br, bb) = a['rect'], b['rect']
    aw, ah, bw, bh = ar-ax, ab-ay, br-bx, bb-by
    return not (min(a['area'], b['area']) < 16
                or not .90 <= aw/bw <= 1.11 or not .90 <= ah/bh <= 1.11
                or abs((ax+ar-bx-br)/2) > 10 or abs((ay+ab-by-bb)/2) > 4)


class NicknameGeometryCacheTests(unittest.TestCase):
    def setUp(self):
        self.clear = np.full((57, 564, 3), 220, np.uint8)
        cv2.putText(self.clear, 'SAMPLE', (12, 40), cv2.FONT_HERSHEY_SIMPLEX,
                    .8, (40, 40, 40), 2, cv2.LINE_AA)
        self.faded = np.clip(self.clear.astype(np.int16)-5, 0, 255).astype(np.uint8)

    def observation(self, pending=True):
        entry = FriendEntry('' if pending else 'saved', 'offline', '今日内', None,
                            194, 213, .99, nickname_pending=pending)
        return layout.CardObservation(CardBand(156, 271), entry, self.clear.copy(),
                                      np.zeros((95, 74, 3), np.uint8))

    def identities(self):
        different = np.full_like(self.clear, 220)
        cv2.putText(different, 'A very long nickname', (10, 38), cv2.FONT_HERSHEY_SIMPLEX,
                    .75, (30, 30, 30), 2, cv2.LINE_AA)
        rows = [{'id': 1, 'nickname': 'saved', 'uid': '111111111'},
                {'id': 2, 'nickname': 'other', 'uid': '222222222'}]
        profiles = [{'friend_id': 1, 'name_image': _png(self.clear)},
                    {'friend_id': 2, 'name_image': _png(different)}]
        return QuickIdentities(rows, profiles)

    def test_geometry_is_exact_existing_mask_metadata(self):
        for image in (self.faded, self.clear, np.full_like(self.clear, 220), None):
            geometry = layout.nickname_ink_geometry(image)
            mask = names.ink_mask(image)
            self.assertEqual(names.tight_rect(mask), geometry['rect'])
            self.assertEqual(int(np.count_nonzero(mask)), geometry['area'])

    def test_geometry_gate_matches_old_formula_at_all_boundaries(self):
        reference = {'rect': (20, 20, 120, 120), 'area': 100}
        cases = [{'rect': None, 'area': 0}]
        for width in (89, 90, 91, 100, 110, 111, 112):
            for height in (89, 90, 100, 111, 112):
                for dx, dy in ((0, 0), (10, 4), (10.5, 4), (10, 4.5), (-10, -4)):
                    cases.append({'rect': (70-width/2+dx, 70-height/2+dy,
                                            70+width/2+dx, 70+height/2+dy), 'area': 100})
        cases.extend({'rect': reference['rect'], 'area': area} for area in (0, 15, 16, 17))
        for value in cases:
            for a, b in ((reference, value), (value, reference)):
                self.assertEqual(old_geometry_gate(a, b), layout.compatible_nickname_geometry(a, b))

    def test_real_score_and_added_stroke_protection_are_unchanged(self):
        with np.load(private_fixture('overlap-fade.npz'), allow_pickle=False) as saved:
            self.faded, self.clear = saved['faded_name'].copy(), saved['clear_name'].copy()
        self.assertAlmostEqual(.9932904839515686,
                               layout.nickname_ink_similarity(self.faded, self.clear), places=7)
        changed = self.clear.copy()
        changed[47:49, 29:31] = 40
        self.assertTrue(layout.compatible_nickname_geometry(layout.nickname_ink_geometry(self.faded),
                                                            layout.nickname_ink_geometry(changed)))
        self.assertGreater(layout.nickname_ink_similarity(self.faded, changed), .985)
        self.assertFalse(names.whole_nickname_foreground_agrees(self.faded, changed))

    def test_unique_text_name_does_not_build_any_image_geometry(self):
        identities = self.identities()
        with patch.object(layout, 'nickname_ink_geometry', wraps=layout.nickname_ink_geometry) as geometry:
            result = identities.resolve([(0, self.observation(False))])
        self.assertEqual('nickname', result[0]['match_basis'])
        geometry.assert_not_called()
        self.assertEqual({}, identities.profile_geometry)

    def test_profile_geometry_is_lazy_shared_and_skips_impossible_pair(self):
        identities = self.identities()
        observations = [(0, self.observation()), (1, self.observation())]
        with patch.object(layout, 'nickname_ink_geometry', wraps=layout.nickname_ink_geometry) as geometry, \
             patch.object(layout, 'nickname_ink_similarity', return_value=1.) as similarity, \
             patch.object(names, 'whole_nickname_foreground_agrees', return_value=True) as whole:
            identities.resolve(observations[:1])
            self.assertEqual(3, geometry.call_count)  # One observation, two profiles.
            identities.resolve(observations)
            self.assertEqual(4, geometry.call_count)  # Only the new observation.
            identities.resolve(observations)
            self.assertEqual(4, geometry.call_count)
        self.assertEqual(2, similarity.call_count)  # Long-name profile is never compared.
        self.assertEqual(2, whole.call_count)

    def test_compatible_geometry_does_not_bypass_whole_stroke_guard(self):
        identities = self.identities()
        with patch.object(layout, 'nickname_ink_similarity', return_value=1.), \
             patch.object(names, 'whole_nickname_foreground_agrees', return_value=False) as whole:
            result = identities.resolve([(0, self.observation())])
        whole.assert_called_once()
        self.assertIsNone(result[0]['uid'])
        self.assertEqual('pending_nickname_image', result[0]['match_basis'])

    def test_blank_observation_skips_all_profile_geometry(self):
        identities = self.identities()
        observation = self.observation()
        observation.name_image[:] = 220
        with patch.object(layout, 'nickname_ink_geometry', wraps=layout.nickname_ink_geometry) as geometry:
            result = identities.resolve([(0, observation)])
        self.assertEqual(1, geometry.call_count)
        self.assertEqual({}, identities.profile_geometry)
        self.assertEqual('needs_remark', result[0]['match_basis'])


if __name__ == '__main__':
    unittest.main()

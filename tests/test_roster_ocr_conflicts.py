"""OCR disagreements are coverage evidence, never a spelling or UID guess."""
from tests import test_regressions as baseline
from tests.private_fixtures import private_fixture

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from app import fast_layout as layout, friend_screen, nickname_ocr, parser
from app import scan_workflow as flow


FIXTURE = 'roster-ocr-conflicts.npz'


def observation(index=133, capture=0, *, signature=False):
    key = 'row_%d_%d' % (index, capture)
    records = json.loads(private_fixture(FIXTURE).with_suffix('.json').read_text(encoding='utf-8'))
    row = next(item['row'] for item in records if item['key'] == key)
    entry = parser.FriendEntry(**row['entry'])
    if not signature:
        entry = replace(entry, signature=None, signature_confidence=0.)
    with np.load(private_fixture(FIXTURE), allow_pickle=False) as saved:
        return layout.CardObservation(friend_screen.CardBand(row['top'], row['bottom']), entry,
            saved[key+'_name'].copy(), saved[key+'_avatar'].copy(), fields=row['fields'])


class RosterOcrConflictTests(unittest.TestCase):
    def test_real_punctuation_and_width_variants_keep_both_original_images(self):
        for index in (128, 133):
            with self.subTest(position=index+1):
                a, b = observation(index, 0), observation(index, 1)
                variants = sorted([a.entry.nickname, b.entry.nickname])
                originals = (a.name_image.copy(), b.name_image.copy())
                self.assertGreaterEqual(layout.nickname_ink_similarity(*originals), .97)
                self.assertTrue(flow.roster_matches([a], [b]))
                for obs, original in zip((a, b), originals):
                    self.assertEqual('', obs.entry.nickname)
                    self.assertTrue(obs.entry.nickname_pending)
                    self.assertEqual(variants, obs.name_audit['roster_ocr_conflict']['variants'])
                    np.testing.assert_array_equal(original, obs.name_image)

    def test_matching_signature_does_not_turn_identical_pixels_into_a_rename(self):
        a, b = observation(128, 0, signature=True), observation(128, 1, signature=True)
        self.assertTrue(flow.roster_matches([a], [b]))
        self.assertTrue(a.entry.nickname_pending and b.entry.nickname_pending)

    def test_real_changed_name_or_wrong_avatar_or_level_cannot_fill_missing_friend(self):
        for changed in ('name', 'avatar', 'level', 'mode'):
            with self.subTest(changed=changed):
                a, b = observation(), observation(capture=1)
                if changed == 'name':
                    b = observation(128, 1)
                elif changed == 'avatar':
                    b.avatar_image = np.random.default_rng(91).integers(
                        0, 255, b.avatar_image.shape, dtype=np.uint8)
                elif changed == 'level':
                    b.entry = replace(b.entry, qianxing_level=1)
                else:
                    b.entry = replace(b.entry, interface_mode='normal')
                self.assertFalse(flow.roster_matches([a], [b]))
                self.assertFalse(a.entry.nickname_pending or b.entry.nickname_pending)

    def test_partial_nickname_cannot_explain_whole_original(self):
        a, b = observation(128, 0), observation(128, 1)
        left, _, right, _ = nickname_ocr.tight_rect(nickname_ocr.ink_mask(b.name_image))
        b.name_image[:, (left+right)//2:] = 220
        b.entry = replace(b.entry, nickname='测试')
        self.assertFalse(flow.roster_matches([a], [b]))

    def test_added_visible_dot_on_long_name_is_a_real_change_despite_high_average(self):
        a, b = observation(), observation(capture=1)
        a.name_image = np.full((57, 564, 3), 220, np.uint8)
        cv2.putText(a.name_image, 'LongNickname', (19, 45), cv2.FONT_HERSHEY_SIMPLEX,
                    .8, (45, 45, 45), 2, cv2.LINE_AA)
        b.name_image = a.name_image.copy()
        _, _, right, bottom = nickname_ocr.tight_rect(nickname_ocr.ink_mask(a.name_image))
        b.name_image[bottom-3:bottom, right+3:right+6] = 45
        a.entry = replace(a.entry, nickname='LongNickname')
        b.entry = replace(b.entry, nickname='LongNickname.')
        b.avatar_image = a.avatar_image.copy()
        self.assertGreater(layout.nickname_ink_similarity(a.name_image, b.name_image), .97)
        self.assertFalse(flow.roster_matches([a], [b]))

    def test_ambiguous_twins_do_not_become_unique_when_an_edge_is_removed(self):
        left = [observation(), observation()]
        right = [observation(capture=1), observation(capture=1)]
        self.assertFalse(flow.roster_matches(left, right))
        self.assertTrue(all(not obs.entry.nickname_pending for obs in left+right))
        # Exact duplicate multiplicity continues to be a valid coverage check.
        self.assertTrue(flow.roster_matches(left, [observation(), observation()]))
        self.assertFalse(flow.roster_matches(left, [observation()]))

    def test_failed_complete_matching_never_mutates_successful_partial_pair(self):
        a, b = observation(), observation(capture=1)
        old_name, new_name = a.entry.nickname, b.entry.nickname
        different = observation(128, 1)
        different.entry = replace(different.entry, qianxing_level=2)
        self.assertFalse(flow.roster_matches([a, observation(128, 0)], [b, different]))
        self.assertEqual((old_name, new_name), (a.entry.nickname, b.entry.nickname))
        self.assertNotIn('roster_ocr_conflict', a.name_audit)
        self.assertNotIn('roster_ocr_conflict', b.name_audit)

    def test_established_conflict_survives_later_uid_ocr_with_weaker_foreground_score(self):
        old, current = observation(), observation(capture=1)
        old.entry = replace(old.entry, nickname='', nickname_pending=True)
        old.name_audit = {'roster_ocr_conflict': {'variants': ['%', '％']}}
        with patch.object(layout, 'nickname_ink_similarity', return_value=.966):
            self.assertTrue(flow.roster_matches([current], [old]))
        self.assertTrue(current.entry.nickname_pending)
        self.assertEqual(['%', '％'], current.name_audit['roster_ocr_conflict']['variants'])


class RosterOcrConflictWorkflowTests(unittest.TestCase):
    def test_conflict_forces_uid_and_stays_pending_without_false_rename(self):
        harness = baseline.WorkflowRegressions('runTest')
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        uid = '111111111'
        old = observation()
        old.entry = replace(old.entry, nickname='旧档案')
        seed = flow.records_from_observations([old])
        seed[0]['uid'] = uid
        fid = harness.db.apply_verified_scan(seed, baseline.T0, 0., '', identity_complete=True)[0]
        harness.scan.interface_mode = 'qianxing'
        workflow = flow.ScanWorkflow(harness.scan)
        captures = iter([[observation()], [observation(capture=1)], [observation()]])
        calls = []

        def collect(uid=False, expected=None):
            calls.append('uid' if uid else 'quick')
            items = next(captures)
            directory = Path(harness.work.name)/str(len(calls))
            directory.mkdir()
            scan = SimpleNamespace(expected=1, slots=dict(enumerate(items)), evidence=directory,
                check=MagicMock(), shot=lambda:(None, [1]), uids={0:'111111111'}, profile_images={})
            workflow.last = scan
            return scan

        reuse_calls = []
        def reuse(items, previous, profiles):
            reuse_calls.append(items)
            if len(reuse_calls) == 1:
                return None
            records = flow.records_from_observations(items)
            records[0].update(id=fid, uid=uid, match_basis='name_image')
            return records

        workflow.collect = collect
        with patch.object(flow, 'reuse_identities', side_effect=reuse):
            result = workflow.run()
        self.assertTrue(result.complete, result.message)
        self.assertEqual(['quick', 'quick', 'uid'], calls)
        self.assertEqual(0, result.restarts)
        self.assertTrue(harness.db.get_friend(fid)['nickname_pending'])
        self.assertEqual([], result.comparison['renamed'])
        self.assertIsNotNone(harness.db.get_nickname_image(fid))
        report = json.loads((workflow.last.evidence/'workflow.json').read_text(encoding='utf-8'))
        self.assertEqual(['quick_confirmation', 'uid_confirmation'],
                         [item['phase'] for item in report['nickname_conflicts']])
        self.assertTrue(all(item['traversal_position'] == 1 for item in report['nickname_conflicts']))
        self.assertTrue(all(item['variants'] == ['%', '％'] for item in report['nickname_conflicts']))


if __name__ == '__main__':
    unittest.main()

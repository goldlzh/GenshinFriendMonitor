"""Replay pending-name overlap without converting image continuity into UID proof.

GENSHIN_T01_EVIDENCE explicitly opts into the user's read-only private snapshot.
No real images/text are distributed with the tests. Missing evidence is a skip.
"""
from tests import test_regressions as baseline
from tests import test_quick_scan_workflow as quick_tests

from dataclasses import fields, replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest

import cv2
import numpy as np
from PIL import Image

from app import fast_layout as layout, nickname_ocr
from app.friend_screen import CardBand
from app.fast_scan import RosterChanged
from app.parser import FriendEntry
from app.quick_scan import QuickScanWorkflow


def load_private_pair():
    value = os.environ.get('GENSHIN_T01_EVIDENCE', '').strip()
    if not value:
        raise unittest.SkipTest('Set GENSHIN_T01_EVIDENCE to opt into the private T01 replay')
    root = Path(value).resolve()
    files = list(root.rglob('overlap-failed.json')) if root.is_dir() else []
    if len(files) != 1:
        raise unittest.SkipTest('Private replay needs exactly one overlap-failed.json')
    source = files[0].parent
    report = json.loads(files[0].read_text(encoding='utf-8'))
    frame = np.asarray(Image.open(source/'overlap-failed.png').convert('RGB'))
    allowed = {f.name for f in fields(FriendEntry)}
    a, b = [FriendEntry(**{k:v for k,v in report[key].items() if k in allowed})
            for key in ('previous', 'current')]
    band = min(layout.bands(frame), key=lambda item: abs(item.center-b.click_y))
    old_band = CardBand(band.top+a.click_y-b.click_y, band.bottom+a.click_y-b.click_y)
    previous = layout.CardObservation(old_band, a,
        np.asarray(Image.open(source/'overlap-previous-name.png').convert('RGB')),
        np.asarray(Image.open(source/'overlap-previous-avatar.png').convert('RGB')))
    current = layout.CardObservation(band, b, layout.nickname_crop(frame, band),
        layout.crop(frame, (158, band.top+10, 232, band.top+105)))
    return report, frame, previous, current


class SyntheticPendingOverlapTests(unittest.TestCase):
    def pair(self):
        # Identical glyph geometry with changed local contrast, not private data.
        image = np.full((57,564,3),235,np.uint8)
        cv2.putText(image,'SYNTHETIC',(10,39),cv2.FONT_HERSHEY_SIMPLEX,
                    1.,(40,40,40),2,cv2.LINE_AA)
        shaded = image.copy()
        mask = nickname_ocr.ink_mask(image)>0
        shaded[mask & (np.indices(mask.shape)[1]<90)] = 130
        avatar = np.repeat(((np.indices((95,74)).sum(axis=0)%17)*12)
                           .astype(np.uint8)[:,:,None],3,axis=2)
        a = layout.CardObservation(CardBand(656,771),
            FriendEntry('','offline','13 days',25,194,713,0.,nickname_pending=True),
            image,avatar)
        b = layout.CardObservation(CardBand(419,534),
            FriendEntry('SYNTHETIC','offline','13 days',25,194,476,.99),
            shaded,avatar.copy())
        return a,b

    def test_complete_pending_glyphs_survive_local_contrast_without_private_fixtures(self):
        a,b = self.pair()
        self.assertLess(layout.nickname_ink_similarity(a.name_image,b.name_image),.97)
        self.assertTrue(nickname_ocr.whole_nickname_foreground_agrees(a.name_image,b.name_image))
        self.assertTrue(QuickScanWorkflow.same_person(None,a,b))
        self.assertTrue(QuickScanWorkflow.same_person(None,b,a))

    def test_pending_fallback_rejects_added_ink_avatar_and_card_field_conflicts(self):
        for kind in ('added_ink','avatar','mode','level','known_labels'):
            a,b = self.pair()
            if kind == 'added_ink':
                b.name_image[4:10,225:231] = 40
                # The looser raw-card match alone would accept this changed text.
                self.assertTrue(layout.same_person(a,b))
                self.assertFalse(nickname_ocr.whole_nickname_foreground_agrees(a.name_image,b.name_image))
            elif kind == 'avatar':
                b.avatar_image[:] = 220
            elif kind == 'mode':
                b.entry = replace(b.entry,interface_mode='qianxing')
            elif kind == 'level':
                b.entry = replace(b.entry,level=26)
            else:
                a.entry = replace(a.entry,nickname='KNOWN-A',nickname_pending=False)
                b.entry = replace(b.entry,nickname='KNOWN-B',nickname_pending=False)
            with self.subTest(kind=kind):
                self.assertFalse(QuickScanWorkflow.same_person(None,a,b))

    def test_blank_or_truncated_name_cannot_use_pending_recovery(self):
        for kind in ('blank','truncated'):
            a,b = self.pair()
            b.name_image[:,0 if kind=='blank' else 90:] = 235
            with self.subTest(kind=kind):
                self.assertFalse(QuickScanWorkflow.same_person(None,a,b))


class PrivateQuickOverlapReplayTests(unittest.TestCase):
    def setUp(self):
        self.report, self.frame, self.a, self.b = load_private_pair()

    def test_real_pending_overlap_survives_background_change_in_both_directions(self):
        # The historical implementation rejects the real pair at ink < .97,
        # even though the complete unscaled foregrounds account for every stroke.
        self.assertAlmostEqual(.967642, layout.nickname_ink_similarity(
            self.a.name_image, self.b.name_image), places=5)
        self.assertTrue(nickname_ocr.whole_nickname_foreground_agrees(
            self.a.name_image, self.b.name_image))
        for pending_a, pending_b in ((True,False), (False,True), (True,True)):
            a, b = replace(self.a), replace(self.b)
            a.entry = replace(a.entry, nickname='' if pending_a else self.b.entry.nickname,
                              nickname_pending=pending_a)
            b.entry = replace(b.entry, nickname='' if pending_b else self.b.entry.nickname,
                              nickname_pending=pending_b)
            with self.subTest(pending_a=pending_a, pending_b=pending_b):
                self.assertTrue(QuickScanWorkflow.same_person(None, a, b))
                self.assertTrue(QuickScanWorkflow.same_person(None, b, a))

    def test_real_recovered_name_updates_only_the_measured_slot_without_uid_or_db_write(self):
        harness = baseline.FastScanRegressions('runTest')
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        workflow = QuickScanWorkflow(harness.scan)
        index = self.report['position']-1
        workflow.expected = 178
        workflow.offset = self.report['offset']
        workflow.origin = workflow.offset+self.b.band.top-index*layout.ROW_PITCH
        workflow.slots[index] = self.a
        page = SimpleNamespace(confirmed=True, expected_total=workflow.expected,
            observations=[self.b], entries=[self.b.entry], issues=[])
        before = harness.db.get_all_friends()
        try:
            indices = workflow.add_page(self.frame, page)
        except RosterChanged:
            self.fail('The measured real overlap was incorrectly rejected as a reordered roster')
        self.assertEqual([index], indices)
        self.assertFalse(workflow.slots[index].entry.nickname_pending)
        self.assertTrue(workflow.slots[index].entry.nickname == self.b.entry.nickname)
        self.assertEqual({}, workflow.uids)
        self.assertEqual(before, harness.db.get_all_friends())

    def test_real_pair_cannot_hide_changed_strokes_avatar_mode_or_level(self):
        for kind in ('added_stroke','deleted_stroke','avatar','mode','level'):
            b = replace(self.b, name_image=self.b.name_image.copy(),
                        avatar_image=self.b.avatar_image.copy())
            if kind == 'added_stroke':
                b.name_image[3:10,3:10] = 25
            elif kind == 'deleted_stroke':
                mask = nickname_ocr.ink_mask(b.name_image)
                right = np.where(mask > 0)[1].max()
                b.name_image[:,right-8:right+1] = 235
            elif kind == 'avatar':
                b.avatar_image[:] = 220
            elif kind == 'mode':
                b.entry = replace(b.entry, interface_mode='qianxing')
            else:
                b.entry = replace(b.entry, level=b.entry.level+1)
            with self.subTest(kind=kind):
                self.assertFalse(QuickScanWorkflow.same_person(None, self.a, b))
                self.assertFalse(QuickScanWorkflow.same_person(None, b, self.a))

    def test_real_visual_recovery_does_not_override_two_known_conflicting_labels(self):
        a, b = replace(self.a), replace(self.b)
        a.entry = replace(a.entry, nickname='KNOWN-A', nickname_pending=False)
        b.entry = replace(b.entry, nickname='KNOWN-B', nickname_pending=False)
        self.assertFalse(QuickScanWorkflow.same_person(None, a, b))


class QuickPendingOverlapWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.harness = quick_tests.QuickScanWorkflowTests('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def test_pending_name_recovers_on_overlap_without_replacing_existing_friend_ids(self):
        ids = self.harness.seed(13, with_baseline=True)
        seen = 0
        def pending_once(index, obs):
            nonlocal seen
            if index == 5:
                seen += 1
                if seen == 1:
                    obs.entry = replace(obs.entry, nickname='', nickname_pending=True)
        state = self.harness.simulate(13, pending_once)
        workflow, result = self.harness.run_workflow(state,
            uid=lambda rect, frame, band, position: '%09d' % (100000000+position-1))
        self.assertTrue(result.complete)
        self.assertFalse(workflow.slots[5].entry.nickname_pending)
        self.assertEqual(set(ids), {row['id'] for row in self.harness.db.get_display_friends()})
        self.assertEqual(13, len(self.harness.db.get_display_friends()))

    def test_unreadable_overlap_remains_unknown_until_identity_is_verified(self):
        def unreadable(index, obs):
            if index == 5:
                obs.entry = replace(obs.entry, nickname='', nickname_pending=True)
        state = self.harness.simulate(8, unreadable)
        workflow, result = self.harness.run_workflow(state)
        record = next(r for r in workflow.records if r['index']==5)
        self.assertIsNone(record.get('uid'))
        self.assertIsNotNone(record.get('identity_review_key'))
        self.assertTrue(result.complete)


if __name__ == '__main__':
    unittest.main()

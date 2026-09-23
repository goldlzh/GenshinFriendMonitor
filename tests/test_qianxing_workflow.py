"""Mode changes require UID evidence, while each level retains its own meaning."""
from tests import test_regressions as baseline

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from app import fast_layout as layout, friend_screen, scan_workflow as flow
from app.fast_scan import FastScan, ScanIncomplete


UID = '333333333'


class QianxingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.harness = baseline.WorkflowRegressions('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def items(self, mode='normal', *, pending=False):
        items = self.harness.items([('名字', UID, 42)])
        items[0].entry = replace(items[0].entry, interface_mode=mode,
            level=60 if mode == 'normal' else None,
            qianxing_level=4 if mode == 'qianxing' else None,
            tags='休闲爱好者' if mode == 'qianxing' else None,
            nickname='' if pending else '名字', nickname_pending=pending)
        return items

    def seed(self, items):
        records = flow.records_from_observations(items)
        records[0]['uid'] = UID
        return self.harness.db.apply_verified_scan(records, baseline.T0, 0., '', identity_complete=True)[0]

    def run_workflow(self, items):
        self.harness.scan.interface_mode = items[0].entry.interface_mode
        workflow = flow.ScanWorkflow(self.harness.scan)
        calls = []
        def collect(uid=False, expected=None):
            calls.append('uid' if uid else 'quick')
            directory = Path(self.harness.work.name)/str(len(calls))
            directory.mkdir(exist_ok=True)
            scan = SimpleNamespace(expected=1, slots={0:items[0]}, evidence=directory,
                check=MagicMock(), shot=lambda:(None,[1]), uids={0:UID}, profile_images={})
            workflow.last = scan
            return scan
        workflow.collect = collect
        return workflow.run(), calls

    def test_switching_interfaces_reconfirms_shared_uid_and_preserves_both_levels(self):
        fid = self.seed(self.items())
        result, calls = self.run_workflow(self.items('qianxing'))
        self.assertTrue(result.complete, result.message)
        self.assertEqual(['quick','uid'], calls)
        saved = self.harness.db.get_friend(fid)
        self.assertEqual((UID,60,4,'休闲爱好者'),
            (saved['uid'],saved['level'],saved['qianxing_level'],saved['tags']))
        self.assertEqual([], result.comparison['added'])
        self.assertEqual([], result.comparison['removed'])
        result, calls = self.run_workflow(self.items())
        self.assertTrue(result.complete, result.message)
        self.assertEqual(['quick','uid'], calls)
        self.assertEqual(1, len(self.harness.db.get_all_friends()))
        self.assertEqual((60,4,'休闲爱好者'), tuple(self.harness.db.get_friend(fid)[key]
            for key in ('level','qianxing_level','tags')))

    def test_unchanged_qianxing_list_reuses_uid(self):
        self.seed(self.items('qianxing'))
        result, calls = self.run_workflow(self.items('qianxing'))
        self.assertTrue(result.complete, result.message)
        self.assertEqual(['quick'], calls)

    def test_pending_qianxing_name_compares_qianxing_level_not_adventure_level(self):
        items = self.items('qianxing', pending=True)
        fid = self.seed(items)
        self.harness.db.update_friend_manual(fid, UID, '', 60, None)
        records = flow.reuse_identities(items, self.harness.db.get_complete_roster(),
                                        self.harness.db.get_visual_profiles())
        self.assertIsNotNone(records)
        self.assertEqual(UID, records[0]['uid'])

    def test_same_names_cannot_hide_a_mode_change_between_passes(self):
        self.assertFalse(flow.roster_matches(self.items(),self.items('qianxing')))

    def test_fast_pass_rejects_changed_scanner_mode(self):
        self.harness.scan.interface_mode = 'qianxing'
        scan = FastScan(self.harness.scan)
        self.harness.scan.interface_mode = 'normal'
        with self.assertRaisesRegex(ScanIncomplete,'模式已改变'):
            scan.check()

    def test_qianxing_recovery_uses_separated_fields_instead_of_whole_card_ocr(self):
        self.harness.scan.interface_mode = 'qianxing'
        scan = FastScan(self.harness.scan)
        item = self.items('qianxing')[0]
        rejected = replace(item, entry=None)
        page = friend_screen.FriendPage(observations=[rejected])
        with patch.object(scan,'check'), patch.object(layout,'read_page',
                return_value=friend_screen.FriendPage(observations=[item])) as reader, \
                patch.object(flow.ocr_engine,'recognize') as whole_card:
            scan.recover_fields(np.zeros((1080,1920,3),np.uint8),page)
        self.assertEqual('qianxing',reader.call_args.kwargs['interface_mode'])
        self.assertEqual('休闲爱好者',page.entries[0].tags)
        whole_card.assert_not_called()

    def test_uid_return_checks_use_nickname_crop_without_adjacent_label(self):
        frame = np.full((1080,1920,3),220,np.uint8)
        band = friend_screen.CardBand(116,231)
        frame[126:221,158:232] = np.random.default_rng(17).integers(0,255,(95,74,3),dtype=np.uint8)
        cv2.putText(frame,'NAME',(278,161),cv2.FONT_HERSHEY_SIMPLEX,.7,(40,40,40),2)
        cv2.rectangle(frame,(400,140),(1000,171),(75,186,224),-1)
        cv2.putText(frame,'OLD TAG',(412,164),cv2.FONT_HERSHEY_SIMPLEX,.6,(255,255,255),1)
        changed = frame.copy()
        cv2.rectangle(changed,(400,140),(1000,171),(75,186,224),-1)
        cv2.putText(changed,'NEW TAG',(412,164),cv2.FONT_HERSHEY_SIMPLEX,.6,(255,255,255),1)
        with patch.object(layout,'bands',return_value=[band]):
            reference = flow.UidPass.visible_reference(frame,'qianxing')
        matched, _ = flow.UidPass.visible_scores(changed,[band],reference,'qianxing')
        self.assertTrue(matched)
        self.assertLess(reference[0].name_image.shape[1],layout.NAME[2]-layout.NAME[0])


if __name__ == '__main__':
    unittest.main()

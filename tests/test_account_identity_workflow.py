"""HUD ownership, signature-backed renames and profile archive integration."""
from tests import test_regressions as _baseline

from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from app import account_identity as identity, scan_workflow as flow
from app.database import Database
from app.scanner import Scanner


OWNER, OTHER, FRIEND = '111111111', '222222222', '333333333'


class AccountIdentityTests(unittest.TestCase):
    def test_hud_requires_label_ascii_digits_and_confidence(self):
        self.assertEqual(OWNER, identity.account_uid_from_text('UID: 111111111', .99))
        for text, score in [('111111111', 1.), ('UID 011111111', 1.),
                            ('UID 111111111 extra', 1.), ('UID 111111111', .89),
                            ('UID 111111111', float('nan'))]:
            self.assertIsNone(identity.account_uid_from_text(text, score))

    def test_only_bottom_right_roi_is_passed_to_ocr(self):
        image = np.zeros((1080,1920,3),np.uint8)
        x1,y1,x2,y2 = identity.ACCOUNT_UID_RECT
        image[y1:y2,x1:x2] = 123
        image[188:228,576:756] = 45  # Friend UID region must be excluded.
        with patch.object(identity.ocr_engine,'recognize_lines',return_value=[('UID '+OWNER,.99)]) as read:
            self.assertEqual(OWNER,identity.read_account_uid(image))
        self.assertEqual((y2-y1,x2-x1,3),read.call_args.args[0][0].shape)
        self.assertTrue(np.all(read.call_args.args[0][0] == 123))

    def test_two_fresh_matching_frames_and_no_input(self):
        capture = MagicMock(return_value=np.zeros((1080,1920,3),np.uint8))
        with patch.object(identity,'read_account_uid',side_effect=[OWNER,OWNER]), \
             patch.object(identity.time,'sleep'):
            self.assertEqual(OWNER,identity.read_current_account((1,2,1921,1082),capture))
        self.assertEqual(2,capture.call_count)

    def test_changing_or_unreadable_uid_cannot_confirm(self):
        for values in ([OWNER,OTHER,OWNER,OTHER],[None]*4):
            with patch.object(identity,'read_account_uid',side_effect=values), \
                 patch.object(identity.time,'sleep'), self.assertRaises(identity.AccountIdentityError):
                identity.read_current_account((0,0,1920,1080),MagicMock())

    def test_account_change_stops_both_scanner_workers(self):
        with DatabaseContext() as db:
            db.set_account_uid(OWNER)
            scanner = Scanner(db)
            with patch.object(identity,'read_account_uid',return_value=OTHER), \
                 self.assertRaisesRegex(identity.AccountIdentityError,'不符'):
                scanner._verify_account_frame(None)
            self.assertTrue(scanner._stop.is_set())
            self.assertTrue(scanner._uid_stop.is_set())

    def test_unassigned_persistent_database_cannot_scan(self):
        with tempfile.TemporaryDirectory() as work:
            db=Database(str(Path(work)/'legacy.db'))
            try:
                with self.assertRaisesRegex(identity.AccountIdentityError,'选择账号'):
                    Scanner(db)._confirm_account((0,0,1920,1080))
            finally:
                db.close()


class DatabaseContext:
    def __enter__(self):
        self.db=Database(':memory:')
        return self.db
    def __exit__(self,*exc):
        self.db.close()


class SignatureWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.harness=_baseline.WorkflowRegressions('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)

    def items(self,name,signature='同一个可靠签名',seed=42):
        items=self.harness.items([(name,FRIEND,seed)])
        items[0].entry=replace(items[0].entry,signature=signature,signature_confidence=.99)
        return items

    def seed(self,items):
        records=flow.records_from_observations(items)
        records[0]['uid']=FRIEND
        self.harness.db.apply_verified_scan(records,_baseline.T0,1.,'',identity_complete=True)

    def test_unique_signature_corresponds_rename_but_requests_uid_verification(self):
        self.seed(self.items('旧名字'))
        current=self.items('新名字')
        records=flow.reuse_identities(current,self.harness.db.get_complete_roster(),
                                     self.harness.db.get_visual_profiles())
        self.assertEqual(FRIEND,records[0]['uid'])
        self.assertEqual('signature',records[0]['match_basis'])
        self.assertTrue(flow.roster_matches(self.items('旧名字'),current))

    def test_empty_duplicate_or_truncated_signature_does_not_bind_new_name(self):
        for signature in ('','相同签名…'):
            with self.subTest(signature=signature):
                self.seed(self.items('旧名字',signature))
                self.assertIsNone(flow.reuse_identities(self.items('新名字',signature),
                    self.harness.db.get_complete_roster(),self.harness.db.get_visual_profiles()))
        old=self.items('甲')+self.items('乙',seed=17)
        records=flow.records_from_observations(old)
        records[0]['uid'],records[1]['uid']=FRIEND,'444444444'
        self.harness.db.apply_verified_scan(records,_baseline.T1,1.,'',identity_complete=True)
        current=self.items('新甲')+self.items('新乙',seed=17)
        self.assertIsNone(flow.reuse_identities(current,self.harness.db.get_complete_roster(),
                                              self.harness.db.get_visual_profiles()))

    def test_same_signature_with_different_avatar_cannot_bind(self):
        self.seed(self.items('旧名字'))
        self.assertIsNone(flow.reuse_identities(self.items('新名字',seed=99),
            self.harness.db.get_complete_roster(),self.harness.db.get_visual_profiles()))

    def workflow_with_profile(self, items, profile):
        workflow=flow.ScanWorkflow(self.harness.scan)
        calls=[]
        def collect(uid=False,expected=None):
            calls.append(uid)
            folder=Path(self.harness.work.name)/str(len(calls));folder.mkdir(exist_ok=True)
            result=SimpleNamespace(expected=len(items),slots=dict(enumerate(items)),evidence=folder,
                 check=MagicMock(),shot=lambda:(None,[len(items)]),uids={0:FRIEND},
                 profile_images={FRIEND:profile})
            workflow.last=result;workflow.passes.append(str(folder))
            return result
        workflow.collect=collect
        return workflow,calls

    def test_signature_rename_collects_profile_and_retains_previous_capture(self):
        self.seed(self.items('旧名字'))
        fid=self.harness.db.get_display_friends()[0]['id']
        self.harness.db.save_profile_capture(fid,b'previous',_baseline.T0,'旧名字')
        workflow,calls=self.workflow_with_profile(self.items('新名字'),b'new')
        self.assertTrue(workflow.run().complete)
        self.assertEqual([False,True],calls)
        captures=self.harness.db.get_profile_captures(fid)
        self.assertEqual([b'new',b'previous'],[c['image'] for c in captures])
        self.assertEqual('新名字',self.harness.db.get_friend(fid)['nickname'])

    def test_account_change_before_commit_preserves_previous_roster_and_images(self):
        self.seed(self.items('旧名字'))
        self.harness.db.set_account_uid(OWNER)
        workflow,_=self.workflow_with_profile(self.items('新名字'),b'new')
        with patch.object(identity,'read_account_uid',return_value=OTHER):
            result=workflow.run()
        self.assertFalse(result.complete)
        row=self.harness.db.get_display_friends()[0]
        self.assertEqual('旧名字',row['nickname'])
        self.assertEqual([],self.harness.db.get_profile_captures(row['id']))

    def test_unconfirmed_profile_images_are_bounded_outside_friend_archive(self):
        scan=flow.UidPass(self.harness.scan,1)
        for index in range(3):
            scan.save_uid_failure('read-%03d' % index,np.full((50,50,3),index,np.uint8),{})
        self.assertEqual(2,len(list((self.harness.scan.evidence_root/'uid-failures').glob('*.jpg'))))
        self.assertEqual([],list(scan.evidence.glob('uid-failed-*.png')))

    def test_blank_nickname_can_restore_list_after_closing_profile(self):
        scan=flow.UidPass(self.harness.scan,1)
        frame=np.full((1080,1920,3),220,np.uint8)
        band=_baseline.friend_screen.CardBand(116,231)
        frame[126:221,158:232]=np.random.default_rng(33).integers(0,255,(95,74,3),dtype=np.uint8)
        with patch.object(scan,'check'),patch.object(scan,'wait'),patch.object(scan,'park_pointer'), \
             patch.object(scan,'shot',return_value=(None,frame)), \
             patch.object(flow.layout,'read_header',return_value=(True,1)), \
             patch.object(flow.layout,'bands',return_value=[band]), \
             patch.object(flow.game_input,'press_esc',return_value=True) as esc:
            scan.restore_list(frame)
        esc.assert_called_once_with(hwnd=None)


if __name__ == '__main__':
    unittest.main()

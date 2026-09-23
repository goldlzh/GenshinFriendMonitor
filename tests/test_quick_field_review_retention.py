"""Successful monitoring avoids duplicate diagnostic crops; failure keeps them."""
from tests import test_regressions as baseline
from tests import test_quick_scan_workflow as workflow_tests

from pathlib import Path
import json
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from app import capture, fast_layout as layout
from app.fast_scan import FastScan, ScanIncomplete, _decode
from app.quick_scan import QuickScanWorkflow


class QuickFieldReviewRetentionTests(unittest.TestCase):
    def setUp(self):
        self.harness = workflow_tests.QuickScanWorkflowTests('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.harness.seed(7)
        self.state = self.harness.simulate(7, self.pending_fields)
        self.harness.stack.enter_context(patch.object(baseline.CONFIG, 'save_scan_evidence', False))

    @staticmethod
    def pending_fields(index, obs):
        if index == 0:
            obs.name_audit = {'method': 'rejected', 'reason': 'review sample'}
            obs.entry.signature_pending = True
            obs.signature_image = np.full((35, 776, 3), 221, np.uint8)
            obs.signature_image[10:22, 12:54] = 91

    def test_three_successful_rounds_do_not_write_extra_field_review_files(self):
        old_path = Path(baseline.CONFIG.log_dir)/'nickname-review'
        before = set(old_path.rglob('*')) if old_path.exists() else set()
        for _ in range(3):
            workflow, result = self.harness.run_workflow(self.state)
            self.assertTrue(result.complete)
            self.assertFalse((workflow.evidence/'field-review').exists())
            self.assertEqual([], workflow.nickname_reviews)
            self.assertEqual({}, workflow._field_review_drafts)
        after = set(old_path.rglob('*')) if old_path.exists() else set()
        self.assertEqual(before, after)

    def test_failed_round_keeps_both_field_crops_under_account_evidence(self):
        workflow = QuickScanWorkflow(self.harness.scanner)
        with patch.object(layout, 'read_header', return_value=(True, 8)):
            result = workflow.run()
        self.assertFalse(result.complete)
        records = list((workflow.evidence/'field-review').glob('*.json'))
        self.assertEqual({'nickname', 'signature'}, {
            json.loads(path.read_text(encoding='utf-8'))['field'] for path in records})
        for path in records:
            metadata = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(self.harness.scanner.account_uid, metadata['account_uid'])
            self.assertTrue(path.resolve().is_relative_to(self.harness.scanner.evidence_root.resolve()))
            image = _decode((path.parent/metadata['image']).read_bytes())
            self.assertGreater(image.size, 0)
        self.assertFalse(list(workflow.evidence.glob('page-*.png')))
        saved_bytes = {path: path.read_bytes() for path in (workflow.evidence/'field-review').iterdir()}
        self.harness.run_workflow(self.state)
        self.assertEqual(saved_bytes, {path: path.read_bytes() for path in saved_bytes})

    def test_invalid_geometry_signals_scanner_stop_before_account_or_game_actions(self):
        scan = FastScan(self.harness.scanner)
        with patch.object(capture, 'capture_region', return_value=np.zeros((700, 1000, 3), np.uint8)), \
             patch.object(self.harness.scanner, '_unsupported_view') as unsupported, \
             patch.object(self.harness.scanner, '_verify_account_frame') as account:
            with self.assertRaisesRegex(ScanIncomplete, '16:9'):
                scan.shot()
        unsupported.assert_called_once()
        account.assert_not_called()


if __name__ == '__main__':
    unittest.main()

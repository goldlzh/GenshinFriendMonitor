"""Selective UID recovery and latest-only identity matching, with no game input."""
from tests import test_regressions as baseline
from tests import test_quick_scan_workflow as quick
from tests.test_quick_identity_storage import record

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from app.database import Database
from app.fast_scan import _png
from app.membership_events import pending_removals
from app.quick_scan import QuickIdentities, QuickScanWorkflow


class SelectiveUidTests(unittest.TestCase):
    def setUp(self):
        self.h = quick.QuickScanWorkflowTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.db = self.h.db

    def changes(self):
        return [r for r in self.db.get_recent_events(changes_only=True) if r['event_type'] in ('added', 'removed')]

    def test_identical_names_and_signatures_are_resolved_by_each_profile(self):
        ids = self.h.seed(2, with_baseline=True)
        for fid in ids:
            self.db.update_friend_manual(fid, self.db.get_friend(fid)['uid'], '同名', 60, '相同签名')
        def twins(index, obs):
            obs.entry = replace(obs.entry, nickname='同名', signature='相同签名', signature_confidence=.99)
        state = self.h.simulate(2, twins)
        workflow, result = self.h.run_workflow(state, uid=lambda rect, frame, band, serial: str(100000000+serial-1))
        self.assertEqual(2, workflow.read_uid.call_count)
        self.assertEqual(ids, [r['id'] for r in self.db.get_display_friends()])
        self.assertEqual({'online'}, {r['current_status'] for r in self.db.get_display_friends()})
        self.assertEqual([], self.changes())
        self.assertTrue(result.uid_complete)

    def test_cross_page_namesake_revisits_only_earlier_unverified_card(self):
        ids = self.h.seed(8, with_baseline=True)
        self.db.update_friend_manual(ids[0], '100000000', '同名', 60, None)
        def twins(index, obs):
            if index in (0, 7):
                obs.entry = replace(obs.entry, nickname='同名')
        state = self.h.simulate(8, twins)
        workflow, _ = self.h.run_workflow(state, uid=lambda rect, frame, band, serial: str(100000000+serial-1))
        self.assertEqual([8, 1], [call.args[3] for call in workflow.read_uid.call_args_list])
        self.assertEqual(ids, [r['id'] for r in self.db.get_display_friends()])
        self.assertEqual([], self.changes())

    def test_weak_unreadable_nickname_uses_uid_and_refreshes_latest_image(self):
        ids = self.h.seed(1, with_baseline=True)
        def weak(index, obs):
            obs.entry = replace(obs.entry, nickname='', nickname_pending=True)
        state = self.h.simulate(1, weak)
        with patch('app.quick_scan.layout.nickname_ink_similarity', return_value=.976):
            workflow, _ = self.h.run_workflow(state, uid='100000000')
        workflow.read_uid.assert_called_once()
        self.assertEqual(ids[0], self.db.get_display_friends()[0]['id'])
        self.assertEqual('online', self.db.get_display_friends()[0]['current_status'])
        self.assertEqual(workflow.records[0]['name_image'], self.db.get_nickname_image(ids[0]))
        self.assertEqual([], self.changes())

    def test_changed_nickname_and_signature_still_recover_uid(self):
        ids = self.h.seed(1, with_baseline=True)
        def changed(index, obs):
            obs.entry = replace(obs.entry, nickname='全新昵称', signature='全新签名', signature_confidence=.99)
        state = self.h.simulate(1, changed)
        workflow, _ = self.h.run_workflow(state, uid='100000000')
        workflow.read_uid.assert_called_once()
        current = self.db.get_display_friends()[0]
        self.assertEqual((ids[0], '全新昵称', '全新签名'), (current['id'], current['nickname'], current['signature']))
        self.assertEqual([], self.changes())

    def test_failed_check_defers_disappearance_then_next_round_retries_and_merges(self):
        owners = self.h.seed(1, with_baseline=True)
        def weak(index, obs):
            obs.entry = replace(obs.entry, nickname='', nickname_pending=True)
        state = self.h.simulate(1, weak)
        first, _ = self.h.run_workflow(state)
        first.read_uid.assert_called_once()
        draft = self.db.get_display_friends()[0]['id']
        self.assertNotEqual(owners[0], draft)
        self.assertEqual(1, len(pending_removals(self.db._conn)))
        second, _ = self.h.run_workflow(state, uid='100000000', stamp=baseline.T2)
        second.read_uid.assert_called_once()
        self.assertEqual(owners, [r['id'] for r in self.db.get_display_friends()])
        self.assertIsNone(self.db.get_friend(draft))
        self.assertEqual([], pending_removals(self.db._conn))
        self.assertEqual([], self.changes())

    def test_conflicting_uid_reads_abort_without_partial_commit(self):
        self.h.seed(2, with_baseline=True)
        def twins(index, obs):
            obs.entry = replace(obs.entry, nickname='同名')
        state = self.h.simulate(2, twins)
        before = list(self.db._conn.iterdump())
        workflow = QuickScanWorkflow(self.h.scanner)
        with patch.object(workflow, 'verify_visible', side_effect=lambda page: ((0,0,1920,1080),
                np.zeros((1080,1920,3), dtype=np.uint8), [o.band for o in page.observations])), \
                patch.object(workflow, 'read_uid', return_value='100000000'):
            result = workflow.run()
        self.assertFalse(result.complete)
        self.assertIn('相同 UID', result.message)
        self.assertEqual(before, list(self.db._conn.iterdump()))


class LatestIdentityArchiveTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)

    def test_old_uidless_archives_cannot_compete_with_latest_roster(self):
        owner = self.db.apply_verified_scan([record('123456789', name='当前')], baseline.T0, 0., 'synthetic')[0]
        stale = self.db.add_friend(None, '当前', 60, None, 'unknown', baseline.T0)
        pixels = _png(np.full((20,80,3), 55, np.uint8))
        self.db._conn.execute('UPDATE visual_profiles SET name_image=? WHERE friend_id=?', (pixels,owner))
        self.db._conn.execute('INSERT INTO visual_profiles VALUES (?,?,?,?,?)',
                              (stale, pixels, pixels, baseline.T0, 'normal'))
        self.db._conn.commit()
        self.assertEqual({owner}, self.db.get_identity_candidate_ids())
        identity = QuickIdentities(self.db.get_all_friends(), self.db.get_visual_profiles(),
                                   matching_ids=self.db.get_identity_candidate_ids())
        self.assertEqual([owner], [r['id'] for r in identity.candidates])
        self.assertEqual([owner], [p['friend_id'] for p, image in identity.images])
        self.assertIsNotNone(self.db.get_friend(stale))

    def test_pending_known_identity_remains_available_beside_current_draft(self):
        owner = self.db.apply_verified_scan([record('123456789', name='当前')], baseline.T0, 0., 'synthetic')[0]
        draft = self.db.apply_verified_scan([record(name='', status='unknown')], baseline.T1, 0., 'synthetic')[0]
        self.assertEqual({owner, draft}, self.db.get_identity_candidate_ids())

    def test_uid_confirmed_merge_keeps_latest_name_signature_image_and_history(self):
        owner = self.db.apply_verified_scan([record('123456789', name='旧名', status='offline')], baseline.T0, 0., 'synthetic')[0]
        draft = self.db.add_friend(None, '新名', 60, '最新签名', 'offline', baseline.T1)
        self.db._conn.execute('UPDATE friends SET last_scan_at=?,signature=? WHERE id=?', (baseline.T0, '旧签名', owner))
        self.db._conn.execute('UPDATE friends SET last_scan_at=? WHERE id=?', (baseline.T1, draft))
        pixels = _png(np.full((20,80,3), 55, np.uint8))
        self.db._conn.execute('INSERT INTO visual_profiles VALUES (?,?,?,?,?)', (draft,pixels,pixels,baseline.T1,'normal'))
        events_before = self.db._conn.execute('SELECT COUNT(*) FROM status_events').fetchone()[0]
        with self.db._conn:
            self.db._merge_friend_locked(owner, draft)
        row = self.db.get_friend(owner)
        self.assertEqual(('123456789','新名','最新签名'), (row['uid'],row['nickname'],row['signature']))
        self.assertEqual(pixels, self.db.get_nickname_image(owner))
        self.assertIsNone(self.db.get_friend(draft))
        self.assertEqual(events_before, self.db._conn.execute('SELECT COUNT(*) FROM status_events').fetchone()[0])
        self.assertEqual({'旧名'}, {r[0] for r in self.db._conn.execute('SELECT alias FROM nickname_aliases WHERE friend_id=?',(owner,))})

    def test_duplicate_same_uid_is_backed_up_and_consolidated_on_open(self):
        with tempfile.TemporaryDirectory(dir=baseline.WORK) as temp:
            path = Path(temp)/'duplicates.db'
            db = Database(str(path))
            first = db.add_friend('123456789', '旧名', 60, '旧签名', 'offline', baseline.T0)
            db.add_friend('123456789', '最新名', 60, '最新签名', 'offline', baseline.T1)
            db.close()
            db = Database(str(path))
            try:
                self.assertEqual(1, len(db.get_all_friends()))
                row = db.get_friend(first)
                self.assertEqual(('最新名', '最新签名'), (row['nickname'], row['signature']))
                self.assertEqual(1, len(list(Path(temp).glob('*.backup-*'))))
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()

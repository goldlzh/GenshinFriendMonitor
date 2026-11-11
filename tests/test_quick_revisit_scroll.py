"""Synthetic image regressions for late identity revisits; no private T01 images."""
from dataclasses import replace
import json
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from tests import test_quick_scan_workflow as quick
from tests import test_regressions as baseline
from app import capture, fast_layout as layout, scroller
from app.quick_scan import QuickScanWorkflow
from app.friend_screen import CardBand


class QuickRevisitScrollTests(unittest.TestCase):
    def setUp(self):
        self.h = quick.QuickScanWorkflowTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.db = self.h.db

    def namesakes(self, count, earlier, *, seeded=False):
        ids = self.h.seed(count, with_baseline=True) if seeded else []
        if seeded:
            self.db.update_friend_manual(ids[earlier], str(100000000 + earlier),
                                         'synthetic-namesake', 60, None)

        def mutate(index, observation):
            if index in (earlier, count - 1):
                observation.entry = replace(observation.entry, nickname='synthetic-namesake')

        state = self.h.simulate(count, mutate)
        workflow = QuickScanWorkflow(self.h.scanner)

        def visible(page):
            frame = capture.capture_region(0, 0, 1920, 1080)
            bands = layout.bands(frame)
            self.assertEqual(len(page.observations), len(bands))
            return (0, 0, 1920, 1080), frame, bands

        workflow.verify_visible = MagicMock(side_effect=visible)
        workflow.read_uid = MagicMock(side_effect=lambda rect, frame, band, serial:
                                      str(100000000 + serial - 1))
        workflow.home = MagicMock(wraps=workflow.home)
        return workflow, state, ids

    def run_workflow(self, workflow):
        with patch('app.quick_scan.now_str', return_value=baseline.T1):
            return workflow.run()

    def assert_committed(self, workflow, result, count, earlier):
        self.assertTrue(result.complete,
                        'slots=%d pages=%d: %s' %
                        (len(workflow.slots), workflow.frame_count, result.message))
        self.assertEqual(count, result.total)
        self.assertEqual(set(range(count)), set(workflow.slots))
        self.assertEqual([count, earlier + 1],
                         [call.args[3] for call in workflow.read_uid.call_args_list])
        self.assertEqual(str(100000000 + earlier), workflow.uids[earlier])
        self.assertEqual(str(100000000 + count - 1), workflow.uids[count - 1])
        friends = self.db.get_display_friends()
        self.assertEqual(count, len(friends))
        self.assertEqual(count, len({row['id'] for row in friends}))
        self.assertEqual(2, len([row for row in friends if row.get('uid')]))
        report = json.loads((workflow.evidence / 'workflow.json').read_text(encoding='utf-8'))
        self.assertTrue(report['committed'])

    def test_complete_global_roster_does_not_reject_valid_historical_page_motion(self):
        # Explicit synthetic displacement/geometry, not private T01 image replay.
        workflow, _, _ = self.namesakes(179, 20)
        workflow.slots = dict.fromkeys(range(179))
        workflow.expected, workflow.offset, workflow.origin = 179, 0., 116
        before, after = object(), object()
        before_rows = [CardBand(116 + i * layout.ROW_PITCH,
                               116 + i * layout.ROW_PITCH + layout.ROW_HEIGHT)
                       for i in range(6)]
        after_rows = before_rows[:5]
        bands = lambda image, **kwargs: before_rows if image is before else after_rows
        thumb = lambda image, **kwargs: SimpleNamespace(
            top=118 if image is before else 150,
            bottom=172 if image is before else 204, at_bottom=False)
        with patch.object(workflow, 'drag') as drag, \
                patch.object(workflow, 'shot', return_value=((0, 0, 1920, 1080), after)), \
                patch.object(workflow, 'save_image'), \
                patch.object(layout, 'bands', side_effect=bands), \
                patch.object(layout, 'scrollbar', side_effect=thumb), \
                patch.object(layout, 'scrollbar_displacement', return_value=None), \
                patch.object(layout, 'scroll_displacement', return_value=(5 * layout.ROW_PITCH, .99)), \
                patch.object(layout, 'is_friend_list', return_value=True):
            try:
                _, returned, movement = workflow.advance((0, 0, 1920, 1080), before)
            except Exception as error:
                self.fail('Valid historical motion rejected: %s; corrections=%s' %
                          (error, [call.kwargs.get('distance') for call in drag.call_args_list[1:]]))
        self.assertIs(after, returned)
        self.assertEqual([], movement['corrections'])
        drag.assert_called_once()

    def test_179_rows_revisit_beyond_home_page_before_first_atomic_commit(self):
        workflow, state, _ = self.namesakes(179, 20)
        apply = self.db.apply_verified_scan

        def commit(records, *args, **kwargs):
            # Collecting all rows cannot commit until both namesakes are verified.
            self.assertEqual({20, 178}, set(workflow.uids))
            self.assertEqual(2, workflow.home.call_count)
            return apply(records, *args, **kwargs)

        with patch.object(self.db, 'apply_verified_scan', side_effect=commit) as save:
            result = self.run_workflow(workflow)
        self.assert_committed(workflow, result, 179, 20)
        save.assert_called_once()
        self.assertGreater(workflow.frame_count, 37)
        self.assertEqual(2, state['read_offsets'].count(0.))

    def test_revisit_first_row_beyond_home_and_partial_bottom_pages(self):
        for count, earlier in ((14, 6), (18, 10), (23, 15)):
            with self.subTest(count=count, earlier=earlier):
                # Each subcase needs an independent DB and patch stack.
                case = QuickRevisitScrollTests('runTest')
                case.setUp()
                try:
                    workflow, _, _ = case.namesakes(count, earlier)
                    result = case.run_workflow(workflow)
                    case.assert_committed(workflow, result, count, earlier)
                finally:
                    case.doCleanups()

    def test_revisit_keeps_existing_ids_and_does_not_duplicate_or_remove_friends(self):
        workflow, _, ids = self.namesakes(18, 6, seeded=True)
        result = self.run_workflow(workflow)
        self.assertTrue(result.complete, result.message)
        self.assertEqual(ids, [row['id'] for row in self.db.get_display_friends()])
        self.assertEqual(18, len(self.db.get_all_friends()))
        changes = [r for r in self.db.get_recent_events(changes_only=True)
                   if r['event_type'] in ('added', 'removed')]
        self.assertEqual([], changes)
        self.assertEqual([18, 7], [call.args[3] for call in workflow.read_uid.call_args_list])

    def test_stalled_historical_scroll_never_commits_completed_row_count(self):
        workflow, _, _ = self.namesakes(18, 6, seeded=True)
        before = list(self.db._conn.iterdump())
        drag = scroller.drag_six_rows.side_effect

        def stalled(*args, **kwargs):
            if workflow.home.call_count >= 2:
                return True  # Synthetic input was ineffective: image never moved.
            return drag(*args, **kwargs)

        with patch.object(scroller.drag_six_rows, 'side_effect', stalled), \
                patch.object(self.db, 'apply_verified_scan', wraps=self.db.apply_verified_scan) as save:
            result = self.run_workflow(workflow)
        self.assertFalse(result.complete)
        self.assertEqual(18, len(workflow.slots))
        self.assertNotIn(6, workflow.uids)
        save.assert_not_called()
        self.assertEqual(before, list(self.db._conn.iterdump()))

    def test_duplicate_uid_during_historical_recheck_keeps_previous_roster(self):
        workflow, _, _ = self.namesakes(18, 6, seeded=True)
        before = list(self.db._conn.iterdump())
        workflow.read_uid.side_effect = lambda *args: '100000017'
        with patch.object(self.db, 'apply_verified_scan', wraps=self.db.apply_verified_scan) as save:
            result = self.run_workflow(workflow)
        self.assertFalse(result.complete)
        self.assertIn('相同 UID', result.message)
        self.assertEqual(before, list(self.db._conn.iterdump()))
        save.assert_not_called()

    def test_reordered_historical_rows_abort_before_rechecking_or_committing(self):
        workflow, _, _ = self.namesakes(18, 6, seeded=True)
        before = list(self.db._conn.iterdump())
        read = layout.read_page.side_effect

        def reordered(frame, **kwargs):
            page = read(frame, **kwargs)
            if workflow.home.call_count >= 2:
                first, second = page.observations[:2]
                page.observations[:2] = [replace(second, band=first.band),
                                         replace(first, band=second.band)]
                page.entries = [observation.entry for observation in page.observations]
            return page

        with patch.object(layout.read_page, 'side_effect', reordered), \
                patch.object(self.db, 'apply_verified_scan', wraps=self.db.apply_verified_scan) as save:
            result = self.run_workflow(workflow)
        self.assertFalse(result.complete)
        self.assertEqual('reordered', workflow.failure_reason)
        self.assertNotIn(6, workflow.uids)
        self.assertEqual(before, list(self.db._conn.iterdump()))
        save.assert_not_called()

    def test_revisit_page_limit_does_not_skip_unverified_identity(self):
        workflow, _, _ = self.namesakes(18, 6, seeded=True)
        before = list(self.db._conn.iterdump())
        revisit = workflow.verify_late_ambiguities

        def bounded_revisit():
            with patch.object(baseline.CONFIG, 'max_scrolls', 1):
                revisit()

        with patch.object(workflow, 'verify_late_ambiguities', side_effect=bounded_revisit), \
                patch.object(self.db, 'apply_verified_scan', wraps=self.db.apply_verified_scan) as save:
            result = self.run_workflow(workflow)
        self.assertFalse(result.complete)
        self.assertNotIn(6, workflow.uids)
        self.assertEqual(before, list(self.db._conn.iterdump()))
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main()

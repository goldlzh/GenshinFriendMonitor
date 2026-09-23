"""Online-only scans must expose a clipped offline boundary at list bottom."""
from tests import test_regressions as baseline
from tests import test_new_friend_tail as tail

from dataclasses import replace
import unittest
from unittest.mock import patch

from app import fast_layout as layout
from app.online_scan import OnlineScanWorkflow
from app.scan_workflow import records_from_observations


class OnlineTailTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting another TestCase's test methods.
    setUp = baseline.FastScanRegressions.setUp
    images = tail.NewFriendTailTests.images
    page = tail.NewFriendTailTests.page
    tail_case = tail.NewFriendTailTests.tail_case

    def test_real_forward_tail_probe_finds_offline_boundary_and_preserves_full_roster(self):
        seed, rect, before, after, first, last = self.tail_case()
        for obs in [*seed.slots.values(), *first.observations, *last.observations]:
            obs.entry = replace(obs.entry, status='online', last_seen=None)
        last.observations[-1].entry = replace(
            last.observations[-1].entry, status='offline', last_seen='31天前')

        roster = [seed.slots[i] for i in sorted(seed.slots)]
        roster += first.observations + [last.observations[-1]]
        records = records_from_observations(roster)
        for index, record in enumerate(records):
            record['uid'] = str(100000000 + index)
        self.db.apply_verified_scan(records, baseline.T0, 0., str(self.work.name),
                                    identity_complete=True)
        previous = self.db.get_complete_roster()
        self.assertEqual(152, len(previous['rows']))

        run = OnlineScanWorkflow(self.scan)
        for field in ('expected', 'origin', 'offset', 'slots'):
            setattr(run, field, getattr(seed, field))
        run.uids = {i: str(100000000 + i) for i in run.slots}
        run.identity_sources = {i: 'test_confirmed_uid' for i in run.slots}

        def inspect_actions(frame, page, context):
            states = [obs.entry.status for obs in page.observations]
            boundary = next((i for i, state in enumerate(states) if state == 'offline'), len(states))
            return states, boundary

        def bind_confirmed_uids(current_rect, frame, page, indices):
            for index in indices:
                run.uids[index] = str(100000000 + index)
                run.identity_sources[index] = 'test_confirmed_uid'
            return current_rect, frame

        # Only field recognition and game I/O are simulated. The actual forward
        # movement, overlap calculation and online commit use the real frame pair.
        with patch.object(run, 'home', return_value=(rect, before)), \
             patch.object(run, 'shot', return_value=(rect, after)), \
             patch.object(run, 'drag') as drag, \
             patch.object(layout, 'read_page', side_effect=[first, last]), \
             patch.object(run, 'inspect_actions', side_effect=inspect_actions), \
             patch.object(run, 'after_page', side_effect=bind_confirmed_uids), \
             patch.object(layout, 'read_header', return_value=(True, 152)), \
             patch.object(self.scan, '_wait', return_value=True):
            result = run.run()

        self.assertTrue(result.complete, result.message)
        self.assertEqual(1, run.tail_probes)
        drag.assert_called_once()
        self.assertGreater(drag.call_args.kwargs['distance'], 0)
        self.assertLessEqual(drag.call_args.kwargs['distance'], 2 * layout.ROW_PITCH)
        self.assertEqual('tail_probe', run.frames[-1]['movement']['strategy'])
        self.assertEqual([], run.frames[-1]['movement']['corrections'])
        self.assertEqual(151, run.boundary)
        self.assertEqual(set(range(151)), set(run.slots))
        self.assertEqual(151, len(run.records))
        self.assertEqual(151, result.online)
        self.assertEqual(152, result.total)
        self.assertEqual(previous, self.db.get_complete_roster())


if __name__ == '__main__':
    unittest.main()

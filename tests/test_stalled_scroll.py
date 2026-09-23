"""Stalled drags recover with bounded inputs and retain failure evidence."""
from tests import test_regressions as _baseline

import atexit
import json
import logging
from unittest.mock import patch
import unittest

import numpy as np

from app import capture, scroller

atexit.register(logging.shutdown)


class StalledScrollTests(unittest.TestCase):
    setUp = _baseline.FastScanRegressions.setUp
    simulation = _baseline.FastScanRegressions.simulation

    def controlled_scan(self, *, primary_moves=False, thumb_moves=False,
                        short_moves=False, delayed_frame=False, prepare=True):
        """Only the explicitly enabled synthetic inputs change the list offset."""
        state = self.simulation(count=30, drag_overshoot=0)
        state.update(primary_inputs=[], thumb_inputs=[], short_inputs=[], post_input_frames=0)
        render = capture.capture_region.side_effect
        maximum = 116 + 29 * self.layout.ROW_PITCH + self.layout.ROW_HEIGHT - self.layout.VIEW[3]

        def move(distance):
            state['offset'] = max(0., min(maximum, state['offset'] + distance))

        def primary(*args, **kwargs):
            distance = kwargs['distance']
            state['primary_inputs'].append(distance)
            if primary_moves and not delayed_frame:
                move(distance)
            return True

        def client(rect, start, end, **kwargs):
            if start[0] == self.layout.DRAG_X:
                state['short_inputs'].append((start, end))
                if short_moves:
                    move(start[1] - end[1])
            else:
                state['thumb_inputs'].append((start, end))
                if thumb_moves:
                    thumb = self.layout.scrollbar(state['frame'])
                    travel = self.layout.TRACK_BOTTOM - self.layout.TRACK_TOP - (thumb.bottom - thumb.top)
                    extent = 30 * self.layout.ROW_PITCH - (self.layout.VIEW[3] - self.layout.VIEW[1])
                    move((end[1] - start[1]) * extent / travel)
            return True

        def frame(*args, **kwargs):
            if state['primary_inputs']:
                state['post_input_frames'] += 1
                if delayed_frame and state['post_input_frames'] == 2:
                    move(state['primary_inputs'][0])
            return render(*args, **kwargs)

        scroller.drag_six_rows.side_effect = primary
        scroller.drag_client.side_effect = client
        capture.capture_region.side_effect = frame
        run = self.fast.FastScan(self.scan)
        run.drag_errors = [-5.] * 4
        run.drag_bias = -5.
        if not prepare:
            return run, state
        with patch.object(self.scan, '_wait', return_value=True):
            rect, before = run.home()
            run.add_page(before, self.layout.read_page(before))
        return run, state, rect, before

    def assert_progress(self, run, after):
        prior_last = max(run.slots)
        run.add_page(after, self.layout.read_page(after))
        self.assertGreater(max(run.slots), prior_last)
        self.assertEqual(set(range(max(run.slots) + 1)), set(run.slots))

    def assert_failure_evidence(self, run):
        directory = run.evidence
        files = sorted(directory.glob('movement-failed-*.png'))
        self.assertEqual(['movement-failed-after.png', 'movement-failed-before.png'], [file.name for file in files])
        for file in files:
            self.assertEqual((1080, 1920, 3), self.fast._decode(file.read_bytes()).shape)
        trace = directory / 'movement.jsonl'
        self.assertTrue(trace.is_file())
        records = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()]
        self.assertTrue(records)
        return records

    def test_delayed_frame_is_remeasured_without_another_drag(self):
        run, state, rect, before = self.controlled_scan(delayed_frame=True)
        with patch.object(self.scan, '_wait', return_value=True):
            _, after, movement = run.advance(rect, before)
        self.assertEqual(1, len(state['primary_inputs']))
        self.assertEqual([], state['thumb_inputs'])
        self.assertEqual([], state['short_inputs'])
        self.assertFalse(movement['thumb_fallback'])
        self.assertFalse(movement['card_fallback'])
        self.assert_progress(run, after)

    def test_one_scrollbar_fallback_recovers_without_card_retry_or_bias_learning(self):
        run, state, rect, before = self.controlled_scan(thumb_moves=True)
        with patch.object(self.scan, '_wait', return_value=True):
            _, after, movement = run.advance(rect, before)
        self.assertEqual((1, 1, 0), tuple(len(state[key]) for key in ('primary_inputs', 'thumb_inputs', 'short_inputs')))
        self.assertTrue(movement['thumb_fallback'])
        self.assertFalse(movement['card_fallback'])
        self.assertEqual(-5., run.drag_bias)
        self.assertEqual([-5.] * 4, run.drag_errors)
        self.assert_progress(run, after)

    def test_missing_scrollbar_recovers_by_short_drag_inside_a_complete_card(self):
        run, state, rect, before = self.controlled_scan(short_moves=True)
        last_band = self.layout.bands(before)[-1]
        with patch.object(self.scan, '_wait', return_value=True), \
                patch.object(self.layout, 'scrollbar', return_value=None):
            _, after, movement = run.advance(rect, before)
        self.assertEqual((1, 0, 1), tuple(len(state[key]) for key in ('primary_inputs', 'thumb_inputs', 'short_inputs')))
        start, end = state['short_inputs'][0]
        self.assertEqual(self.layout.DRAG_X, start[0])
        self.assertLess(start[1], last_band.bottom)
        self.assertGreater(start[1], last_band.top)
        self.assertGreater(start[1] - end[1], 0)
        self.assertLessEqual(start[1] - end[1], 2 * self.layout.ROW_PITCH + 1e-6)
        self.assertTrue(movement['card_fallback'])
        self.assertFalse(movement['thumb_fallback'])
        self.assertEqual([-5.] * 4, run.drag_errors)
        self.assert_progress(run, after)

    def test_ineffective_scrollbar_gets_only_one_short_forward_retry(self):
        run, state, rect, before = self.controlled_scan(short_moves=True)
        with patch.object(self.scan, '_wait', return_value=True):
            _, after, movement = run.advance(rect, before)
        self.assertEqual((1, 1, 1), tuple(len(state[key]) for key in ('primary_inputs', 'thumb_inputs', 'short_inputs')))
        self.assertTrue(movement['thumb_fallback'])
        self.assertTrue(movement['card_fallback'])
        self.assertEqual([], movement['corrections'])
        self.assertEqual(-5., run.drag_bias)
        self.assert_progress(run, after)

    def test_persistent_stall_stops_without_writing_roster_and_forces_two_evidence_images(self):
        self.db.add_friend('111111111', '已保存好友', 60, None, 'offline', _baseline.T0)
        original = self.db.get_all_friends()
        run, state = self.controlled_scan(prepare=False)
        self.assertFalse(_baseline.CONFIG.save_scan_evidence)
        with patch.object(self.scan, '_wait', return_value=True):
            result = run.run()
        self.assertFalse(result.complete)
        self.assertEqual(original, self.db.get_all_friends())
        self.assertEqual(1, state['reads'])
        self.assertEqual((1, 1, 1), tuple(len(state[key]) for key in ('primary_inputs', 'thumb_inputs', 'short_inputs')))
        self.assertEqual([-5.] * 4, run.drag_errors)
        records = self.assert_failure_evidence(run)
        self.assertFalse(records[-1]['complete'])
        self.assertEqual(state['post_input_frames'], len(records[-1]['samples']))
        self.assertTrue(all({'pixels', 'correlation', 'old_scrollbar', 'new_scrollbar'} <= sample.keys()
                            for sample in records[-1]['samples']))
        np.testing.assert_array_equal(self.fast._decode((run.evidence / 'movement-failed-after.png').read_bytes()),
                                      state['frame'])

    def test_bottom_scrollbar_stops_without_any_fallback_input(self):
        run, state, rect, before = self.controlled_scan()
        bottom = self.layout.Scrollbar(self.layout.TRACK_BOTTOM - 54, self.layout.TRACK_BOTTOM)
        with patch.object(self.scan, '_wait', return_value=True), \
                patch.object(self.layout, 'scrollbar', return_value=bottom):
            with self.assertRaises(self.fast.ScanIncomplete):
                run.advance(rect, before)
        self.assertEqual((1, 0, 0), tuple(len(state[key]) for key in ('primary_inputs', 'thumb_inputs', 'short_inputs')))
        self.assertEqual([], self.db.get_all_friends())

    def test_primary_motion_misreported_as_zero_does_not_trigger_another_input(self):
        run, state, rect, before = self.controlled_scan(primary_moves=True)
        with patch.object(self.scan, '_wait', return_value=True), \
                patch.object(self.layout, 'scroll_displacement', return_value=(0., .99)):
            with self.assertRaises(self.fast.ScanIncomplete):
                run.advance(rect, before)
        self.assertGreater(state['offset'], 0)
        self.assertEqual((1, 0, 0), tuple(len(state[key]) for key in ('primary_inputs', 'thumb_inputs', 'short_inputs')))
        self.assert_failure_evidence(run)
        np.testing.assert_array_equal(self.fast._decode((run.evidence / 'movement-failed-after.png').read_bytes()),
                                      state['frame'])

    def test_scrollbar_motion_misreported_as_zero_requires_fresh_stillness_proof(self):
        run, state, rect, before = self.controlled_scan(thumb_moves=True)
        with patch.object(self.scan, '_wait', return_value=True), \
                patch.object(self.layout, 'scroll_displacement', return_value=(0., .99)):
            with self.assertRaises(self.fast.ScanIncomplete):
                run.advance(rect, before)
        self.assertGreater(state['offset'], 0)
        self.assertEqual((1, 1, 0), tuple(len(state[key]) for key in ('primary_inputs', 'thumb_inputs', 'short_inputs')))
        self.assert_failure_evidence(run)

    def test_stop_during_settle_prevents_fallback_inputs(self):
        run, state, rect, before = self.controlled_scan()
        with patch.object(self.scan, '_wait', return_value=False):
            with self.assertRaises(self.fast.ScanIncomplete):
                run.advance(rect, before)
        self.assertEqual((1, 0, 0), tuple(len(state[key]) for key in ('primary_inputs', 'thumb_inputs', 'short_inputs')))
        self.assertEqual([], self.db.get_all_friends())
        self.assert_failure_evidence(run)

    def test_lost_focus_after_primary_drag_prevents_fallback_inputs(self):
        run, state, rect, before = self.controlled_scan()
        self.scan._navigation_error = '游戏失去焦点'
        with patch.object(self.scan, '_wait_for_game_foreground',
                          side_effect=lambda **kwargs: not state['primary_inputs']):
            with self.assertRaises(self.fast.ScanIncomplete):
                run.advance(rect, before)
        self.assertEqual((1, 0, 0), tuple(len(state[key]) for key in ('primary_inputs', 'thumb_inputs', 'short_inputs')))
        self.assertEqual([], self.db.get_all_friends())


class StationaryViewTests(unittest.TestCase):
    setUp = _baseline.FastScanRegressions.setUp
    simulation = _baseline.FastScanRegressions.simulation

    def test_same_card_content_is_stationary_without_a_visible_scrollbar(self):
        self.simulation(count=30, drag_overshoot=0)
        before = capture.capture_region(0, 0, 1920, 1080)
        with patch.object(self.layout, 'scrollbar', return_value=None):
            self.assertTrue(self.layout.stationary_view(before, before.copy(), interface_mode='normal'))

    def test_different_cards_at_same_row_coordinates_are_not_stationary(self):
        state = self.simulation(count=30, drag_overshoot=0)
        before = capture.capture_region(0, 0, 1920, 1080)
        state['offset'] = self.layout.ROW_PITCH
        after = capture.capture_region(0, 0, 1920, 1080)
        with patch.object(self.layout, 'scrollbar', return_value=None):
            self.assertFalse(self.layout.stationary_view(before, after, interface_mode='normal'))

    def test_moved_scrollbar_contradicts_identical_card_pixels(self):
        self.simulation(count=30, drag_overshoot=0)
        before = capture.capture_region(0, 0, 1920, 1080)
        after = before.copy()
        def scrollbar(image, **kwargs):
            return self.layout.Scrollbar(118, 172) if image is before else self.layout.Scrollbar(150, 204)
        with patch.object(self.layout, 'scrollbar', side_effect=scrollbar):
            self.assertFalse(self.layout.stationary_view(before, after, interface_mode='normal'))

    def test_blank_card_fields_cannot_prove_stillness(self):
        image = np.full((1080, 1920, 3), 205, dtype=np.uint8)
        with patch.object(self.layout, 'scrollbar', return_value=None):
            self.assertFalse(self.layout.stationary_view(image, image.copy(), interface_mode='normal'))


if __name__ == '__main__':
    unittest.main()

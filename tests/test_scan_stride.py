"""Measured scan strides retain continuity without filling every page to six rows."""
from tests import test_regressions as _baseline

import atexit
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import friend_screen

atexit.register(logging.shutdown)


class ScanStrideTests(unittest.TestCase):
    # Borrow the isolated image/drag simulator without inheriting unrelated tests.
    setUp = _baseline.FastScanRegressions.setUp
    simulation = _baseline.FastScanRegressions.simulation

    def assert_complete_roster(self, result, run, state, count=150):
        self.assertTrue(result.complete, result.message)
        self.assertEqual(count, result.total)
        self.assertEqual(set(range(count)), set(run.slots))
        friends = self.db.get_display_friends()
        self.assertEqual(count, len({friend['id'] for friend in friends}))
        self.assertEqual({'好友%03d' % index for index in range(count)},
                         {friend['nickname'] for friend in friends})
        self.assertTrue(state['commands'])
        self.assertTrue(all(30 <= distance <= 780 for distance in state['commands']))

    def test_stable_stride_scans_150_friends_without_reverse_drags(self):
        # drag_overshoot=0 means the image motion actually follows each command.
        state = self.simulation(count=150, drag_overshoot=0)
        run = self.fast.FastScan(self.scan)
        with patch.object(self.scan, '_wait', return_value=True):
            result = run.run()
        self.assert_complete_roster(result, run, state)
        self.assertEqual(0, state['corrections'])
        self.assertLessEqual(state['reads'], 32)
        # Each ordinary page retains the last previously read row, giving a
        # visual overlap; bottom clamping may retain more than one old row.
        for previous, current in zip(run.frames, run.frames[1:]):
            self.assertIn(previous['indices'][-1], current['indices'])
        self.assertLess(state['commands'][0], 5.5 * self.layout.ROW_PITCH)

    def test_single_large_underdrag_does_not_shift_stable_median_compensation(self):
        self.simulation(count=30, drag_overshoot=-21)
        run = self.fast.FastScan(self.scan)
        run.drag_errors = [-5.] * 4
        run.drag_bias = -5.
        with patch.object(self.scan, '_wait', return_value=True):
            rect, before = run.home()
            run.add_page(before, self.layout.read_page(before))
            _, after, movement = run.advance(rect, before)
            run.add_page(after, self.layout.read_page(after))
        self.assertGreaterEqual(movement['initial']['correlation'], .85)
        self.assertAlmostEqual(movement['initial']['pixels'] - movement['commanded_pixels'], -21, delta=1)
        self.assertEqual(5, len(run.drag_errors))
        self.assertEqual([-5.] * 4, run.drag_errors[:-1])
        self.assertAlmostEqual(run.drag_errors[-1], -21, delta=1)
        self.assertEqual(-5., run.drag_bias)
        self.assertEqual(-5., movement['drag_bias'])
        self.assertEqual([], movement['corrections'])
        self.assertEqual(set(range(max(run.slots) + 1)), set(run.slots))

    def test_fifty_pixel_overshoot_keeps_all_150_friends_with_bounded_correction(self):
        state = self.simulation(count=150, drag_overshoot=50)
        run = self.fast.FastScan(self.scan)
        with patch.object(self.scan, '_wait', return_value=True):
            result = run.run()
        self.assert_complete_roster(result, run, state)
        self.assertLessEqual(state['corrections'], 2)
        self.assertLessEqual(state['reads'], 35)

    def test_ninety_pixel_overshoot_keeps_all_150_friends_with_bounded_correction(self):
        state = self.simulation(count=150, drag_overshoot=90)
        run = self.fast.FastScan(self.scan)
        with patch.object(self.scan, '_wait', return_value=True):
            result = run.run()
        self.assert_complete_roster(result, run, state)
        self.assertLessEqual(state['corrections'], 3)
        self.assertLessEqual(state['reads'], 35)

    def test_three_four_or_five_complete_contiguous_rows_need_no_reverse_drag(self):
        for visible_count in (5, 4, 3):
            with self.subTest(complete_rows=visible_count):
                state = self.simulation(count=150, drag_overshoot=0)
                run = self.fast.FastScan(self.scan)
                with patch.object(self.scan, '_wait', return_value=True):
                    rect, before = run.home()
                    run.add_page(before, self.layout.read_page(before))
                    prior_last = max(run.slots)
                    real_bands = self.layout.bands

                    def partial_bands(image, *args, **kwargs):
                        bands = real_bands(image, *args, **kwargs)
                        return bands if image is before else bands[:visible_count]

                    with patch.object(self.layout, 'bands', side_effect=partial_bands):
                        _, after, movement = run.advance(rect, before)
                    page = self.layout.read_page(after)
                    page.observations = page.observations[:visible_count]
                    page.bands = page.bands[:visible_count]
                    page.entries = page.entries[:visible_count]
                    indices = run.add_page(after, page)
                self.assertEqual(visible_count, len(indices))
                self.assertIn(prior_last, indices)
                self.assertGreater(max(run.slots), prior_last)
                self.assertEqual(set(range(max(run.slots) + 1)), set(run.slots))
                self.assertEqual([], movement['corrections'])
                self.assertEqual(0, state['corrections'])
                self.assertLessEqual(movement['commanded_pixels'], 780)

    def rejected_movement(self, *, pixels, correlation, after_thumb_top=250, first_top=116):
        """Hold a bad measured frame unchanged through all bounded retries."""
        run = self.fast.FastScan(self.scan)
        run.expected, run.origin = 150, 116
        run.slots = dict.fromkeys(range(6))
        before, after = object(), object()
        rect = (0, 0, 1920, 1080)
        before_bands = [friend_screen.CardBand(round(116 + index * self.layout.ROW_PITCH),
                                              round(116 + index * self.layout.ROW_PITCH + self.layout.ROW_HEIGHT))
                        for index in range(6)]
        after_bands = [friend_screen.CardBand(round(first_top + index * self.layout.ROW_PITCH),
                                             round(first_top + index * self.layout.ROW_PITCH + self.layout.ROW_HEIGHT))
                       for index in range(5)]

        def bands(image, *args, **kwargs):
            return before_bands if image is before else after_bands

        def scrollbar(image, *args, **kwargs):
            top = 118 if image is before else after_thumb_top
            return SimpleNamespace(top=top, bottom=top + 54, at_bottom=False)

        with patch.object(run, 'drag') as drag, \
                patch.object(run, 'shot', return_value=(rect, after)), \
                patch.object(run, 'save_image'), \
                patch.object(self.scan, '_wait', return_value=True), \
                patch.object(self.layout, 'bands', side_effect=bands), \
                patch.object(self.layout, 'scrollbar', side_effect=scrollbar), \
                patch.object(self.layout, 'scrollbar_displacement', return_value=None), \
                patch.object(self.layout, 'scroll_displacement', return_value=(pixels, correlation)), \
                patch.object(self.layout, 'is_friend_list', return_value=True):
            with self.assertRaises(self.fast.ScanIncomplete) as raised:
                run.advance(rect, before)
        self.assertEqual(set(range(6)), set(run.slots))
        self.assertEqual([], self.db.get_all_friends())
        self.assertLessEqual(drag.call_count, 3)
        self.assertLessEqual(drag.call_args_list[0].kwargs['distance'], 780)
        return str(raised.exception), drag

    def test_real_unseen_row_gap_stops_after_bounded_recovery(self):
        message, drag = self.rejected_movement(pixels=9 * self.layout.ROW_PITCH, correlation=.99)
        self.assertIn('未跳过', message)
        self.assertGreater(drag.call_count, 1)

    def test_reverse_motion_is_rejected_without_accepting_a_new_page(self):
        message, drag = self.rejected_movement(pixels=-self.layout.ROW_PITCH,
                                                correlation=.99, after_thumb_top=90)
        self.assertIn('方向', message)
        self.assertEqual(1, drag.call_count)

    def test_low_correlation_never_accepts_apparently_contiguous_rows(self):
        message, drag = self.rejected_movement(pixels=5 * self.layout.ROW_PITCH, correlation=.4)
        self.assertIn('相关度', message)
        self.assertGreater(drag.call_count, 1)


if __name__ == '__main__':
    unittest.main()

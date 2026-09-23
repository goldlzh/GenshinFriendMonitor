"""A three-pixel scrollbar tolerance must not hide the last friend of 152."""
from tests import test_regressions as baseline
from tests.private_fixtures import private_fixture

import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np

from app import fast_layout as layout, friend_screen
from app.fast_scan import FastScan, ScanIncomplete
from app.parser import FriendEntry


class NewFriendTailTests(unittest.TestCase):
    setUp = baseline.FastScanRegressions.setUp

    def images(self):
        with np.load(private_fixture('new-friend-tail.npz'), allow_pickle=False) as data:
            return data['before'], data['after']

    def page(self, frame, names, total=152):
        page = friend_screen.FriendPage(confirmed=True, expected_total=total)
        for band, name in zip(layout.bands(frame, interface_mode='qianxing'), names):
            entry = FriendEntry(name, 'offline', '31天前', None, 194, band.center, .99,
                                interface_mode='qianxing')
            page.observations.append(layout.CardObservation(band, entry,
                layout.nickname_crop(frame, band, 'qianxing'),
                layout.crop(frame, (158, band.top+10, 232, band.top+105))))
            page.bands.append(band)
            page.entries.append(entry)
        return page

    def tail_case(self):
        before, after = self.images()
        first = self.page(before, ['测试甲', '<^_^>', '测试乙', '测试丙', '测试丁', '测试戊'])
        last = self.page(after, ['<^_^>', '测试乙', '测试丙', '测试丁', '测试戊', '测试己'])
        self.scan.interface_mode = 'qianxing'
        run = FastScan(self.scan)
        run.expected, run.origin = 152, 116
        run.offset = 145*layout.ROW_PITCH+run.origin-first.bands[0].top
        # Earlier rows are already independently verified. Only the real final
        # pair is replayed here; images come from adjacent live passes, not a
        # newly executed game drag (see fixture metadata).
        run.slots = {i: layout.CardObservation(first.bands[0], baseline.entry('earlier%03d' % i),
            first.observations[0].name_image, first.observations[0].avatar_image) for i in range(145)}
        rect = (0, 0, 1920, 1080)
        return run, rect, before, after, first, last

    def test_real_thumb_is_near_bottom_while_last_friend_is_only_partial(self):
        before, after = self.images()
        self.assertTrue(layout.scrollbar(before).at_bottom)
        self.assertEqual(941, layout.scrollbar(before).bottom)
        self.assertEqual(944, layout.scrollbar(after).bottom)
        dy, score = layout.scroll_displacement(before, after,
            expected=layout.scrollbar_displacement(layout.scrollbar(before), layout.scrollbar(after), 152),
            interface_mode='qianxing')
        self.assertAlmostEqual(dy, 107, delta=1)
        self.assertGreater(score, .90)

    def test_real_tail_gets_one_forward_probe_and_completes_152_without_reverse(self):
        run, rect, before, after, first, last = self.tail_case()
        with patch.object(run, 'home', return_value=(rect, before)), \
             patch.object(run, 'shot', return_value=(rect, after)), \
             patch.object(run, 'drag') as drag, \
             patch.object(layout, 'read_page', side_effect=[first, last]), \
             patch.object(self.scan, '_wait', return_value=True):
            result = run.run(commit=False)
        self.assertTrue(result.complete, result.message)
        self.assertEqual(set(range(152)), set(run.slots))
        self.assertEqual('测试己', run.slots[151].entry.nickname)
        self.assertEqual(1, run.tail_probes)
        drag.assert_called_once()
        self.assertGreater(drag.call_args.kwargs['distance'], 0)
        self.assertLessEqual(drag.call_args.kwargs['distance'], 2*layout.ROW_PITCH)
        self.assertEqual('tail_probe', run.frames[-1]['movement']['strategy'])
        self.assertEqual([], self.db.get_all_friends())

    def test_stationary_bottom_stops_without_repeated_ocr_or_partial_commit(self):
        run, rect, before, _, first, _ = self.tail_case()
        with patch.object(run, 'home', return_value=(rect, before)), \
             patch.object(run, 'shot', return_value=(rect, before)), \
             patch.object(run, 'drag') as drag, \
             patch.object(layout, 'read_page', return_value=first) as read, \
             patch.object(self.scan, '_wait', return_value=True):
            result = run.run()
        self.assertFalse(result.complete)
        self.assertIn('未向下移动', result.message)
        read.assert_called_once()
        drag.assert_called_once()
        self.assertEqual(151, len(run.slots))
        self.assertEqual([], self.db.get_all_friends())
        audit = json.loads((run.evidence/'movement.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertFalse(audit['complete'])
        self.assertTrue(audit['tail_probe'])

    def test_near_bottom_probe_is_bounded_even_if_detector_stays_near_bottom(self):
        run, rect, before, _, _, _ = self.tail_case()
        with patch.object(run, 'advance', return_value=(rect, before, {})) as advance:
            run.advance_page(rect, before)
            with self.assertRaisesRegex(ScanIncomplete, '未继续重复拖动'):
                run.advance_page(rect, before)
        advance.assert_called_once()

    def test_changed_header_during_tail_probe_still_rejects_the_whole_draft(self):
        run, rect, before, after, first, last = self.tail_case()
        last.expected_total = 153
        with patch.object(run, 'home', return_value=(rect, before)), \
             patch.object(run, 'shot', return_value=(rect, after)), \
             patch.object(run, 'drag'), \
             patch.object(layout, 'read_page', side_effect=[first, last]), \
             patch.object(self.scan, '_wait', return_value=True):
            result = run.run()
        self.assertFalse(result.complete)
        self.assertEqual('count_changed', run.failure_reason)
        self.assertEqual([], self.db.get_all_friends())

    def test_stop_before_tail_probe_never_sends_input(self):
        run, rect, before, _, _, _ = self.tail_case()
        self.scan._stop.set()
        with patch.object(run, 'home', return_value=(rect, before)), \
             patch.object(run, 'drag') as drag:
            result = run.run()
        self.assertFalse(result.complete)
        drag.assert_not_called()

    def test_conflicting_overlap_retains_actual_frame_before_record_page(self):
        run, rect, before, after, first, last = self.tail_case()
        last.observations[0].avatar_image = np.zeros_like(last.observations[0].avatar_image)
        with patch.object(run, 'home', return_value=(rect, before)), \
             patch.object(run, 'shot', return_value=(rect, after)), \
             patch.object(run, 'drag'), \
             patch.object(layout, 'read_page', side_effect=[first, last]), \
             patch.object(self.scan, '_wait', return_value=True):
            result = run.run()
        self.assertFalse(result.complete)
        self.assertEqual('reordered', run.failure_reason)
        self.assertTrue((run.evidence/'overlap-failed.png').is_file())
        self.assertTrue((run.evidence/'overlap-previous-name.png').is_file())
        audit = json.loads((run.evidence/'overlap-failed.json').read_text(encoding='utf-8'))
        self.assertEqual(147, audit['position'])
        self.assertLess(audit['avatar_similarity'], .80)
        self.assertEqual([], self.db.get_all_friends())


if __name__ == '__main__':
    unittest.main()

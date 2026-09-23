"""Fresh UID fields replace one verified page without mixing frames or inputs."""
from tests import test_regressions as baseline

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import json
import tempfile
import time
import unittest

import cv2
import numpy as np

from app import fast_layout as layout, friend_screen, parser
from app.account_identity import AccountIdentityError
from app.fast_scan import FastScan, RosterChanged, ScanIncomplete
from app.scan_workflow import UidPass


RECT = (0, 0, 1920, 1080)


def frame(shift=0, changed_name=False):
    image = np.full((1080, 1920, 3), 60, np.uint8)
    for index in range(6):
        top = round(116 + index*layout.ROW_PITCH) + shift
        image[top:top+115, 136:1788] = 212
        image[top+10:top+105, 158:232] = np.random.default_rng(index).integers(
            0, 255, (95, 74, 3), dtype=np.uint8)
        name = 'CHANGED' if changed_name and index == 0 else 'friend%d' % index
        cv2.putText(image, name, (282, top+45), cv2.FONT_HERSHEY_SIMPLEX,
                    .85, (45, 45, 45), 2, cv2.LINE_AA)
    return image


def page(image, missing=()):
    result = friend_screen.FriendPage(confirmed=True, expected_total=151)
    for index, band in enumerate(layout.bands(image, 'qianxing')):
        entry = parser.FriendEntry('friend%d' % index, 'offline', '31天前', None,
            194, int(band.center), .99, interface_mode='qianxing')
        if index in missing:
            entry = None
        obs = layout.CardObservation(band, entry, layout.nickname_crop(image, band, 'qianxing'),
            layout.crop(image, (158, band.top+10, 232, band.top+105)),
            fields=[('friend%d' % index, .99), ('--', 1.), ('31天前最后登录', .99)],
            issue='missing level' if entry is None else '',
            level_audit={'kind': 'unreadable' if entry is None else 'unset'})
        result.bands.append(band)
        result.observations.append(obs)
        if entry is not None:
            result.entries.append(entry)
    return result


class UidFieldRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=baseline.WORK)
        self.addCleanup(self.work.cleanup)
        self.scan = UidPass.__new__(UidPass)
        self.scan.interface_mode = 'qianxing'
        self.scan.expected = 151
        self.scan.origin = 116
        self.scan.offset = 0.
        self.scan.frame_count = 3
        self.scan.started = time.perf_counter()
        self.scan.evidence = Path(self.work.name)
        self.scan.scanner = SimpleNamespace(_check_pause=MagicMock(return_value=True), _say=MagicMock(),
                                            evidence_root=Path(self.work.name))
        self.scan.check = MagicMock()
        self.scan.park_pointer = MagicMock()
        self.scan.wait = MagicMock()
        self.scan.shot = MagicMock()
        self.scan.slots = {}
        self.scan.uids = {}
        self.original = frame()
        self.original_page = page(self.original, (2,))
        self.header = patch.object(layout, 'read_header', return_value=(True, 151))
        self.header.start()
        self.addCleanup(self.header.stop)

    def audit(self):
        return [json.loads(line) for line in
                (self.scan.evidence/'field-recovery.jsonl').read_text(encoding='utf-8').splitlines()]

    def assert_original_unchanged(self):
        self.assertIsNone(self.original_page.observations[2].entry)
        self.assertEqual('missing level', self.original_page.observations[2].issue)
        self.assertEqual({}, self.scan.slots)
        self.assertEqual(0., self.scan.offset)

    def test_complete_page_needs_no_new_capture_wait_or_pointer_movement(self):
        complete = page(self.original)
        result = self.scan.prepare_page(RECT, self.original, complete)
        self.assertIs(complete, result[2])
        self.scan.check.assert_not_called()
        self.scan.shot.assert_not_called()
        self.scan.wait.assert_not_called()
        self.scan.park_pointer.assert_not_called()
        self.assertEqual([], list(self.scan.evidence.iterdir()))

    def test_fresh_whole_page_recovers_fields_and_uid_collection_continues(self):
        fresh = frame()
        complete = page(fresh)
        self.scan.shot.return_value = RECT, fresh
        with patch.object(layout, 'read_page', return_value=complete):
            rect, chosen_frame, chosen_page = self.scan.prepare_page(RECT, self.original, self.original_page)
        self.assertIs(fresh, chosen_frame)
        self.assertIs(complete, chosen_page)
        self.assert_original_unchanged()
        indices = self.scan.add_page(chosen_frame, chosen_page)
        with patch.object(self.scan, 'verify_visible', return_value=(RECT, fresh, complete.bands)), \
                patch.object(self.scan, 'read_uid', side_effect=['%09d' % (111111111+i) for i in range(6)]) as reader:
            self.scan.after_page(rect, chosen_frame, chosen_page, indices)
        self.assertEqual(6, reader.call_count)
        self.assertEqual(6, len(self.scan.uids))
        self.assertTrue(all(obs.entry is not None for obs in self.scan.slots.values()))
        self.assertEqual(1, self.scan.shot.call_count)
        self.assertTrue(self.audit()[0]['accepted'])

    def test_two_incomplete_new_pages_are_never_spliced_together(self):
        one, two = frame(), frame()
        self.scan.shot.side_effect = [(RECT, one), (RECT, two)]
        with patch.object(layout, 'read_page', side_effect=[page(one, (3,)), page(two, (2,))]) as reader:
            with self.assertRaisesRegex(ScanIncomplete, '两次新帧'):
                self.scan.prepare_page(RECT, self.original, self.original_page)
        self.assertEqual(2, self.scan.shot.call_count)
        self.assertEqual(2, reader.call_count)
        self.assert_original_unchanged()
        self.assertEqual(2, len(self.audit()[0]['attempts']))
        self.assertFalse(self.audit()[0]['accepted'])
        self.assertEqual(2, len(list(self.scan.evidence.glob('field-recovery-*.png'))))

    def test_changed_name_is_not_read_as_the_original_page(self):
        other = frame(changed_name=True)
        self.scan.shot.return_value = RECT, other
        with patch.object(layout, 'read_page') as reader:
            with self.assertRaises(ScanIncomplete):
                self.scan.prepare_page(RECT, self.original, self.original_page)
        reader.assert_not_called()
        self.assertEqual(2, self.scan.shot.call_count)
        self.assert_original_unchanged()

    def test_count_change_stops_after_first_capture(self):
        self.scan.shot.return_value = RECT, frame()
        with patch.object(layout, 'read_header', return_value=(True, 152)), \
                patch.object(layout, 'read_page') as reader:
            with self.assertRaises(RosterChanged):
                self.scan.prepare_page(RECT, self.original, self.original_page)
        reader.assert_not_called()
        self.assertEqual(1, self.scan.shot.call_count)
        self.assert_original_unchanged()

    def test_window_move_stops_without_ocr_or_state_changes(self):
        self.scan.shot.return_value = (1, 0, 1921, 1080), frame()
        with patch.object(layout, 'read_page') as reader:
            with self.assertRaisesRegex(ScanIncomplete, '窗口位置'):
                self.scan.prepare_page(RECT, self.original, self.original_page)
        reader.assert_not_called()
        self.assert_original_unchanged()

    def test_capture_account_or_foreground_guard_failure_preserves_original(self):
        for error in (AccountIdentityError('different account'), ScanIncomplete('foreground lost')):
            with self.subTest(error=str(error)):
                self.scan.shot.side_effect = error
                with self.assertRaises(type(error)):
                    self.scan.prepare_page(RECT, self.original, self.original_page)
                self.assert_original_unchanged()

    def test_stop_during_ocr_cannot_publish_recovered_fields(self):
        fresh = frame()
        self.scan.shot.return_value = RECT, fresh
        self.scan.check.side_effect = [None, ScanIncomplete('stopped')]
        with patch.object(layout, 'read_page', return_value=page(fresh)):
            with self.assertRaisesRegex(ScanIncomplete, 'stopped'):
                self.scan.prepare_page(RECT, self.original, self.original_page)
        self.assert_original_unchanged()
        self.assertFalse(self.audit()[0]['accepted'])

    def test_translated_page_needs_two_stable_frames_and_corrects_existing_origin(self):
        self.scan.offset = 6240.
        shifted = frame(12)
        self.scan.shot.side_effect = [(RECT, shifted), (RECT, shifted.copy())]
        with patch.object(layout, 'read_page', return_value=page(shifted)) as reader:
            _, chosen, restored = self.scan.prepare_page(RECT, self.original, self.original_page)
        self.assertEqual(2, self.scan.shot.call_count)
        self.assertEqual(1, reader.call_count)
        self.assertEqual(6228., self.scan.offset)
        self.assertEqual(list(range(50, 56)), self.scan.add_page(chosen, restored))
        self.assertEqual(12, restored.field_recovery_shift)
        self.assertEqual(6228., self.audit()[0]['offset_after'])

    def test_first_page_translation_establishes_new_origin_without_subtracting_offset(self):
        self.scan.origin = None
        shifted = frame(12)
        self.scan.shot.return_value = RECT, shifted
        with patch.object(layout, 'read_page', return_value=page(shifted)):
            _, chosen, restored = self.scan.prepare_page(RECT, self.original, self.original_page)
        self.assertEqual(0., self.scan.offset)
        self.assertEqual(list(range(6)), self.scan.add_page(chosen, restored))
        self.assertEqual(128, self.scan.origin)

    def test_still_moving_after_two_captures_never_adopts_page_or_offset(self):
        self.scan.shot.side_effect = [(RECT, frame(12)), (RECT, frame(20))]
        with patch.object(layout, 'read_page') as reader:
            with self.assertRaises(ScanIncomplete):
                self.scan.prepare_page(RECT, self.original, self.original_page)
        reader.assert_not_called()
        self.assert_original_unchanged()
        self.assertEqual(2, self.scan.shot.call_count)

    def test_message_callback_failure_cannot_publish_offset_or_recovered_page(self):
        self.scan.offset = 6240.
        shifted = frame(12)
        self.scan.shot.return_value = RECT, shifted
        self.scan.scanner._say.side_effect = RuntimeError('callback failed')
        with patch.object(layout, 'read_page', return_value=page(shifted)):
            with self.assertRaisesRegex(RuntimeError, 'callback failed'):
                self.scan.prepare_page(RECT, self.original, self.original_page)
        self.assertEqual(6240., self.scan.offset)
        self.assertEqual({}, self.scan.slots)
        self.assertIsNone(self.original_page.observations[2].entry)
        self.assertFalse(self.audit()[0]['accepted'])

    def test_normal_run_records_and_assigns_only_the_prepared_frame(self):
        fast = FastScan.__new__(FastScan)
        fast.interface_mode = 'qianxing'
        fresh = frame()
        complete = page(fresh)
        fast.scanner = self.scan.scanner
        fast.home = MagicMock(return_value=(RECT, self.original))
        fast.check = MagicMock()
        fast.prepare_page = MagicMock(return_value=(RECT, fresh, complete))
        fast.add_page = MagicMock(return_value=list(range(6)))
        fast.record_page = MagicMock()
        fast.after_page = MagicMock(return_value=(RECT, fresh))
        fast.expected = 6
        fast.slots = dict(enumerate(complete.observations))
        fast.finish = MagicMock(return_value='complete')
        with patch.object(layout, 'read_page', return_value=self.original_page):
            self.assertEqual('complete', fast.run(commit=False))
        self.assertIs(fresh, fast.add_page.call_args.args[0])
        self.assertIs(complete, fast.add_page.call_args.args[1])
        self.assertIs(fresh, fast.record_page.call_args.args[0])
        self.assertIs(complete, fast.record_page.call_args.args[1])

    def test_adopted_translation_is_reflected_in_recorded_movement(self):
        fast = FastScan.__new__(FastScan)
        fast.interface_mode = 'qianxing'
        fresh = frame(12)
        first, restored = page(self.original), page(fresh)
        restored.field_recovery_shift = 12
        fast.scanner = self.scan.scanner
        fast.home = MagicMock(return_value=(RECT, self.original))
        fast.check = MagicMock()
        fast.prepare_page = MagicMock(side_effect=[(RECT, self.original, first), (RECT, fresh, restored)])
        fast.expected, fast.slots = 12, {}

        def add(image, current):
            start = len(fast.slots)
            fast.slots.update({start+i:obs for i, obs in enumerate(current.observations)})
            return list(range(start, len(fast.slots)))

        fast.add_page = add
        fast.record_page = MagicMock()
        fast.after_page = MagicMock(side_effect=[(RECT, self.original), (RECT, fresh)])
        fast.advance = MagicMock(return_value=(RECT, fresh, {'pixels':624., 'correlation':.95}))
        fast.finish = MagicMock(return_value='complete')
        with patch.object(layout, 'read_page', side_effect=[first, self.original_page]), \
                patch.object(layout, 'scrollbar', return_value=None):
            self.assertEqual('complete', fast.run(commit=False))
        movement = fast.record_page.call_args_list[1].args[3]
        self.assertEqual(624., movement['measured_pixels'])
        self.assertEqual(612., movement['pixels'])
        self.assertEqual(12, movement['field_recovery_shift'])


if __name__ == '__main__':
    unittest.main()

"""Online-prefix collection with no game input and isolated databases."""
from tests import test_regressions as baseline
from tests.private_fixtures import private_fixture

from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from app import fast_layout as layout, friend_screen, parser
from app.database import Database
from app.fast_scan import FastScan, ScanIncomplete, _png
from app.online_scan import OnlineScanWorkflow, classify_action, read_actions, reusable_online_identities
from app.scan_workflow import records_from_observations
from app.scanner import Scanner


RECT = (0, 0, 1920, 1080)


def observation(index, row=0, status='online', mode='qianxing', pending=False):
    band = friend_screen.CardBand(round(116+row*layout.ROW_PITCH), round(116+row*layout.ROW_PITCH+115))
    name = np.full((57, 230, 3), 218, np.uint8)
    cv2.putText(name, 'FRIEND%d' % index, (12, 38), cv2.FONT_HERSHEY_SIMPLEX, .8,
                (42, 42, 42), 2, cv2.LINE_AA)
    avatar = np.random.default_rng(index).integers(0, 256, (95, 74, 3), dtype=np.uint8)
    entry = parser.FriendEntry('friend%d' % index, status, '今日内' if status == 'offline' else None,
        60 if mode == 'normal' else None, 194, int(band.center), .99,
        nickname_pending=pending, interface_mode=mode, qianxing_level=20 if mode == 'qianxing' else None)
    return layout.CardObservation(band, entry, name, avatar,
        fields=[(entry.nickname, .99), ('20', .99), ('空闲中' if status == 'online' else '今日内', .99)])


def profile(obs, uid='111111111', friend_id=1):
    return {'friend_id': friend_id, 'uid': uid, 'nickname': obs.entry.nickname,
            'nickname_pending': obs.entry.nickname_pending, 'level': obs.entry.level,
            'qianxing_level': obs.entry.qianxing_level, 'interface_mode': obs.entry.interface_mode,
            'name_image': _png(obs.name_image), 'avatar_image': _png(obs.avatar_image)}


def make_page(indices, states=None, total=8):
    states = states or ['online']*len(indices)
    observations = [observation(index, row, state) for row, (index, state) in enumerate(zip(indices, states))]
    page = friend_screen.FriendPage(confirmed=True, expected_total=total,
        observations=observations, entries=[o.entry for o in observations], bands=[o.band for o in observations])
    frame = np.zeros((1080, 1920, 3), np.uint8)
    frame[0, 0, 0] = indices[0] if indices else 0
    return frame, page


class OnlineClassificationTests(unittest.TestCase):
    def inspect(self, states, audit):
        with tempfile.TemporaryDirectory(dir=baseline.WORK) as work:
            scan = OnlineScanWorkflow.__new__(OnlineScanWorkflow)
            scan.evidence, scan.interface_mode = Path(work), 'qianxing'
            scan.frame_count, scan.started, scan.action_evidence = 0, time.perf_counter(), []
            scan.save_image = MagicMock()
            with patch('app.online_scan.read_actions', return_value=(states, audit)):
                result = scan.inspect_actions(np.zeros((20,20,3),np.uint8), MagicMock(), 'test')
            return result, scan.action_evidence[-1]

    def test_actual_supplied_action_crops_preserve_long_label_and_expose_decoration_noise(self):
        from app import ocr_engine
        real_reader = ocr_engine.recognize_lines
        with np.load(private_fixture('online-actions.npz')) as samples:
            for mode in ('normal', 'qianxing'):
                with self.subTest(mode=mode):
                    images = [samples['%s_%d_%s' % (mode, row, suffix)] for row in range(4)
                              for suffix in ('narrow_raw', 'narrow_padding', 'wide_raw', 'wide_padding', 'left')]
                    frame, page = make_page(range(4))
                    with patch('app.online_scan.ocr_engine.recognize_lines', side_effect=lambda _: real_reader(images)):
                        states, audit = read_actions(frame, page.observations, mode)
                    self.assertEqual(['online', 'online', 'offline', None], states)
                    (resolved, boundary), report = self.inspect(states, audit)
                    self.assertEqual(['online', 'online', 'offline', 'offline'], resolved)
                    self.assertEqual(2, boundary)
                    self.assertEqual('offline_tail_left_status', report['rows'][3]['reason'])
                    if mode == 'normal':
                        self.assertEqual('前往其大厅', audit[0]['wide_reads'][0][0])
                        self.assertNotEqual('前往其大厅', audit[0]['narrow_reads'][0][0])

    def test_actions_require_two_exact_reliable_reads_and_no_status_contradiction(self):
        for word in ('互动', '前往其大厅', '申请加入'):
            self.assertEqual('online', classify_action([(word, .99)]*2, 'online'))
            self.assertEqual('online', classify_action([(word, .99)]*2, None))
            self.assertIsNone(classify_action([(word, .99)]*2, 'offline'))
        self.assertEqual('offline', classify_action([('离线', .99)]*2, None))
        self.assertEqual('offline', classify_action([('', 0.)]*2, 'offline'))
        for reads, left in (([('', 0.)]*2, None), ([('', 0.)]*2, 'online'),
                            ([('互动', .89)]*2, 'online'), ([('互动', .99), ('离线', .99)], None),
                            ([('日互动', .99)]*2, 'online'), ([('离线', .99)]*2, 'online')):
            self.assertIsNone(classify_action(reads, left))

    def test_separate_width_hypotheses_keep_complete_long_text_and_ignore_icon_noise(self):
        frame, page = make_page(range(3), ['online', 'online', 'offline'])
        outputs = [('互动', .99)]*2+[('日互动', .99)]*2+[('空闲中', .99)]
        outputs += [('其大厅', .99)]*2+[('前往其大厅', .99)]*2+[('空闲中', .99)]
        outputs += [('', 0.)]*4+[('今日内', .99)]
        with patch('app.online_scan.ocr_engine.recognize_lines', return_value=outputs):
            states, audit = read_actions(frame, page.observations, 'qianxing')
        self.assertEqual(['online', 'online', 'offline'], states)
        self.assertEqual('前往其大厅', audit[1]['wide_reads'][0][0])

    def test_unknown_right_text_is_not_treated_as_empty_offline(self):
        frame, page = make_page([0], ['offline'])
        with patch('app.online_scan.ocr_engine.recognize_lines', return_value=[('', 0.)]*2+[('C', .48)]*2+[('今日内', .99)]):
            states, _ = read_actions(frame, page.observations, 'qianxing')
        self.assertEqual([None], states)

    def test_contradictory_short_and_long_actions_do_not_use_left_status_as_vote(self):
        frame, page = make_page([0])
        with patch('app.online_scan.ocr_engine.recognize_lines', return_value=[('离线', .99)]*2+
                   [('申请加入', .99)]*2+[('在线', .99)]):
            states, _ = read_actions(frame, page.observations, 'qianxing')
        self.assertEqual([None], states)

    def test_tail_requires_explicit_left_offline_and_rejects_any_reliable_online_action(self):
        for state, left, online_evidence in (('online', 'online', True),
                (None, None, False), (None, 'online', False), (None, 'offline', True)):
            with self.subTest(state=state, left=left, online=online_evidence):
                with self.assertRaises(ScanIncomplete):
                    self.inspect(['online', 'offline', state], [{}, {},
                        {'left_status': left, 'reliable_online_action': online_evidence}])

    def test_later_strict_offline_cannot_explain_earlier_unknown_action(self):
        with self.assertRaises(ScanIncomplete):
            self.inspect(['online', None, 'offline'], [{}, {'left_status': 'offline'}, {}])


class OnlineIdentityTests(unittest.TestCase):
    def test_unchanged_uid_requires_unique_strong_same_mode_name_and_avatar(self):
        obs = observation(1)
        old = profile(obs)
        self.assertEqual([old], reusable_online_identities([obs], [old]))
        cases = [dict(old, nickname='renamed'), dict(old, interface_mode='normal'),
                 dict(old, qianxing_level=19), dict(old, avatar_image=_png(observation(2).avatar_image))]
        for other in cases:
            self.assertEqual([None], reusable_online_identities([obs], [other]))
        self.assertEqual([None], reusable_online_identities([obs], [old, dict(old, uid='222222222', friend_id=2)]))
        self.assertEqual([None, None], reusable_online_identities([obs, obs], [old]))
        self.assertEqual([None], reusable_online_identities([obs], [old],
            [{'id': 2, 'uid': '222222222', 'nickname': obs.entry.nickname}]))

    def test_blank_fragment_or_added_character_cannot_reuse_uid(self):
        obs = observation(1, pending=True)
        old = profile(obs)
        blank = replace(obs, name_image=np.full_like(obs.name_image, 218))
        self.assertEqual([None], reusable_online_identities([blank], [profile(blank)]))
        self.assertEqual([None], reusable_online_identities([replace(obs, name_image=obs.name_image[:, :90])], [old]))
        changed = obs.name_image.copy()
        cv2.putText(changed, '.', (200, 38), cv2.FONT_HERSHEY_SIMPLEX, .8, (42, 42, 42), 2)
        self.assertEqual([None], reusable_online_identities([replace(obs, name_image=changed)], [old]))


class OnlineWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(dir=baseline.WORK)
        self.addCleanup(self.work.cleanup)
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.scanner = Scanner(self.db)
        self.scanner.interface_mode = 'qianxing'
        self.scanner._hwnd = None
        self.scanner._say = MagicMock()
        self.root = Path(self.work.name)
        self.config = patch.object(baseline.CONFIG, 'log_dir', str(self.root))
        self.config.start()
        self.addCleanup(self.config.stop)
        self.font = patch('app.fast_scan.nickname_ocr.configure_game_font')
        self.font.start()
        self.addCleanup(self.font.stop)

    def seed(self, total=8):
        observations = [observation(i) for i in range(total)]
        records = records_from_observations(observations)
        for index, record in enumerate(records):
            record['uid'] = '%09d' % (100000000+index)
        self.db.apply_verified_scan(records, baseline.T0, 0., str(self.root), identity_complete=True)
        return self.db.get_complete_roster()

    def prepare(self, pages):
        scan = OnlineScanWorkflow(self.scanner)
        scan.top_verified = True
        scan.check = MagicMock()
        scan.home = MagicMock(return_value=(RECT, pages[0][0]))
        scan.shot = MagicMock(return_value=(RECT, pages[-1][0]))
        scan.prepare_page = MagicMock(side_effect=lambda rect, frame, page: (rect, frame, page))
        scan.recover_fields = MagicMock()
        by_marker = {int(frame[0,0,0]): page for frame, page in pages}
        by_identity = {id(page.observations[0].name_image): (frame, page) for frame, page in pages if page.observations}
        def visible(page):
            frame, _ = by_identity[id(page.observations[0].name_image)]
            return RECT, frame, page.bands
        scan.verify_visible = MagicMock(side_effect=visible)
        def uid(rect, frame, band, serial):
            value = '%09d' % (100000000+serial-1)
            scan.profile_images[value] = b'profile-test'
            return value
        scan.read_uid = MagicMock(side_effect=uid)
        advance_frames = iter(pages[1:])
        def advance(rect, frame):
            next_frame, _ = next(advance_frames)
            pixels = (int(next_frame[0,0,0])-int(frame[0,0,0]))*layout.ROW_PITCH
            scan.offset += pixels
            return RECT, next_frame, {'pixels': pixels, 'correlation': 1.}
        scan.advance = MagicMock(side_effect=advance)
        stack = baseline.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('app.online_scan.layout.read_page', side_effect=lambda f, **kwargs: by_marker[int(f[0,0,0])]))
        stack.enter_context(patch('app.online_scan.layout.read_header', return_value=(True, pages[0][1].expected_total)))
        stack.enter_context(patch('app.online_scan.layout.scrollbar', return_value=None))
        def actions(frame, observations, mode):
            page = by_marker[int(frame[0,0,0])]
            states = [o.entry.status if o.entry else 'online' for o in page.observations]
            return states, [{'status': s, 'left_status': s, 'reliable_online_action': s == 'online'} for s in states]
        stack.enter_context(patch('app.online_scan.read_actions', side_effect=actions))
        return scan

    def test_boundary_five_online_one_offline_keeps_full_roster_and_reuses_only_online(self):
        previous = self.seed()
        scan = self.prepare([make_page(range(6), ['online']*5+['offline'])])
        result = scan.run()
        self.assertTrue(result.complete, result.message)
        self.assertEqual(previous, self.db.get_complete_roster())
        self.assertEqual(5, result.online)
        self.assertEqual(8, result.total)
        self.assertEqual(set(range(5)), set(scan.slots))
        self.assertEqual(set(range(5)), set(scan.uids))
        scan.read_uid.assert_not_called()
        scan.advance.assert_not_called()
        self.assertTrue(all(len(call.args[0].observations) == 6 for call in scan.verify_visible.call_args_list))

    def test_new_renamed_and_cross_mode_items_read_only_their_own_uid(self):
        self.seed()
        first = make_page(range(6), ['online']*5+['offline'])
        first[1].observations[1].entry.nickname = 'changed-name'
        first[1].observations[2].name_image = observation(19).name_image
        scan = self.prepare([first])
        scan.profiles[3]['interface_mode'] = 'normal'
        result = scan.run()
        self.assertTrue(result.complete, result.message)
        self.assertEqual([2,3,4], [call.args[3] for call in scan.read_uid.call_args_list])
        self.assertTrue(all(len(call.args[0].observations) == 6 for call in scan.verify_visible.call_args_list))

    def test_more_than_six_online_uses_measured_overlap_and_reads_no_offline_uid(self):
        previous = self.seed(12)
        scan = self.prepare([make_page(range(6), total=12),
                             make_page(range(5,11), ['online']*3+['offline']*3, total=12)])
        result = scan.run()
        self.assertTrue(result.complete, result.message)
        self.assertEqual(8, result.online)
        self.assertEqual(1, scan.advance.call_count)
        self.assertEqual(set(range(8)), set(scan.slots))
        self.assertEqual(previous, self.db.get_complete_roster())

    def test_zero_online_commits_empty_subset_without_uid_or_full_roster_replacement(self):
        previous = self.seed()
        scan = self.prepare([make_page(range(6), ['offline']*6)])
        result = scan.run()
        self.assertTrue(result.complete, result.message)
        self.assertEqual(0, result.online)
        self.assertEqual(previous, self.db.get_complete_roster())
        scan.read_uid.assert_not_called()
        scan.advance.assert_not_called()

    def test_all_online_small_roster_finishes_at_total_without_offline_boundary(self):
        scan = self.prepare([make_page(range(3), total=3)])
        result = scan.run()
        self.assertTrue(result.complete, result.message)
        self.assertEqual((3,3), (result.total,result.online))
        self.assertEqual(3, scan.read_uid.call_count)
        self.assertIsNone(scan.boundary)
        self.assertIsNone(self.db.get_complete_roster())
        scan.advance.assert_not_called()

    def test_empty_roster_has_no_uid_or_movement(self):
        scan = self.prepare([make_page([], total=0)])
        result = scan.run()
        self.assertTrue(result.complete, result.message)
        self.assertEqual(0, result.total)
        scan.read_uid.assert_not_called()
        scan.advance.assert_not_called()

    def test_unsorted_statuses_fail_before_uid_or_database_write(self):
        previous = self.seed()
        scan = self.prepare([make_page(range(6), ['online', 'offline']+['online']*4)])
        with patch.object(self.db, 'apply_online_scan', wraps=self.db.apply_online_scan) as commit:
            result = scan.run()
        self.assertFalse(result.complete)
        commit.assert_not_called()
        scan.read_uid.assert_not_called()
        self.assertEqual(previous, self.db.get_complete_roster())

    def test_duplicate_uid_failure_is_atomic(self):
        self.seed()
        scan = self.prepare([make_page(range(6), ['online']*5+['offline'])])
        scan.profiles = []
        scan.read_uid.side_effect = None
        scan.read_uid.return_value = '123456789'
        with patch.object(self.db, 'apply_online_scan') as commit:
            result = scan.run()
        self.assertFalse(result.complete)
        self.assertEqual(2, scan.read_uid.call_count)
        commit.assert_not_called()

    def test_final_account_guard_failure_never_commits(self):
        self.seed()
        scan = self.prepare([make_page(range(6), ['online']*5+['offline'])])
        scan.shot.side_effect = ScanIncomplete('account changed')
        with patch.object(self.db, 'apply_online_scan') as commit:
            result = scan.run()
        self.assertFalse(result.complete)
        commit.assert_not_called()

    def test_online_status_change_after_uid_never_commits(self):
        self.seed()
        scan = self.prepare([make_page(range(6), ['online']*5+['offline'])])
        readings = [(states, [{'left_status': state, 'status': state} for state in states])
                    for states in (['online']*5+['offline'], ['online']*4+['offline']*2)]
        with patch('app.online_scan.read_actions', side_effect=readings), \
                patch.object(self.db, 'apply_online_scan') as commit:
            result = scan.run()
        self.assertFalse(result.complete)
        self.assertIn('状态在 UID', result.message)
        commit.assert_not_called()

    def test_count_change_or_skipped_online_rows_never_commits(self):
        for second, expected_error in ((make_page(range(5,11), total=13), '总数'),
                                        (make_page(range(7,13), total=14), '未覆盖')):
            with self.subTest(error=expected_error):
                first = make_page(range(6), total=14 if expected_error == '未覆盖' else 12)
                scan = self.prepare([first, second])
                with patch.object(self.db, 'apply_online_scan') as commit:
                    result = scan.run()
                self.assertFalse(result.complete)
                self.assertIn(expected_error, result.message)
                commit.assert_not_called()

    def test_all_offline_next_page_still_requires_measured_row_grid_alignment(self):
        self.seed(12)
        pages = [make_page(range(6), total=12), make_page(range(6,12), ['offline']*6, total=12)]
        scan = self.prepare(pages)
        def mismeasured(rect, frame):
            scan.offset = 6*layout.ROW_PITCH+20
            return RECT, pages[1][0], {'pixels': scan.offset, 'correlation': .9}
        scan.advance.side_effect = mismeasured
        with patch.object(self.db, 'apply_online_scan') as commit:
            result = scan.run()
        self.assertFalse(result.complete)
        self.assertIn('行距', result.message)
        commit.assert_not_called()

    def test_missing_online_fields_abort_but_missing_offline_fields_need_no_uid(self):
        self.seed()
        frame, page = make_page(range(6), ['online']*5+['offline'])
        page.observations[0].entry = None
        scan = self.prepare([(frame,page)])
        with patch.object(self.db, 'apply_online_scan') as commit:
            result = scan.run()
        self.assertFalse(result.complete)
        scan.prepare_page.assert_called_once()
        scan.read_uid.assert_not_called()
        commit.assert_not_called()

        frame, page = make_page(range(6), ['online']*5+['offline'])
        page.observations[5].entry = None
        scan = self.prepare([(frame,page)])
        states = ['online']*5+['offline']
        with patch('app.online_scan.read_actions', return_value=(states, [{'left_status': s} for s in states])):
            result = scan.run()
        self.assertTrue(result.complete, result.message)
        scan.prepare_page.assert_not_called()
        scan.read_uid.assert_not_called()

    def test_offline_tail_identity_change_blocks_uid_and_commit(self):
        scan = self.prepare([make_page(range(6), ['online']*5+['offline'])])
        scan.verify_visible.side_effect = ScanIncomplete('sixth offline identity changed')
        with patch.object(self.db, 'apply_online_scan') as commit:
            result = scan.run()
        self.assertFalse(result.complete)
        self.assertEqual(6, len(scan.verify_visible.call_args.args[0].observations))
        scan.read_uid.assert_not_called()
        commit.assert_not_called()

    def test_guarded_shot_preserves_account_and_foreground_checks(self):
        scan = OnlineScanWorkflow(self.scanner)
        scan.check = MagicMock()
        self.scanner._get_rect = MagicMock(return_value=RECT)
        self.scanner._verify_account_frame = MagicMock(side_effect=ScanIncomplete('account changed'))
        with patch('app.fast_scan.capture.capture_region', return_value=np.zeros((1080,1920,3), np.uint8)):
            with self.assertRaisesRegex(ScanIncomplete, 'account changed'):
                scan.shot()
        scan.check.assert_called_once()
        self.scanner._verify_account_frame.assert_called_once()


if __name__ == '__main__':
    unittest.main()

"""Fast nickname monitoring preserves local identities without enrolling every UID."""
from tests import test_regressions as baseline

from dataclasses import replace
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from app import capture, fast_layout as layout
from app.fast_scan import ScanIncomplete, _decode, _png
from app.quick_scan import QuickIdentities, QuickScanWorkflow


class QuickScanWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.harness = baseline.FastScanRegressions('runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.stack = self.harness.stack
        self.db, self.scanner = self.harness.db, self.harness.scan
        self.stack.enter_context(patch('app.fast_scan.nickname_ocr.configure_game_font'))
        self.stack.enter_context(patch.object(self.scanner, '_wait', return_value=True))
        self.stack.enter_context(patch.object(baseline.game_input, 'click_client',
                                            side_effect=AssertionError('test must not click a game')))
        self.stack.enter_context(patch.object(baseline.game_input, 'press_esc',
                                            side_effect=AssertionError('test must not send game keys')))
        self.stack.enter_context(patch.object(baseline.game_input, 'move_client',
                                            side_effect=AssertionError('test must not move the real pointer')))

    def seed(self, count, *, signature_index=None, with_baseline=False, status='offline'):
        ids = []
        for index in range(count):
            ids.append(self.db.add_friend('%09d' % (100000000+index), '好友%03d' % index,
                60, '唯一完整签名' if index == signature_index else None, status, baseline.T0))
        if with_baseline:
            pixels = _png(np.full((30,120,3), 205, np.uint8))
            records = [{'id': fid, 'uid': '%09d' % (100000000+index),
                        'entry': baseline.entry('好友%03d' % index, status=status),
                        'name_image': pixels, 'avatar_image': pixels}
                       for index, fid in enumerate(ids)]
            self.db.apply_verified_scan(records, baseline.T0, 0., self.harness.work.name,
                                        identity_complete=True)
        return ids

    def simulate(self, count, mutate=None):
        state = self.harness.simulation(count=count, move_rows=5)
        original_drag = baseline.scroller.drag_client.side_effect
        def drag(rect, start, end, **kwargs):
            if start[0] > 1800 and end[1] == layout.TRACK_TOP:
                state['offset'] = 0.
                return True
            return original_drag(rect, start, end, **kwargs)
        baseline.scroller.drag_client.side_effect = drag
        raw_read = layout.read_page.side_effect
        state['read_offsets'] = []
        state['read_quick_flags'] = []
        def read(frame, **kwargs):
            page = raw_read(frame, **kwargs)
            state['read_offsets'].append(state['offset'])
            state['read_quick_flags'].append(kwargs.get('quick'))
            for obs in page.observations:
                index = int(obs.entry.nickname[-3:])
                if mutate is not None:
                    mutate(index, obs)
            page.entries = [obs.entry for obs in page.observations]
            return page
        layout.read_page.side_effect = read
        self.stack.enter_context(patch.object(layout, 'read_header', return_value=(True, count)))
        return state

    def run_workflow(self, state, *, uid=None, online_only=False, stamp=baseline.T1):
        state['offset'] = 0.
        workflow = QuickScanWorkflow(self.scanner, online_only=online_only)
        def visible(page):
            frame = capture.capture_region(0,0,1920,1080)
            bands = layout.bands(frame)
            self.assertEqual(len(page.observations), len(bands))
            return (0,0,1920,1080), frame, bands
        workflow.verify_visible = MagicMock(side_effect=visible)
        workflow.read_uid = MagicMock(side_effect=uid if callable(uid) else None,
                                     return_value=uid)
        with patch('app.quick_scan.now_str', return_value=stamp):
            result = workflow.run()
        self.assertTrue(result.complete, result.message)
        if not online_only:
            self.assertEqual(set(range(workflow.expected)), set(workflow.slots))
        self.assertTrue(all(state['read_quick_flags']))
        return workflow, result

    def test_unique_nickname_keeps_uid_when_level_avatar_and_roster_size_change(self):
        ids = self.seed(12)
        def changed(index, obs):
            if index == 3:
                obs.entry = replace(obs.entry, level=59)
                obs.avatar_image = np.zeros_like(obs.avatar_image)
        state = self.simulate(13, changed)
        workflow, result = self.run_workflow(state, uid='100000012')
        workflow.read_uid.assert_called_once()
        self.assertEqual(13, workflow.read_uid.call_args.args[3])
        self.assertEqual(13, result.total)
        self.assertEqual(ids[3], self.db.find_by_uid('100000003')['id'])
        self.assertEqual(59, self.db.find_by_uid('100000003')['level'])
        self.assertEqual('100000012', self.db.find_by_nickname('好友012')['uid'])
        self.assertGreater(len(state['commands']), 0)
        self.assertTrue(all(event['movement']['correlation'] >= .67
                            for event in workflow.frames if event['movement']))

    def test_scanner_entry_routes_both_quick_modes_without_full_uid_workflow(self):
        self.seed(7)
        state = self.simulate(7)
        def actions(frame, observations, mode):
            return ['online']*len(observations), [('申请加入', .99)]*len(observations)
        with patch.object(self.scanner, '_ensure_friend_list', return_value=True), \
                patch.object(self.scanner, '_confirm_account'), \
                patch('app.scan_workflow.ScanWorkflow', side_effect=AssertionError('full UID is manual only')), \
                patch.object(QuickScanWorkflow, 'read_uid', side_effect=AssertionError('no rename, no UID')) as uid, \
                patch('app.quick_scan.read_quick_actions', side_effect=actions):
            for online_only in (False, True):
                with self.subTest(online_only=online_only), \
                        patch.object(baseline.CONFIG, 'online_only', online_only):
                    state['offset'] = 0.
                    result = self.scanner.scan_once()
                    self.assertTrue(result.complete, result.message)
                    self.assertEqual(7, result.total)
                    self.assertEqual('online' if online_only else 'scan', result.mode)
                    self.assertEqual('100000006', self.db.find_by_nickname('好友006')['uid'])
        uid.assert_not_called()

    def test_small_list_without_scrollbar_reuses_single_home_page_ocr(self):
        state = self.simulate(2)
        with patch.object(layout, 'scrollbar', return_value=None):
            workflow, result = self.run_workflow(state)
        self.assertEqual(2, result.total)
        self.assertEqual([True], state['read_quick_flags'])
        self.assertEqual(1, workflow.frame_count)
        workflow.read_uid.assert_not_called()

    def test_first_scan_uidless_friends_accrue_time_and_reuse_local_ids_next_round(self):
        self.db.configure_monitoring(0, at=baseline.T0)
        state = self.simulate(7)
        first, _ = self.run_workflow(state, stamp=baseline.T1)
        created = self.db.find_by_nickname('好友006')
        self.assertIsNone(created['uid'])
        self.assertEqual('online', created['current_status'])
        self.assertEqual(baseline.T1, created['first_online_at'])
        second, _ = self.run_workflow(state, stamp=baseline.T2)
        same = self.db.find_by_nickname('好友006')
        self.assertEqual(created['id'], same['id'])
        self.assertEqual(7, len(self.db.get_all_friends()))
        projected = next(r for r in self.db.get_all_friends(now=baseline.T3) if r['id'] == same['id'])
        self.assertEqual(120, projected['cumulative_online_seconds'])
        first.read_uid.assert_not_called()
        second.read_uid.assert_not_called()
        self.assertEqual('nickname', next(r['match_basis'] for r in second.records if r['id'] == same['id']))

    def test_identical_names_and_signatures_use_stable_unknown_placeholders(self):
        old = self.db.add_friend('123456789', '重名', 60, '同一句完整签名', 'online', baseline.T0)
        self.db.configure_monitoring(0, at=baseline.T0)
        def twins(index, obs):
            obs.entry = replace(obs.entry, nickname='重名', signature='同一句完整签名',
                                signature_confidence=.99)
        state = self.simulate(2, twins)
        first, result = self.run_workflow(state)
        placeholders = self.db.get_display_friends(now=baseline.T2)
        self.assertEqual(2, len(placeholders))
        self.assertEqual({'unknown'}, {r['current_status'] for r in placeholders})
        self.assertEqual({None}, {r['uid'] for r in placeholders})
        self.assertTrue(all(r['identity_review_key'] and r['identity_review_reason'] for r in placeholders))
        self.assertTrue(all(r['cumulative_online_seconds'] == 0 for r in placeholders))
        self.assertTrue(all(r['id'] != old for r in placeholders))
        ids = {r['id'] for r in placeholders}
        second, _ = self.run_workflow(state, stamp=baseline.T2)
        self.assertEqual(ids, {r['id'] for r in self.db.get_display_friends()})
        self.assertEqual(3, len(self.db.get_all_friends()))
        self.assertEqual('123456789', self.db.get_friend(old)['uid'])
        self.assertIn('UID未能确认', result.message)
        self.assertEqual(2, first.read_uid.call_count)
        self.assertEqual(2, second.read_uid.call_count)

    def test_unreadable_nickname_keeps_original_image_and_local_id(self):
        originals = []
        def unreadable(index, obs):
            obs.entry = replace(obs.entry, nickname='', nickname_pending=True)
            obs.name_audit = {'method': 'rejected', 'image_only': True, 'reason': 'unsupported glyph'}
            obs.fields = [('', 0.), ('Lv.60', .99), ('在线', .99)]
            originals.append(obs.name_image.copy())
        state = self.simulate(1, unreadable)
        first, _ = self.run_workflow(state)
        row = self.db.get_display_friends()[0]
        self.assertTrue(row['nickname_pending'])
        self.assertEqual('', row['nickname'])
        self.assertIsNone(row['uid'])
        np.testing.assert_array_equal(originals[0], _decode(self.db.get_nickname_image(row['id'])))
        second, _ = self.run_workflow(state, stamp=baseline.T2)
        self.assertEqual(row['id'], self.db.get_display_friends()[0]['id'])
        self.assertEqual(1, len(self.db.get_all_friends()))
        self.assertEqual('needs_remark', second.records[0]['match_basis'])
        first.read_uid.assert_called_once()
        second.read_uid.assert_called_once()

    def test_blank_nickname_is_unknown_with_screenshot_and_no_uid_guess(self):
        def blank(index, obs):
            obs.entry = replace(obs.entry, nickname='', nickname_pending=True)
            obs.name_image = np.full_like(obs.name_image, 205)
        state = self.simulate(1, blank)
        first, _ = self.run_workflow(state)
        row = self.db.get_display_friends()[0]
        self.assertEqual('unknown', row['current_status'])
        self.assertIsNone(row['uid'])
        self.assertIsNotNone(self.db.get_nickname_image(row['id']))
        second, _ = self.run_workflow(state, stamp=baseline.T2)
        self.assertEqual(row['id'], self.db.get_display_friends()[0]['id'])
        first.read_uid.assert_called_once()
        second.read_uid.assert_called_once()

    def test_unique_signature_rename_reads_one_uid_then_continues_following_pages(self):
        ids = self.seed(13, signature_index=2)
        def renamed(index, obs):
            if index == 2:
                obs.entry = replace(obs.entry, nickname='新昵称', signature='唯一完整签名', signature_confidence=.99)
        state = self.simulate(13, renamed)
        uid_offsets = []
        def uid(*args):
            uid_offsets.append(state['offset'])
            return '100000002'
        workflow, result = self.run_workflow(state, uid=uid)
        workflow.read_uid.assert_called_once()
        self.assertEqual(3, workflow.read_uid.call_args.args[3])
        self.assertEqual([0.], uid_offsets)
        self.assertGreater(max(state['read_offsets']), uid_offsets[0])
        self.assertEqual(13, result.total)
        self.assertEqual(ids[2], self.db.find_by_uid('100000002')['id'])
        self.assertEqual('新昵称', self.db.find_by_uid('100000002')['nickname'])
        self.assertTrue(all(len(call.args[0].observations) == 6
                            for call in workflow.verify_visible.call_args_list))

    def test_blank_nickname_at_overlapping_slot_does_not_block_next_page(self):
        def blank(index, obs):
            if index == 5:
                obs.entry = replace(obs.entry, nickname='', nickname_pending=True)
                obs.name_image = np.full_like(obs.name_image, 205)
        state = self.simulate(8, blank)
        workflow, result = self.run_workflow(state)
        self.assertEqual(8, result.total)
        self.assertGreater(workflow.frame_count, 1)
        self.assertEqual('unknown', workflow.records[5]['entry'].status)
        self.assertIsNotNone(workflow.records[5]['identity_review_key'])
        workflow.read_uid.assert_called_once()
        self.assertEqual(6, workflow.read_uid.call_args.args[3])

    def test_overlapping_friend_seen_offline_is_removed_from_online_selection(self):
        self.seed(8, with_baseline=True)
        state = self.simulate(8)
        calls = []
        def actions(frame, observations, mode):
            calls.append(state['offset'])
            states = ['online']*len(observations)
            if len(calls) == 2:
                states[0] = 'offline'
            return states, [('label', .99)]*len(states)
        with patch('app.quick_scan.read_quick_actions', side_effect=actions):
            workflow, _ = self.run_workflow(state, online_only=True)
        offline_index = workflow.frames[1]['indices'][0]
        self.assertNotIn(offline_index, workflow.active_indices)
        self.assertNotIn(offline_index, {r['index'] for r in workflow.records})
        self.assertEqual('offline', self.db.find_by_uid('%09d' % (100000000+offline_index))['current_status'])
        workflow.read_uid.assert_not_called()

    def test_different_verified_uid_cannot_inherit_signature_candidates_history(self):
        self.db.configure_monitoring(0, at=baseline.T0)
        old = self.db.add_friend('100000002', '旧昵称', 60, '唯一完整签名', 'online', baseline.T0)
        def possible(index, obs):
            obs.entry = replace(obs.entry, nickname='新昵称', signature='唯一完整签名', signature_confidence=.99)
        state = self.simulate(1, possible)
        workflow, _ = self.run_workflow(state, uid='999999999', stamp=baseline.T1)
        previous, new = self.db.get_friend(old), self.db.find_by_uid('999999999')
        self.assertNotEqual(old, new['id'])
        self.assertEqual('旧昵称', previous['nickname'])
        self.assertEqual('100000002', previous['uid'])
        self.assertEqual(0, new['cumulative_online_seconds'])
        self.assertEqual(baseline.T1, new['first_online_at'])
        self.assertIsNone(workflow.records[0]['id'])
        self.assertEqual(60, previous['cumulative_online_seconds'])

    def test_failed_rename_uid_keeps_unknown_placeholder_and_continues(self):
        self.seed(13, signature_index=2)
        def renamed(index, obs):
            if index == 2:
                obs.entry = replace(obs.entry, nickname='新昵称', signature='唯一完整签名', signature_confidence=.99)
        state = self.simulate(13, renamed)
        def failure(*args):
            raise ScanIncomplete('unreadable UID')
        workflow, result = self.run_workflow(state, uid=failure)
        self.assertEqual(13, result.total)
        self.assertEqual({2}, workflow.failed_uids)
        pending = next(r for r in self.db.get_display_friends() if r['identity_review_key'])
        self.assertEqual('unknown', pending['current_status'])
        self.assertIsNone(pending['uid'])
        self.assertGreater(max(state['read_offsets']), 0)
        workflow.read_uid.assert_called_once()

    def test_online_mode_does_not_enroll_all_uids_or_replace_complete_baseline(self):
        self.seed(13, with_baseline=True)
        previous = self.db.get_complete_roster()
        def states(index, obs):
            obs.entry = replace(obs.entry, status='online' if index < 5 else 'offline')
        state = self.simulate(13, states)
        def actions(frame, observations, mode):
            return ([o.entry.status for o in observations],
                    [('申请加入' if o.entry.status == 'online' else '离线', .99) for o in observations])
        with patch('app.quick_scan.read_quick_actions', side_effect=actions):
            workflow, result = self.run_workflow(state, online_only=True)
        self.assertEqual(5, result.online)
        self.assertEqual(5, len(workflow.records))
        self.assertEqual(1, workflow.frame_count)
        self.assertEqual([], state['commands'])
        workflow.read_uid.assert_not_called()
        self.assertEqual(previous, self.db.get_complete_roster())

    def test_confirmed_uid_takes_precedence_over_conflicting_inferred_match(self):
        row = {'id': 1, 'uid': '111111111', 'nickname': '既有昵称', 'signature': '签名一'}
        state = self.simulate(2)
        frame = capture.capture_region(0,0,1920,1080)
        observations = layout.read_page(frame, quick=True).observations
        for obs, name, signature in zip(observations, ('既有昵称', '疑似改名'), ('签名二', '签名一')):
            obs.entry = replace(obs.entry, nickname=name, signature=signature, signature_confidence=.99)
        # The actual UID read owns the row; the inferred match must be checked.
        result = QuickIdentities([row], []).resolve(enumerate(observations), verified={1: '111111111'})
        self.assertEqual(['unknown', 'online'], [r['entry'].status for r in result])
        self.assertEqual([None, '111111111'], [r['uid'] for r in result])
        self.assertTrue(result[0]['uid_check_reason'])
        self.assertIsNone(result[1]['identity_review_key'])

    def test_cross_page_duplicate_pair_is_reconsidered_before_atomic_commit(self):
        old = self.seed(8, with_baseline=True)[0]
        self.db.update_friend_manual(old, '100000000', '重名', 60, '同签名')
        def twins(index, obs):
            if index in (0, 7):
                obs.entry = replace(obs.entry, nickname='重名', signature='同签名', signature_confidence=.99)
        state = self.simulate(8, twins)
        first, _ = self.run_workflow(state)
        reviews = [r for r in self.db.get_display_friends() if r['identity_review_key']]
        self.assertEqual(2, len(reviews))
        self.assertEqual({'unknown'}, {r['current_status'] for r in reviews})
        self.assertTrue(all(r['id'] != old and r['uid'] is None for r in reviews))
        ids = {r['id'] for r in reviews}
        second, _ = self.run_workflow(state, stamp=baseline.T2)
        self.assertEqual(ids, {r['id'] for r in self.db.get_display_friends() if r['identity_review_key']})
        self.assertEqual(10, len(self.db.get_all_friends()))
        self.assertGreater(first.frame_count, 1)
        self.assertEqual(2, first.read_uid.call_count)
        self.assertEqual(2, second.read_uid.call_count)

    def test_online_new_friends_remain_uidless_and_reuse_local_rows(self):
        def statuses(index, obs):
            obs.entry = replace(obs.entry, status='online' if index < 2 else 'offline')
        state = self.simulate(6, statuses)
        def actions(frame, observations, mode):
            return [o.entry.status for o in observations], [('label', .99)]*len(observations)
        with patch('app.quick_scan.read_quick_actions', side_effect=actions):
            first, result = self.run_workflow(state, online_only=True)
            first_ids = {r['id'] for r in self.db.get_display_friends()}
            second, _ = self.run_workflow(state, online_only=True, stamp=baseline.T2)
        self.assertEqual(2, result.online)
        self.assertEqual(first_ids, {r['id'] for r in self.db.get_display_friends()})
        self.assertEqual(2, len(self.db.get_all_friends()))
        self.assertTrue(all(r['uid'] is None for r in self.db.get_all_friends()))
        self.assertIsNone(self.db.get_complete_roster())
        first.read_uid.assert_not_called()
        second.read_uid.assert_not_called()

    def test_failed_rename_retries_next_round_and_keeps_review_local_id(self):
        self.seed(13, signature_index=2)
        def renamed(index, obs):
            if index == 2:
                obs.entry = replace(obs.entry, nickname='待核验新名', signature='唯一完整签名',
                                    signature_confidence=.99)
        state = self.simulate(13, renamed)
        def failure(*args):
            raise ScanIncomplete('profile UID could not be confirmed')
        first, _ = self.run_workflow(state, uid=failure)
        first.read_uid.assert_called_once()
        pending = next(r for r in self.db.get_display_friends() if r['identity_review_key'])
        second, _ = self.run_workflow(state, uid=failure, stamp=baseline.T2)
        second.read_uid.assert_called_once()
        same = next(r for r in self.db.get_display_friends() if r['identity_review_key'])
        self.assertEqual(pending['id'], same['id'])
        self.assertEqual(pending['identity_review_key'], same['identity_review_key'])
        self.assertEqual('unknown', same['current_status'])
        self.assertIsNone(same['uid'])
        self.assertEqual(14, len(self.db.get_all_friends()))

    def test_online_six_unknown_rows_continue_until_a_real_offline_boundary(self):
        self.seed(13, with_baseline=True)
        previous = self.db.get_complete_roster()
        def states(index, obs):
            status = 'unknown' if index < 6 else 'online' if index < 8 else 'offline'
            obs.entry = replace(obs.entry, status=status)
        state = self.simulate(13, states)
        def actions(frame, observations, mode):
            statuses = [o.entry.status for o in observations]
            return statuses, [('uncertain' if s == 'unknown' else s, .5 if s == 'unknown' else .99)
                              for s in statuses]
        with patch('app.quick_scan.read_quick_actions', side_effect=actions):
            workflow, result = self.run_workflow(state, online_only=True)
        self.assertGreaterEqual(workflow.frame_count, 2)
        self.assertGreater(max(state['read_offsets']), 0.)
        self.assertEqual(8, workflow.boundary)
        self.assertEqual(set(range(8)), workflow.active_indices)
        self.assertEqual(8, len(workflow.records))
        self.assertEqual(6, sum(r['entry'].status == 'unknown' for r in workflow.records))
        self.assertEqual(2, result.online)
        self.assertEqual(previous, self.db.get_complete_roster())
        workflow.read_uid.assert_not_called()

    def test_uidless_signature_candidate_is_checked_without_inheriting_unproven_history(self):
        self.db.configure_monitoring(0, at=baseline.T0)
        old = self.db.add_friend(None, '未核验旧名', 60, '唯一完整签名', 'online', baseline.T0)
        def new_name(index, obs):
            obs.entry = replace(obs.entry, nickname='另一个新名', signature='唯一完整签名',
                                signature_confidence=.99)
        state = self.simulate(1, new_name)
        workflow, _ = self.run_workflow(state, uid='999999999', stamp=baseline.T1)
        workflow.read_uid.assert_called_once()
        new = self.db.find_by_uid('999999999')
        previous = self.db.get_friend(old)
        self.assertIsNotNone(new)
        self.assertNotEqual(old, new['id'])
        self.assertIsNone(previous['uid'])
        self.assertEqual('999999999', new['uid'])
        self.assertEqual('online', new['current_status'])
        self.assertIsNone(new['identity_review_key'])
        self.assertIsNotNone(self.db.get_nickname_image(new['id']))
        self.assertEqual('未核验旧名', previous['nickname'])
        self.assertEqual(60, previous['cumulative_online_seconds'])
        self.assertEqual(0, new['cumulative_online_seconds'])
        self.assertEqual(baseline.T1, new['first_online_at'])
        self.assertEqual(2, len(self.db.get_all_friends()))
        second, _ = self.run_workflow(state, uid='999999999', stamp=baseline.T2)
        second.read_uid.assert_not_called()
        self.assertEqual(new['id'], self.db.get_display_friends()[0]['id'])
        self.assertEqual(2, len(self.db.get_all_friends()))

    def test_online_mixed_page_keeps_online_rows_after_offline_and_stops_after_page(self):
        self.seed(13, with_baseline=True, status='online')
        previous = self.db.get_complete_roster()
        mixed = ['online', 'offline', 'online', 'offline', 'online', 'online']
        def statuses(index, obs):
            obs.entry = replace(obs.entry, status=mixed[index] if index < len(mixed) else 'offline')
        state = self.simulate(13, statuses)
        def actions(frame, observations, mode):
            return [o.entry.status for o in observations], [('label', .99)]*len(observations)
        with patch('app.quick_scan.read_quick_actions', side_effect=actions):
            workflow, result = self.run_workflow(state, online_only=True)
        self.assertEqual(4, result.online)
        self.assertEqual({0,2,4,5}, workflow.active_indices)
        self.assertEqual([0,2,4,5], [record['index'] for record in workflow.records])
        self.assertEqual(1, workflow.frame_count)
        self.assertEqual([], state['commands'])
        self.assertEqual(previous, self.db.get_complete_roster())
        offline_events = [e for e in self.db.get_recent_events(changes_only=True) if e['event_type'] == 'offline']
        self.assertEqual({'100000001', '100000003'}, {e['uid'] for e in offline_events})
        workflow.read_uid.assert_not_called()

    def test_online_and_offline_identical_pair_never_assigns_online_time_to_old_uid(self):
        old = self.db.add_friend('123456789', '重名', 60, '同签名', 'offline', baseline.T0)
        self.db.configure_monitoring(0, at=baseline.T0)
        online_index = [0]
        def twins(index, obs):
            obs.entry = replace(obs.entry, nickname='重名', signature='同签名',
                                signature_confidence=.99,
                                status='online' if index == online_index[0] else 'offline')
        state = self.simulate(2, twins)
        def actions(frame, observations, mode):
            return [o.entry.status for o in observations], [('label', .99)]*len(observations)
        with patch('app.quick_scan.read_quick_actions', side_effect=actions):
            first, _ = self.run_workflow(state, online_only=True)
            pending = next(row for row in self.db.get_all_friends() if row['identity_review_key'])
            self.assertEqual((None, 'unknown', 0),
                             (pending['uid'], pending['current_status'], pending['cumulative_online_seconds']))
            self.assertNotEqual(old, pending['id'])
            # Offline cards still constrain identity when they precede the
            # online card, but cannot consume its reusable review placeholder.
            online_index[0] = 1
            second, _ = self.run_workflow(state, online_only=True, stamp=baseline.T2)
        self.assertEqual(2, len(self.db.get_all_friends()))
        self.assertEqual(pending['id'], second.records[0]['id'])
        self.assertEqual('needs_remark', second.records[0]['match_basis'])
        self.assertEqual('unknown', self.db.get_friend(pending['id'])['current_status'])
        original = self.db.get_friend(old)
        self.assertEqual(('123456789', 'offline', None, 0),
                         (original['uid'], original['current_status'], original['first_online_at'],
                          original['cumulative_online_seconds']))
        first.read_uid.assert_called_once()
        second.read_uid.assert_called_once()


if __name__ == '__main__':
    unittest.main()

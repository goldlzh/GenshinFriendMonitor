"""Slow OCR must not turn stale menu screenshots into game input."""
from tests import test_regressions as baseline

import json
import unittest
from unittest.mock import patch

import cv2

from app import capture, friend_screen, input as game_input
from app import profile_layout as profile, scan_workflow as flow
from app.config import CONFIG
from app.fast_scan import ScanIncomplete


class UidMenuFreshnessTests(unittest.TestCase):
    setUp = baseline.UidTraversalRegressions.setUp
    simulation = baseline.FastScanRegressions.simulation

    def moving_menu(self, *, miss_first=False, disappear_on_retry=False,
                    profile_opens=True, profile_uids=('123456789', '123456789')):
        self.simulation(count=2)
        rect = (0, 0, 1920, 1080)
        bare = capture.capture_region(*rect)
        bare[0, 0] = 1
        menu = bare.copy()
        menu[135:430, 330:620] = (220, 225, 230)
        cv2.putText(menu, 'VIEW PROFILE', (390, 261), cv2.FONT_HERSHEY_SIMPLEX,
                    .5, (30, 30, 30), 2)
        menu[0, 0] = 2
        book = bare.copy()
        book[170:891, 363:1559] = (190, 190, 190)
        book[0, 0] = 3
        frames = {'bare': bare, 'menu': menu, 'profile': book}
        target = baseline.text_box('查看资料', x=390, y=238, w=135, h=30)
        state = {'time': 0., 'role': 'bare', 'pending': None,
                 'avatars': 0, 'profiles': 0, 'escapes': 0,
                 'invalid_profiles': 0, 'menu_reads': [], 'shots': [], 'uid_reads': 0}

        def current():
            pending = state['pending']
            if pending is not None and state['time'] >= pending[0]:
                state['role'], state['pending'] = pending[1], None
            return state['role']

        def advance(seconds):
            state['time'] += max(0., seconds)
            current()

        def shot():
            role = current()
            state['shots'].append((state['time'], role))
            return rect, frames[role].copy()

        def click(current_rect, point):
            role = current()
            if point[0] < 250:
                state['avatars'] += 1
                if role == 'menu':
                    # A second avatar click toggles an already-open menu shut.
                    state['pending'] = (state['time'] + .1, 'bare')
                elif not (miss_first and state['avatars'] == 1):
                    state['pending'] = (state['time'] + .1, 'menu')
            else:
                state['profiles'] += 1
                if role != 'menu':
                    state['invalid_profiles'] += 1
                state['pending'] = None
                state['role'] = 'profile' if role == 'menu' and profile_opens else 'bare'

        def menu_ocr(image):
            role = 'menu' if int(image[0, 0, 0]) == 2 else 'bare'
            state['menu_reads'].append((state['time'], role))
            if disappear_on_retry and role == 'menu' and state['avatars'] >= 2:
                state['pending'] = (state['time'] + .2, 'bare')
            # The first inference exceeds the entire old menu readiness budget.
            advance(1.6 if len(state['menu_reads']) == 1 else .3)
            return [target] if role == 'menu' else []

        def read_profile(image):
            if int(image[0, 0, 0]) != 3:
                return profile.ProfileRead(False, None, [('', 0.)] * 3)
            index = min(state['uid_reads'], len(profile_uids) - 1)
            uid = profile_uids[index]
            state['uid_reads'] += 1
            return profile.ProfileRead(True, uid,
                [('UID ' + (uid or '?'), .99), ('角色展柜', .99), ('名片', .99)])

        def escape(**kwargs):
            state['escapes'] += 1
            # An accidental ESC on the bare list would close it in the game.
            self.assertNotEqual('bare', current(), 'must not press ESC on the friend list')
            state.update(role='bare', pending=None)
            return True

        scan = flow.UidPass(self.scan, 2)
        patches = (
            patch.object(flow.time, 'perf_counter', side_effect=lambda: state['time']),
            patch.object(scan, 'wait', side_effect=advance),
            patch.object(scan, 'shot', side_effect=shot),
            patch.object(scan, 'click', side_effect=click),
            patch.object(profile, 'menu_changed', return_value=True),
            patch.object(profile, 'read_menu', side_effect=menu_ocr),
            patch.object(profile, 'read', side_effect=read_profile),
            patch.object(flow.layout, 'read_header',
                side_effect=lambda image: (int(image[0, 0, 0]) != 3, 2)),
            patch.object(game_input, 'press_esc', side_effect=escape),
            patch.object(CONFIG, 'menu_wait_seconds', .8),
            patch.object(CONFIG, 'profile_wait_seconds', .4),
        )
        for mocked in patches:
            self.stack.enter_context(mocked)
        return scan, rect, bare, state

    def test_slow_initial_bare_ocr_refreshes_menu_without_toggling_avatar_again(self):
        scan, rect, before, state = self.moving_menu()
        uid = scan.read_uid(rect, before, friend_screen.CardBand(116, 231), 1)
        self.assertEqual('123456789', uid)
        self.assertEqual('bare', state['menu_reads'][0][1])
        self.assertEqual(1, state['avatars'])
        self.assertEqual(1, state['profiles'])
        self.assertEqual(0, state['invalid_profiles'])
        self.assertEqual(1, state['escapes'])
        self.assertTrue(any(role == 'menu' and stamp >= 1.6 for stamp, role in state['shots']))
        self.assertEqual([], self.db.get_all_friends())

    def test_menu_disappearing_during_retry_ocr_never_clicks_old_target_or_escapes_list(self):
        scan, rect, before, state = self.moving_menu(miss_first=True, disappear_on_retry=True)
        with self.assertRaises(ScanIncomplete):
            scan.read_uid(rect, before, friend_screen.CardBand(116, 231), 1)
        self.assertEqual(2, state['avatars'])
        self.assertEqual(0, state['profiles'])
        self.assertEqual(0, state['invalid_profiles'])
        self.assertEqual(0, state['escapes'])
        self.assertEqual({}, scan.profile_images)

    def test_profile_that_never_opens_reports_visibility_failure_and_keeps_list_open(self):
        scan, rect, before, state = self.moving_menu(profile_opens=False)
        with self.assertRaises(ScanIncomplete):
            scan.read_uid(rect, before, friend_screen.CardBand(116, 231), 1)
        audit = json.loads((scan.evidence / 'uid-001-read.json').read_text(encoding='utf-8'))
        self.assertEqual('profile_not_visible', audit['failure_reason'])
        self.assertTrue(all(not sample['visible'] and sample['uid'] is None for sample in audit['samples']))
        self.assertEqual(0, state['escapes'])
        self.assertEqual({}, scan.profile_images)

    def test_conflicting_readable_uids_report_conflict_and_do_not_archive(self):
        scan, rect, before, state = self.moving_menu(profile_uids=('123456789', '223456789'))
        with self.assertRaises(ScanIncomplete):
            scan.read_uid(rect, before, friend_screen.CardBand(116, 231), 1)
        audit = json.loads((scan.evidence / 'uid-001-read.json').read_text(encoding='utf-8'))
        self.assertEqual('uid_conflict', audit['failure_reason'])
        self.assertEqual(['123456789', '223456789'], [sample['uid'] for sample in audit['samples']])
        self.assertEqual(1, state['escapes'])
        self.assertEqual({}, scan.profile_images)

    def test_count_change_on_reordered_bare_list_restarts_without_escape(self):
        scan, rect, before, state = self.moving_menu(miss_first=True)
        with patch.object(flow.layout, 'read_header', return_value=(True, 3)), \
             patch.object(scan, 'visible_scores', return_value=(False, [])):
            with self.assertRaises(flow.RosterChanged) as raised:
                scan.read_uid(rect, before, friend_screen.CardBand(116, 231), 1)
        self.assertEqual('count_changed', raised.exception.reason)
        self.assertEqual(1, state['avatars'])
        self.assertEqual(0, state['profiles'])
        self.assertEqual(0, state['escapes'])
        self.assertEqual({}, scan.profile_images)

    def test_missing_profile_on_reordered_bare_list_does_not_escape(self):
        scan, rect, before, state = self.moving_menu(profile_opens=False)
        with patch.object(scan, 'visible_scores', return_value=(False, [])):
            with self.assertRaises(ScanIncomplete):
                scan.read_uid(rect, before, friend_screen.CardBand(116, 231), 1)
        audit = json.loads((scan.evidence / 'uid-001-read.json').read_text(encoding='utf-8'))
        self.assertEqual('profile_not_visible', audit['failure_reason'])
        self.assertEqual(1, state['avatars'])
        self.assertEqual(1, state['profiles'])
        self.assertEqual(0, state['escapes'])
        self.assertEqual({}, scan.profile_images)

    def test_count_change_after_missing_profile_restarts_even_when_rows_changed(self):
        scan, rect, before, state = self.moving_menu(profile_opens=False)
        with patch.object(flow.layout, 'read_header',
                side_effect=lambda image: (True, 3 if state['profiles'] else 2)), \
             patch.object(scan, 'visible_scores', return_value=(False, [])):
            with self.assertRaises(flow.RosterChanged) as raised:
                scan.read_uid(rect, before, friend_screen.CardBand(116, 231), 1)
        self.assertEqual('count_changed', raised.exception.reason)
        self.assertEqual(1, state['avatars'])
        self.assertEqual(1, state['profiles'])
        self.assertEqual(0, state['escapes'])
        self.assertEqual({}, scan.profile_images)


if __name__ == '__main__':
    unittest.main()

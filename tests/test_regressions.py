"""Behavioral regressions. All databases and game input are isolated."""
import atexit
from tests.private_fixtures import private_fixture
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import sys
from contextlib import ExitStack, contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Discovery imports this file as test_regressions; individual test modules use
# tests.test_regressions. Both names must share the one isolated CONFIG object.
if __name__ in ('test_regressions', 'tests.test_regressions'):
    sys.modules.setdefault('test_regressions', sys.modules[__name__])
    sys.modules.setdefault('tests.test_regressions', sys.modules[__name__])

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
(ROOT / 'logs').mkdir(exist_ok=True)
SCRATCH = tempfile.TemporaryDirectory(prefix='regression-', dir=ROOT / 'logs')
WORK = Path(SCRATCH.name).resolve()
assert WORK.is_relative_to(ROOT / 'logs')

from app import config
config.CONFIG = config.Config(db_path=str(WORK / 'unused.db'), log_dir=str(WORK))
config.CONFIG.ensure_dirs()
from app import capture, friend_screen, input as game_input, ocr_engine, parser, scroller, window
from app.database import Database
from app.scanner import Scanner
from app import excel_export, mysql_export
from app.ui import main_window
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QCloseEvent
from openpyxl import load_workbook
import numpy as np

QT = QApplication.instance() or QApplication([])
CONFIG = config.CONFIG
# Legacy-path tests remain useful; the fast path has its own image/coverage tests.
CONFIG.fast_scan = False
CONFIG.fast_ocr = False
REAL_DRAG = scroller.drag
REAL_SCROLL_DOWN = scroller.scroll_down
T0, T1, T2 = ('2026-09-20T10:00:00', '2026-09-20T10:01:00', '2026-09-20T10:02:00')
T3, T4 = '2026-09-20T10:03:00', '2026-09-20T10:04:00'


def _cleanup_shared_fixture():
    QT.processEvents()
    logging.shutdown()
    SCRATCH.cleanup()


# Other modules reuse CONFIG and FastScan's isolated image simulator. Their
# tests can run after this module, so keep its scratch root until process exit.
atexit.register(_cleanup_shared_fixture)


def entry(name='晨风', status='online', last_seen=None, confidence=.99):
    return parser.FriendEntry(name, status, last_seen, 60, 120, 80, confidence)


def text_box(text, x=100, y=20, w=100, h=30, score=.99):
    return ocr_engine.OcrResult(text, score, [[x,y],[x+w,y],[x+w,y+h],[x,y+h]])


class Regressions(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in dict(bring_to_front=False, ensure_friend_list=False,
                               startup_delay_seconds=0, focus_wait_seconds=0,
                               max_scrolls=8, bottom_confirmations=2,
                               removal_confirmations=2, uid_scan_interval_seconds=0,
                               menu_wait_seconds=0, profile_wait_seconds=0,
                               profile_close_seconds=0, scroll_settle_seconds=0,
                               ocr_confidence=.6, ocr_upscale_factor=1.5,
                               scroll_mode='wheel').items():
            self.stack.enter_context(patch.object(CONFIG, key, value))
        self.stack.enter_context(patch.object(Scanner, '_ensure_friend_list', return_value=True))
        self.top = self.stack.enter_context(patch.object(scroller, 'scroll_to_top', return_value=True))
        self.down = self.stack.enter_context(patch.object(scroller, 'scroll_down', return_value=True))
        self.shot = self.stack.enter_context(patch.object(capture, 'capture_region', return_value=np.zeros((40,40,3), dtype=np.uint8)))
        self.ocr = self.stack.enter_context(patch.object(ocr_engine, 'recognize', return_value=[]))
        self.stack.enter_context(patch('win32api.GetAsyncKeyState', return_value=0))
        for name in ('click', 'press_key', 'press_esc'):
            self.stack.enter_context(patch.object(game_input, name))
        self.stack.enter_context(patch.object(window, 'bring_to_front'))
        self.stack.enter_context(patch.object(window, 'get_window_rect', return_value=(0,0,1280,720)))
        self.db = Database(':memory:')
        self.db.configure_monitoring(0)
        self.scanners = []
        self.addCleanup(self.db.close)
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for scan in self.scanners:
            scan.stop()
            for worker in (scan._thread, scan._uid_thread):
                if worker is not None:
                    worker.join(3)
                    self.assertFalse(worker.is_alive(), 'Worker failed to stop')

    def scanner(self):
        scan = Scanner(self.db)
        scan.set_manual_rect((0,0,1280,720))
        self.scanners.append(scan)
        return scan

    def friend(self, name='晨风', uid=None, status='online', now=T0):
        return self.db.add_friend(uid, name, 60, None, status, now)

    @contextmanager
    def pages(self, cards, counts=None):
        counts = counts if counts is not None else [None] * len(cards)
        from itertools import chain, repeat
        results = [friend_screen.FriendPage(confirmed=True, expected_total=count, entries=entries)
                   for entries, count in zip(cards, counts)]
        with patch.object(friend_screen, 'read_page', side_effect=chain(results, repeat(results[-1]))):
            yield

    def complete_page(self, scan, cards, count):
        with self.pages([cards] * 3, [count] * 3):
            result = scan.scan_once()
        self.assertTrue(result.complete)
        return result

    def removed(self):
        return [e['nickname'] for e in self.db.get_recent_events() if e['event_type'] == 'removed']

    def test_status_duration_and_transition_timestamp(self):
        fid = self.friend(uid='123456789')
        self.db.update_status(fid, 'online', '晨风', 60, T1)
        self.assertEqual(self.db.get_friend(fid)['status_updated_at'], T0)
        self.db.update_status(fid, 'offline', '晨风', 60, T2)
        row = self.db.get_friend(fid)
        self.assertEqual(row['cumulative_online_seconds'], 120)
        self.assertEqual(row['status_updated_at'], T2)
        self.assertEqual(self.db.get_today_online_ranges(fid, '2026-09-20'), ['10:00-10:02'])

    def test_overnight_ranges_are_clipped_to_both_days(self):
        fid = self.friend(now='2026-09-19T23:58:00')
        self.db.update_status(fid, 'offline', None, None, '2026-09-20T00:02:00')
        self.assertEqual(self.db.get_today_online_ranges(fid, '2026-09-19'), ['23:58-24:00'])
        self.assertEqual(self.db.get_today_online_ranges(fid, '2026-09-20'), ['00:00-00:02'])

    def test_active_session_is_not_reported_on_unrelated_dates(self):
        fid = self.friend(now='2026-09-19T23:58:00')
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026,9,20,0,2)
        with patch('app.database.datetime', Clock):
            self.assertEqual(self.db.get_today_online_ranges(fid, '2026-09-19'), ['23:58-24:00'])
            self.assertEqual(self.db.get_today_online_ranges(fid, '2026-09-20'), ['00:00-进行中'])
            self.assertEqual(self.db.get_today_online_ranges(fid, '2026-09-21'), [])

    def test_duplicate_cards_count_each_friend_once(self):
        scan = self.scanner()
        scan._process_entry(entry(), (0,0,800,600), T0)
        scan._process_entry(entry(), (0,0,800,600), T1)
        self.assertEqual((len(scan._seen_ids), scan._online, scan._offline), (1,1,0))
        scan._process_entry(entry(status='offline'), (0,0,800,600), T2)
        self.assertEqual((len(scan._seen_ids), scan._online, scan._offline), (1,0,1))

    def test_similar_names_keep_independent_identity(self):
        fid = self.friend('旅行者甲', uid='123456789')
        scan = self.scanner()
        scan._process_entry(entry('旅行者乙'), (0,0,800,600), T1)
        self.assertEqual(len(self.db.get_all_friends()), 2)
        self.assertEqual(self.db.get_friend(fid)['nickname'], '旅行者甲')
        self.assertEqual(self.db.get_dedup_logs(), [])

    def test_legacy_merge_undo_restores_names_and_is_idempotent(self):
        fid = self.friend('旅行者甲', uid='123456789')
        self.db.update_status(fid, 'online', '旅行者乙', 60, T1)
        self.db.log_dedup('旅行者乙', fid, '旅行者甲', .75, T1)
        log_id = self.db.get_dedup_logs()[0]['id']
        self.assertTrue(self.db.undo_dedup(log_id))
        self.assertFalse(self.db.undo_dedup(log_id))
        self.assertEqual(sorted(f['nickname'] for f in self.db.get_all_friends()), ['旅行者乙','旅行者甲'])
        self.assertEqual(self.db.get_friend(fid)['uid'], '123456789')
        self.scanner()._process_entry(entry('旅行者乙'), (0,0,800,600), T2)
        self.assertEqual(len(self.db.get_all_friends()), 2)

    def test_aliases_are_exact_and_deduplicated(self):
        fid = self.friend('新昵称')
        self.db.add_nickname_alias(fid, 'Old-Name', T0)
        self.db.add_nickname_alias(fid, 'Old-Name', T1)
        self.assertEqual(self.db.find_friend_by_alias('Old-Name')[0]['id'], fid)
        self.assertIsNone(self.db.find_friend_by_alias('OldName')[0])
        self.assertEqual(self.db._conn.execute('SELECT COUNT(*) FROM nickname_aliases').fetchone()[0], 1)

    def test_uid_merge_preserves_disjoint_history(self):
        old = self.friend('旧昵称', '123456789')
        self.db.update_status(old, 'offline', None, None, T2)
        new = self.friend('新昵称', now=T3)
        self.db.update_status(new, 'online', None, None, T4)
        fid = self.scanner()._bind_uid_result('123456789', '新昵称')
        self.assertEqual(fid, old)
        self.assertEqual(len(self.db.get_all_friends()), 1)
        row = self.db.get_friend(old)
        self.assertEqual(row['cumulative_online_seconds'], 180)
        self.assertEqual(row['first_seen_at'], T0)
        self.assertEqual(row['first_online_at'], T3)
        self.assertIsNotNone(self.db.find_friend_by_alias('旧昵称')[0])

    def test_uid_merge_does_not_double_count_overlapping_history(self):
        old = self.friend('旧昵称', '123456789')
        self.db.update_status(old, 'online', None, None, T2)
        new = self.friend('新昵称', now=T1)
        self.db.update_status(new, 'online', None, None, T3)
        self.scanner()._bind_uid_result('123456789', '新昵称')
        self.assertEqual(self.db.get_friend(old)['cumulative_online_seconds'], 180)

    def test_uid_binding_creates_new_friend_and_preserves_other_uids(self):
        original = self.friend('同名', '987654321')
        fid = self.scanner()._bind_uid_result('123456789', '同名')
        self.assertNotEqual(original, fid)
        self.assertEqual(len(self.db.get_all_friends()), 2)
        self.assertEqual(self.db.get_friend(original)['uid'], '987654321')

    def test_manual_edit_rejects_invalid_identity(self):
        fid = self.friend()
        for uid, name in [('123', '晨风'), (None, '')]:
            with self.subTest(uid=uid, name=name), self.assertRaises(ValueError):
                self.db.update_friend_manual(fid, uid, name, 60, None)
        self.assertEqual(self.db.get_friend(fid)['nickname'], '晨风')

    def test_low_confidence_and_placeholders_do_not_enter_database(self):
        scan = self.scanner()
        self.assertIsNone(scan._process_entry(entry(confidence=.4), (0,0,800,600), T0))
        self.assertIsNone(scan._process_entry(entry('未识别好友@100_100'), (0,0,800,600), T0))
        self.assertEqual(self.db.get_all_friends(), [])

    def test_last_seen_is_saved_and_cleared_when_online(self):
        scan = self.scanner()
        fid = scan._process_entry(entry(status='offline', last_seen='3天前'), (0,0,800,600), T0)
        self.assertEqual(self.db.get_friend(fid)['last_seen'], '3天前')
        scan._process_entry(entry(), (0,0,800,600), T1)
        self.assertIsNone(self.db.get_friend(fid)['last_seen'])

    def test_incomplete_capture_does_not_remove_friends(self):
        a, b = self.friend(), self.friend('样例')
        scan = self.scanner()
        scan._seen_ids_prev = {a,b}
        self.shot.side_effect = [np.zeros((40,40,3), dtype=np.uint8), None]
        with self.pages([[entry()]], [2]):
            result = scan.scan_once()
        self.assertFalse(result.complete)
        self.assertEqual(self.removed(), [])
        self.assertEqual(scan._seen_ids_prev, {a,b})

    def test_removal_requires_two_complete_scans_and_records_sessions(self):
        scan = self.scanner()
        self.complete_page(scan, [entry(), entry('样例')], 2)
        self.complete_page(scan, [entry()], 1)
        self.assertEqual(self.removed(), [])
        self.complete_page(scan, [entry()], 1)
        self.assertEqual(self.removed(), ['样例'])
        self.complete_page(scan, [entry()], 1)
        self.assertEqual(self.removed(), ['样例'])
        self.assertEqual(self.db._conn.execute('SELECT COUNT(*) FROM scan_sessions').fetchone()[0], 4)

    def test_incomplete_scan_interrupts_removal_confirmation(self):
        scan = self.scanner()
        self.complete_page(scan, [entry(), entry('样例')], 2)
        self.complete_page(scan, [entry()], 1)
        with self.pages([[entry()]]*3, [2]*3):
            self.assertFalse(scan.scan_once().complete)
        self.complete_page(scan, [entry()], 1)
        self.assertEqual(self.removed(), [])

    def test_missing_header_never_infers_removal(self):
        old = self.friend('历史好友')
        scan = self.scanner()
        scan._seen_ids_prev = {old}
        with self.pages([[entry()], [entry('样例')], [entry('样例')], [entry('样例')]]):
            result = scan.scan_once()
        self.assertTrue(result.complete)
        self.assertIn('未识别好友总数', result.message)
        self.assertEqual(self.removed(), [])

    def test_scroll_failure_and_stop_cannot_be_complete(self):
        for stop in (False, True):
            with self.subTest(stop=stop):
                scan = self.scanner()
                if stop:
                    self.down.side_effect = lambda *a, **kw: (scan.stop() or True)
                else:
                    self.down.return_value = False
                with self.pages([[entry()]], [1]):
                    self.assertFalse(scan.scan_once().complete)
                self.assertEqual(self.removed(), [])

    def test_uid_start_reads_and_saves_uid(self):
        self.friend()
        scan = self.scanner()
        with self.pages([[entry()]]), patch.object(scan, '_read_uid_via_menu', return_value='123456789') as reader:
            self.assertTrue(scan.start_uid_scan())
            scan._uid_thread.join(3)
            self.assertFalse(scan.is_uid_scanning)
        reader.assert_called_once()
        self.assertIsNotNone(self.db.find_by_uid('123456789'))

    def test_uid_waits_for_previous_worker_and_blocks_new_scan(self):
        scan = self.scanner()
        entered, release, uid_entered = threading.Event(), threading.Event(), threading.Event()
        def previous():
            entered.set()
            release.wait(3)
        def uid():
            uid_entered.set()
            return {'total': 0, 'success': 0, 'complete': True}
        with patch.object(scan, '_run', side_effect=previous), patch.object(scan, 'scan_uids_once', side_effect=uid):
            try:
                self.assertTrue(scan.start())
                self.assertTrue(entered.wait(1))
                self.assertTrue(scan.start_uid_scan())
                self.assertFalse(scan.start())
                self.assertFalse(uid_entered.wait(.05))
            finally:
                release.set()
            scan._uid_thread.join(3)
            self.assertTrue(uid_entered.is_set())

    def test_cancelling_pending_uid_does_not_restart_it(self):
        scan = self.scanner()
        entered, release = threading.Event(), threading.Event()
        def previous():
            entered.set()
            release.wait(3)
        with patch.object(scan, '_run', side_effect=previous), patch.object(scan, 'scan_uids_once') as uid:
            try:
                scan.start()
                self.assertTrue(entered.wait(1))
                scan.start_uid_scan()
                scan.stop()
                scan._uid_thread.join(1)
                self.assertFalse(scan.is_uid_scanning)
                uid.assert_not_called()
            finally:
                release.set()

    def test_uid_exception_closes_opened_menu(self):
        scan = self.scanner()
        self.shot.side_effect = RuntimeError('test capture failure')
        with self.assertRaises(RuntimeError):
            scan._read_uid_via_menu(entry(), (0,0,800,600))
        self.assertEqual(game_input.press_esc.call_count, 2)

    def legacy_path(self, table='friends'):
        path = WORK / (self.id().split('.')[-1] + '.db')
        conn = sqlite3.connect(path)
        try:
            conn.execute(f'''CREATE TABLE {table} (uid TEXT PRIMARY KEY, nickname TEXT, level INTEGER,
                signature TEXT, current_status TEXT, first_seen_at TEXT, last_scan_at TEXT,
                first_online_at TEXT, total_online_seconds INTEGER, status_updated_at TEXT)''')
            conn.execute(f'INSERT INTO {table}(uid,nickname) VALUES (?,?)', ('123456789','晨风'))
            conn.commit()
        finally:
            conn.close()
        return path

    def test_legacy_migration_preserves_data_and_creates_backup(self):
        path = self.legacy_path()
        db = Database(str(path))
        try:
            self.assertEqual(db.get_all_friends()[0]['uid'], '123456789')
            self.assertIn('last_seen', db.get_all_friends()[0])
        finally:
            db.close()
        self.assertEqual(len(list(WORK.glob(path.name + '.backup-*'))), 1)
        reopened = Database(str(path))
        self.assertEqual(len(reopened.get_all_friends()), 1)
        reopened.close()

    def test_recovery_from_legacy_renamed_table(self):
        path = self.legacy_path('friends_old')
        db = Database(str(path))
        self.assertEqual(db.get_all_friends()[0]['nickname'], '晨风')
        db.close()

    def test_migration_failure_rolls_back_table_rename(self):
        path = self.legacy_path()
        with patch.object(Database, '_copy_legacy', side_effect=RuntimeError('copy failed')):
            with self.assertRaises(RuntimeError):
                Database(str(path))
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute('SELECT uid FROM friends').fetchone()[0], '123456789')
            self.assertNotIn('id', [r[1] for r in conn.execute('PRAGMA table_info(friends)')])
        finally:
            conn.close()

    def test_excel_round_trip_with_last_seen(self):
        self.db.add_friend('123456789', '晨风', 60, None, 'offline', T0, last_seen='3天前')
        dest = WORK / 'excel'
        path = excel_export.export_friends_to_excel(self.db.export_rows(), self.db.export_events(), str(dest))
        wb = load_workbook(path, read_only=True)
        try:
            self.assertEqual(wb.sheetnames, ['好友信息', '状态变动'])
            values = list(wb['好友信息'].values)
            self.assertEqual(dict(zip(values[0], values[1]))['最近登录'], '3天前')
        finally:
            wb.close()

    def test_mysql_skips_pending_uid_before_connecting(self):
        with patch('pymysql.connect') as connect:
            result = mysql_export.export_to_mysql([{'uid': None}, {'uid': 'wrong'}])
        connect.assert_not_called()
        self.assertEqual((result.written, result.skipped), (0,2))

    def test_mysql_exports_valid_uid_and_rolls_back_on_failure(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        with patch('pymysql.connect', return_value=connection):
            result = mysql_export.export_to_mysql([{'uid': '123456789'}, {'uid': None}])
        self.assertEqual((result.written, result.skipped), (1,1))
        connection.commit.assert_called_once()
        self.assertEqual(cursor.execute.call_args_list[-1].args[1][0], '123456789')
        cursor.execute.side_effect = RuntimeError('SQL failed')
        with patch('pymysql.connect', return_value=connection), self.assertRaises(RuntimeError):
            mysql_export.export_to_mysql([{'uid': '123456789'}])
        connection.rollback.assert_called_once()

    def test_parser_and_friend_count(self):
        items = [text_box('晨风',180,20,90,30), text_box('Lv.60',180,60,55,16),
                 text_box('在线',245,60,50,16), text_box('样例',180,150,90,30),
                 text_box('Lv.60',180,190,55,16), text_box('3天前在线',245,190,120,16)]
        self.assertEqual([(e.nickname,e.status) for e in parser.parse(items)], [('晨风','online'),('样例','offline')])
        self.assertEqual(parser.extract_friend_count([text_box('我的好友'), text_box('2/100',x=210)]), 2)
        self.assertIsNone(parser.extract_friend_count([text_box('旅行者2/100')]))
        self.assertIsNone(parser.extract_uid_from_text('UID 1234567890'))

    def test_ocr_filters_low_scores_and_handles_numpy_scores(self):
        # The mocked public recognize used to isolate scanner tests is bypassed here.
        from importlib import util
        spec = util.spec_from_file_location('ocr_regression_copy', ROOT / 'app/ocr_engine.py')
        module = util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = SimpleNamespace(boxes=np.array([[[0,0],[10,0],[10,10],[0,10]]]*2),
                                 txts=['可靠', '不可靠'], scores=np.array([.99,.4]))
        with patch.object(module, 'get_engine', return_value=lambda image: result):
            found = module.recognize(np.zeros((20,20,3), dtype=np.uint8))
        self.assertEqual([r.text for r in found], ['可靠'])

    def test_config_creates_file_and_ignores_method_keys(self):
        path = WORK / 'config-test.json'
        with patch.object(config, 'CONFIG_PATH', str(path)), patch.object(config.Config, 'ensure_dirs'):
            config.Config.load()
            self.assertTrue(path.is_file())
            path.write_text(json.dumps({'save': 'invalid', 'scan_interval_seconds': 30}), encoding='utf-8')
            cfg = config.Config.load()
        self.assertTrue(callable(cfg.save))
        self.assertEqual(cfg.scan_interval_seconds, 30)

    def ui(self):
        with patch.object(main_window, 'Database', return_value=self.db):
            win = main_window.MainWindow()
        self.addCleanup(win.close)
        return win

    def test_start_button_minimizes_after_task_is_accepted(self):
        win=self.ui();win.window_combo.addItem('game',123);win.show()
        with patch.object(CONFIG,'minimize_on_start',True),patch.object(win.scanner,'start',return_value=True):
            win._toggle_scan()
        self.assertTrue(win.isMinimized())
        self.assertEqual(win.start_btn.text(),'停止扫描')

    def test_rejected_start_does_not_hide_window(self):
        win=self.ui();win.window_combo.addItem('game',123);win.show()
        with patch.object(CONFIG,'minimize_on_start',True),patch.object(win.scanner,'start',return_value=False):
            win._toggle_scan()
        self.assertFalse(win.isMinimized())

    def test_retry_timing_restarts_on_each_accepted_start(self):
        win=self.ui();win.window_combo.addItem('game',123)
        with patch.object(win.scanner,'start',side_effect=[True,True,False]), \
             patch.object(main_window.time,'perf_counter',side_effect=[100.,600.,999.]), \
             patch.object(win,'_yield_to_game'):
            win._toggle_scan()
            self.assertEqual(win.scan_started_at,100.)
            win._toggle_scan()
            self.assertEqual(win.scan_started_at,600.)
            win._toggle_scan()
            self.assertEqual(win.scan_started_at,600.)

    def test_start_hands_focus_to_game_before_losing_foreground_window(self):
        win=self.ui();win.window_combo.addItem('game',123);win.show()
        observed=[]
        def activate(hwnd):
            observed.append((hwnd,win.isMinimized()))
            return True
        with patch.object(CONFIG,'bring_to_front',True), \
             patch.object(CONFIG,'minimize_on_start',True), \
             patch.object(win.scanner,'start',return_value=True), \
             patch.object(window,'bring_to_front',side_effect=activate):
            win._toggle_scan()
        self.assertEqual(observed,[(123,False)])
        self.assertTrue(win.isMinimized())

    def test_rejected_start_does_not_take_game_focus(self):
        win=self.ui();win.window_combo.addItem('game',123);win.show()
        with patch.object(CONFIG,'bring_to_front',True), \
             patch.object(win.scanner,'start',return_value=False), \
             patch.object(window,'bring_to_front') as activate:
            win._toggle_scan()
        activate.assert_not_called()

    def test_manual_region_start_does_not_focus_stale_game_window(self):
        win=self.ui();win.window_combo.addItem('old game',123);win.show()
        win._manual_rect=(10,20,1930,1100)
        with patch.object(CONFIG,'bring_to_front',True), \
             patch.object(win.scanner,'start',return_value=True), \
             patch.object(window,'bring_to_front') as activate:
            win._toggle_scan()
        activate.assert_not_called()

    def test_minimize_preference_can_keep_monitoring_window_visible(self):
        win=self.ui();win.window_combo.addItem('game',123);win.show()
        with patch.object(CONFIG,'minimize_on_start',False),patch.object(win.scanner,'start',return_value=True):
            win._toggle_scan()
        self.assertFalse(win.isMinimized())

    def test_resume_returns_focus_but_pause_keeps_controls_visible(self):
        win=self.ui();win.show()
        worker=MagicMock();worker.is_alive.return_value=True;win.scanner._thread=worker
        try:
            with patch.object(CONFIG,'minimize_on_start',True):
                win._toggle_pause()
                self.assertTrue(win.scanner.is_paused)
                self.assertFalse(win.isMinimized())
                win._toggle_pause()
                self.assertFalse(win.scanner.is_paused)
                self.assertTrue(win.isMinimized())
        finally:
            worker.is_alive.return_value=False

    def test_uid_start_also_minimizes_after_confirmation(self):
        win=self.ui();win.window_combo.addItem('game',123);win.show()
        with patch.object(CONFIG,'minimize_on_start',True), \
             patch.object(win.scanner,'start_uid_scan',return_value=True), \
             patch.object(main_window.QMessageBox,'question',return_value=main_window.QMessageBox.Yes):
            win._toggle_uid_scan()
        self.assertTrue(win.isMinimized())
        self.assertTrue(win.pause_btn.isEnabled())

    def test_ui_search_selects_all_matches(self):
        self.friend('旅行者甲')
        self.friend('旅行者乙')
        win = self.ui()
        win.search_edit.setText('旅行者')
        win._select_matching_friends()
        self.assertEqual(len(win._selected_friend_ids()), 2)

    def test_ui_manual_region_wins_over_old_window_selection(self):
        win = self.ui()
        win.window_combo.addItem('old window', 123)
        win._manual_rect = (10,20,1290,740)
        with patch.object(win.scanner, 'start', return_value=True):
            win._toggle_scan()
        self.assertEqual(win.scanner._manual_rect, (10,20,1290,740))
        self.assertIsNone(win.scanner._hwnd)

    def test_ui_close_keeps_database_open_until_workers_finish(self):
        win = self.ui()
        worker = MagicMock()
        worker.is_alive.return_value = True
        win.scanner._thread = worker
        event = QCloseEvent()
        win.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertIsNotNone(self.db._conn)
        worker.is_alive.return_value = False
        win.closeEvent(QCloseEvent())
        self.assertIsNone(self.db._conn)

    def test_scroll_failure_is_propagated_and_drag_releases_button(self):
        with patch.object(scroller, 'scroll_clicks', return_value=False):
            self.assertFalse(REAL_SCROLL_DOWN((0,0,800,600)))
        with patch.object(scroller, '_move_to', side_effect=[True, False]), patch('win32api.mouse_event') as mouse:
            self.assertFalse(REAL_DRAG((0,0,800,600), 24))
        import win32con
        self.assertEqual([call.args[0] for call in mouse.call_args_list],
                         [win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP])


    def test_unknown_status_is_not_counted_as_offline(self):
        self.friend()
        self.db.reset_runtime_state()
        self.assertEqual(self.db.get_status_summary(),
                         {'total': 1, 'online': 0, 'offline': 0, 'unknown': 1})

    def test_manual_merge_rolls_back_if_later_edit_fails(self):
        original = self.friend('旧昵称', '123456789')
        pending = self.friend('新昵称')
        with self.assertRaises(sqlite3.ProgrammingError):
            self.db.update_friend_manual(pending, '123456789', '新昵称', object(), None)
        self.assertEqual(len(self.db.get_all_friends()), 2)
        self.assertEqual(self.db.get_friend(original)['nickname'], '旧昵称')
        self.assertIsNone(self.db.get_friend(pending)['uid'])

    def test_closed_ui_ignores_late_worker_callbacks(self):
        win = self.ui()
        win.closeEvent(QCloseEvent())
        self.assertIsNone(self.db._conn)
        from app.scanner import ScanResult
        win._on_result(ScanResult(T0, 0, 0, 0))
        win._check_uid_scan_done()

    def test_uid_handoff_keeps_active_window_until_scan_stops(self):
        win = self.ui()
        win.window_combo.addItem('different window', 222)
        win.scanner._hwnd = 111
        worker = MagicMock()
        worker.is_alive.return_value = True
        win.scanner._thread = worker
        try:
            with patch.object(main_window.QMessageBox, 'question', return_value=main_window.QMessageBox.Yes), patch.object(win.scanner, 'start_uid_scan', return_value=True), patch.object(main_window.QTimer, 'singleShot'):
                win._toggle_uid_scan()
            self.assertEqual(win.scanner._hwnd, 111)
        finally:
            worker.is_alive.return_value = False



class ScreenLayoutRegressions(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in dict(ensure_friend_list=True, bring_to_front=False,
                               startup_delay_seconds=0, focus_wait_seconds=0,
                               navigation_max_esc=3, friend_open_attempts=2,
                               navigation_wait_seconds=0, ocr_confidence=.6).items():
            self.stack.enter_context(patch.object(CONFIG, key, value))
        self.stack.enter_context(patch('win32api.GetAsyncKeyState', return_value=0))
        self.stack.enter_context(patch.object(window, 'get_window_rect', return_value=(0,0,1920,1080)))
        self.front = self.stack.enter_context(patch.object(window, 'is_foreground', return_value=True))
        self.permission = self.stack.enter_context(patch.object(window, 'input_permission_error', return_value=None))
        self.frame = np.full((1080,1920,3), 40, dtype=np.uint8)
        self.shot = self.stack.enter_context(patch.object(capture, 'capture_region', return_value=self.frame))
        self.ocr = self.stack.enter_context(patch.object(ocr_engine, 'recognize', return_value=[]))
        self.keys = []
        self.stack.enter_context(patch.object(game_input, 'press_key', side_effect=lambda key, **kw: self.keys.append(chr(key))))
        self.stack.enter_context(patch.object(game_input, 'press_esc', side_effect=lambda **kw: self.keys.append('ESC')))
        self.stack.enter_context(patch.object(game_input, 'click'))
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.scan = Scanner(self.db)
        self.scan.set_window(123)

        # These navigation regressions isolate key sequencing; HUD mode OCR and
        # its two-frame confirmation have separate Qianxing navigation tests.
        self.stack.enter_context(patch.object(self.scan, '_read_interface_mode', return_value='normal'))
        self.stack.enter_context(patch.object(window, 'find_client_window', return_value=None))

    def cards(self, bands=((116,231),(242,357))):
        frame = self.frame.copy()
        for top,bottom in bands:
            frame[top:bottom,136:1788] = 185
        return frame

    def fields(self, nickname='晨风', status='在线', level=60):
        return [text_box(nickname,10,14,120,38), text_box(f'Lv.{level}',10,71,72,23),
                text_box(status,125,71,120,23)]

    def header(self):
        return [text_box('好友 / 我的好友',10,15,225,32)]

    def test_title_requires_breadcrumb_in_fixed_upper_left(self):
        self.ocr.return_value = [text_box('好友',10,15,70,32)]
        self.assertFalse(friend_screen.is_friend_list(self.frame))
        self.ocr.return_value = [text_box('好友 / 添加好友',10,15,225,32)]
        self.assertFalse(friend_screen.is_friend_list(self.frame))
        self.ocr.return_value = self.header()
        self.assertTrue(friend_screen.is_friend_list(self.frame))
        actual = self.ocr.call_args.args[0]
        self.assertEqual(actual.shape, (72,375,3))

    def test_friend_count_can_precede_title_in_ocr_sort_order(self):
        items = [text_box('148/200',154,18,123,36), text_box('我的好友',47,18.67,107,36)]
        self.assertEqual(parser.extract_friend_count(items),148)
        self.assertIsNone(parser.extract_friend_count([items[0],text_box('我的好友',47,90,107,36)]))

    def test_split_title_and_ocr_slash_are_supported(self):
        self.ocr.return_value = [text_box('我的好友',95,16,130,32), text_box('好友',10,15,65,32),
                                 text_box('I',80,17,10,30)]
        self.assertTrue(friend_screen.is_friend_list(self.frame))
        self.ocr.return_value[0].confidence = .4
        self.assertFalse(friend_screen.is_friend_list(self.frame))

    def test_misleading_friend_text_cannot_enable_card_ocr(self):
        self.ocr.return_value = [text_box('旅行者好友',10,15,180,32)]
        result = friend_screen.read_page(self.cards())
        self.assertFalse(result.confirmed)
        self.assertEqual(result.entries, [])
        self.assertEqual(self.ocr.call_count, 1)

    def test_non_16_9_and_partial_list_crops_are_rejected(self):
        for shape in ((600,800,3),(600,1600,3),(80,120,3)):
            with self.subTest(shape=shape):
                self.assertIsNone(friend_screen.normalize(np.zeros(shape,dtype=np.uint8)))
        self.ocr.assert_not_called()

    def test_complete_cards_are_separated_and_partial_card_is_skipped(self):
        frame = self.cards(((116,231),(242,357),(870,966)))
        bands = friend_screen.detect_card_bands(frame)
        self.assertEqual([(b.top,b.bottom) for b in bands], [(116,231),(242,357)])

    def test_card_edges_follow_scroll_offset(self):
        frame = self.cards(((112,183),(194,309),(320,435)))
        bands = friend_screen.detect_card_bands(frame)
        self.assertEqual([(b.top,b.bottom) for b in bands], [(194,309),(320,435)])

    def test_previous_bottom_edge_cannot_start_a_partial_next_card(self):
        frame=self.cards(((689,804),(815,911)))
        bands=friend_screen.detect_card_bands(frame)
        self.assertEqual([(b.top,b.bottom) for b in bands],[(689,804)])

    def test_proportional_layout_at_1080p_1440p_2160p(self):
        import cv2
        for size in ((1920,1080),(2560,1440),(3840,2160)):
            with self.subTest(size=size):
                image = cv2.resize(self.cards(),size)
                found = friend_screen.detect_card_bands(friend_screen.normalize(image))
                self.assertEqual(len(found), 2)
                self.assertLessEqual(abs(found[0].top-116),1)
                self.assertLessEqual(abs(found[1].bottom-357),1)

    def test_single_card_fields_keep_name_level_and_state_together(self):
        a = parser.parse_card(self.fields('旅行者甲','在线',58),115,194,175)
        b = parser.parse_card(self.fields('旅行者乙','3天前在线',60),115,194,301)
        self.assertEqual((a.nickname,a.level,a.status,a.last_seen),('旅行者甲',58,'online',None))
        self.assertEqual((b.nickname,b.level,b.status,b.last_seen),('旅行者乙',60,'offline','3天前'))

    def test_name_can_contain_status_words_or_numbers(self):
        for name in ('一直在线','12345','<^_^>','%','^人^੭'):
            self.assertEqual(parser.parse_card(self.fields(name),115,1,2).nickname,name)

    def test_missing_nickname_cannot_be_replaced_by_status_or_signature(self):
        self.assertIsNone(parser.parse_card(self.fields()[1:],115,1,2))
        self.assertIsNone(parser.parse_card([text_box('签名',330,71,120,23)],115,1,2))

    def test_conflicting_status_missing_level_or_unknown_state_is_skipped(self):
        for fields in (self.fields()+[text_box('离线',260,71,80,23)],
                       self.fields(status='在线 离线'), self.fields(status='无法识别'),
                       self.fields(level=99), [self.fields()[0],self.fields()[2]]):
            with self.subTest(fields=fields):
                self.assertIsNone(parser.parse_card(fields,115,1,2))

    def test_multiple_name_lines_or_text_spanning_slots_is_rejected(self):
        self.assertIsNone(parser.parse_card(self.fields()+[text_box('另一个人',150,43,100,16)],115,1,2))
        fields=self.fields();fields[0]=text_box('跨昵称状态两行',10,4,180,85)
        self.assertIsNone(parser.parse_card(fields,115,1,2))

    def test_separate_cards_and_avatar_coordinates_are_scaled(self):
        self.ocr.side_effect = [self.header(),[text_box('我的好友 2/200')],
                                self.fields('旅行者甲','在线',58),self.fields('旅行者乙','今日内',60)]
        import cv2
        page = friend_screen.read_page(cv2.resize(self.cards(),(3840,2160)),left_offset=100,top_offset=50)
        self.assertTrue(page.reliable)
        self.assertEqual(page.expected_total,2)
        self.assertEqual([(e.nickname,e.level,e.status) for e in page.entries],
                         [('旅行者甲',58,'online'),('旅行者乙',60,'offline')])
        self.assertEqual(page.entries[0].click_x,488)
        self.assertLessEqual(abs(page.entries[1].click_y-649),2)
        for call in self.ocr.call_args_list[2:]:
            self.assertEqual(call.args[0].shape[1],friend_screen.TEXT_RIGHT-friend_screen.TEXT_LEFT)
            self.assertLess(call.args[0].shape[0],125)

    def test_same_name_cards_are_not_collapsed_into_one_person(self):
        self.ocr.side_effect = [self.header(),[text_box('我的好友 2/200')],self.fields(),self.fields(status='今日内')]
        page = friend_screen.read_page(self.cards())
        self.assertFalse(page.reliable)
        self.assertEqual(page.entries,[])
        self.assertTrue(any('同名' in issue for issue in page.issues))

    def test_cancellation_between_cards_discards_remaining_work(self):
        self.ocr.side_effect = [self.header(),[text_box('我的好友 2/200')],self.fields()]
        page = friend_screen.read_page(self.cards(),should_continue=MagicMock(side_effect=[True,False]))
        self.assertEqual(len(page.entries),1)
        self.assertFalse(page.reliable)
        self.assertEqual(self.ocr.call_count,3)

    def test_hud_accepts_enter_with_or_without_chat_icon(self):
        from PIL import Image
        frame = self.frame.copy()
        icon = np.array(Image.open(ROOT/'app/assets/chat_icon.png').convert('RGB'))
        frame[1005:1045,15:64]=icon
        self.ocr.return_value = [text_box('Enter',77,1006-friend_screen.ENTER_RECT[1],88,37)]
        self.assertTrue(friend_screen.is_gameplay_hud(frame))
        self.assertTrue(friend_screen.is_gameplay_hud(self.frame))
        self.ocr.return_value=[]
        self.assertFalse(friend_screen.is_gameplay_hud(frame))

    def test_hud_requires_confident_standalone_enter_in_bottom_left_crop(self):
        for text,score in [('Enter',.59),('Enter聊天',.99),('PressEnter',.99),('确认',.99)]:
            self.ocr.return_value=[text_box(text,score=score)]
            self.assertFalse(friend_screen.is_gameplay_hud(self.frame))
        self.ocr.return_value=[text_box('ENTER',score=.99)]
        self.assertTrue(friend_screen.is_gameplay_hud(self.frame))
        self.assertEqual(self.ocr.call_args.args[0].shape,(216,310,3))

    def test_already_in_friend_list_reopens_after_confirming_hud_mode(self):
        with patch.object(friend_screen,'is_friend_list',side_effect=[True,False,True]), \
             patch.object(friend_screen,'is_gameplay_hud',return_value=True):
            self.assertTrue(self.scan._ensure_friend_list((0,0,1920,1080)))
        self.assertEqual(self.keys,['ESC','O'])
        self.assertEqual(self.scan.interface_mode,'normal')

    def test_navigation_esc_until_hud_then_o_and_verify_title(self):
        with patch.object(friend_screen,'is_friend_list',side_effect=[False,False,False,True]), \
             patch.object(friend_screen,'is_gameplay_hud',side_effect=[False,False,True]):
            self.assertTrue(self.scan._ensure_friend_list((0,0,1920,1080)))
        self.assertEqual(self.keys,['ESC','ESC','O'])
        self.assertEqual(self.shot.call_count,4)

    def test_navigation_stops_at_esc_limit_without_pressing_o(self):
        with patch.object(friend_screen,'is_friend_list',return_value=False), \
             patch.object(friend_screen,'is_gameplay_hud',return_value=False):
            self.assertFalse(self.scan._ensure_friend_list((0,0,1920,1080)))
        self.assertEqual(self.keys,['ESC']*3)
        self.assertEqual(self.shot.call_count,4)

    def test_o_retries_require_hud_and_have_a_limit(self):
        with patch.object(friend_screen,'is_friend_list',return_value=False), \
             patch.object(friend_screen,'is_gameplay_hud',return_value=True):
            self.assertFalse(self.scan._ensure_friend_list((0,0,1920,1080)))
        self.assertEqual(self.keys,['O','O'])

    def test_navigation_sends_no_keys_on_capture_failure(self):
        self.shot.return_value=None
        self.assertFalse(self.scan._ensure_friend_list((0,0,1920,1080)))
        self.assertEqual(self.keys,[])

    def test_navigation_stops_if_window_loses_focus(self):
        self.front.return_value=False
        with patch.object(friend_screen,'is_friend_list',return_value=False), \
             patch.object(friend_screen,'is_gameplay_hud',return_value=False):
            self.assertFalse(self.scan._ensure_friend_list((0,0,1920,1080)))
        self.assertEqual(self.keys,[])

    def test_focus_is_rechecked_after_pause(self):
        self.front.return_value=False
        with patch.object(self.scan,'_check_pause',return_value=True) as pause:
            self.assertFalse(self.scan._navigation_key(0x4F))
        pause.assert_called_once()
        self.assertEqual(self.keys,[])

    def test_manual_crop_never_sends_global_navigation_keys(self):
        self.scan.set_manual_rect((0,0,1920,1080))
        with patch.object(friend_screen,'is_friend_list',return_value=False):
            self.assertFalse(self.scan._ensure_friend_list((0,0,1920,1080)))
        self.assertEqual(self.keys,[])

    def test_disabling_navigation_does_not_disable_title_validation(self):
        with patch.object(CONFIG,'ensure_friend_list',False), \
             patch.object(friend_screen,'is_friend_list',return_value=False), \
             patch.object(friend_screen,'read_page') as reader:
            result=self.scan.scan_once()
        self.assertFalse(result.complete)
        self.assertEqual(self.keys,[])
        reader.assert_not_called()
        self.assertEqual(self.db.get_all_friends(),[])

    def test_stop_interrupts_navigation_without_further_keys(self):
        self.scan._stop.set()
        self.assertFalse(self.scan._ensure_friend_list((0,0,1920,1080)))
        self.assertEqual(self.keys,[])
        self.shot.assert_not_called()

    def test_each_page_is_validated_before_updating_friends(self):
        first=friend_screen.FriendPage(confirmed=True,expected_total=2,entries=[entry()])
        wrong=friend_screen.FriendPage(confirmed=False,entries=[entry('不应入库')],issues=['标题消失'])
        with patch.object(self.scan,'_ensure_friend_list',return_value=True), \
             patch.object(scroller,'scroll_to_top',return_value=True), \
             patch.object(scroller,'scroll_down',return_value=True) as down, \
             patch.object(friend_screen,'read_page',side_effect=[first,wrong]):
            result=self.scan.scan_once()
        self.assertFalse(result.complete)
        self.assertIn('标题消失',result.message)
        self.assertEqual([f['nickname'] for f in self.db.get_all_friends()],['晨风'])
        down.assert_called_once()
        self.assertEqual(self.db._conn.execute('SELECT COUNT(*) FROM scan_sessions').fetchone()[0],0)

    def test_permission_failure_prevents_scan_and_preserves_message(self):
        message='游戏以管理员权限运行，请使用 start-admin.bat'
        self.permission.return_value=message
        with patch.object(scroller,'scroll_to_top') as scroll,patch.object(friend_screen,'read_page') as read:
            result=self.scan.scan_once()
        self.assertFalse(result.complete)
        self.assertEqual(result.message,message)
        self.assertEqual(self.keys,[])
        scroll.assert_not_called();read.assert_not_called()
        self.assertEqual(self.db.get_all_friends(),[])

    def test_permission_failure_prevents_uid_clicks(self):
        self.permission.return_value='需要管理员启动'
        with patch.object(self.scan,'_read_uid_via_menu') as click:
            result=self.scan.scan_uids_once()
        self.assertFalse(result['complete'])
        self.assertEqual(result['message'],'需要管理员启动')
        click.assert_not_called()

    def test_navigation_send_failure_is_preserved_in_scan_result(self):
        with patch.object(friend_screen,'is_friend_list',return_value=False), \
             patch.object(friend_screen,'is_gameplay_hud',return_value=True), \
             patch.object(game_input,'press_key',side_effect=game_input.InputError('系统拒绝 O 按键')):
            result=self.scan.scan_once()
        self.assertFalse(result.complete)
        self.assertEqual(result.message,'系统拒绝 O 按键')
        self.assertEqual(self.shot.call_count,1)

    def fake_focus_clock(self):
        clock=SimpleNamespace(now=0.0,waited=0.0)
        self.stack.enter_context(patch('app.scanner.time.monotonic',side_effect=lambda:clock.now))
        def wait(seconds):
            clock.now+=max(seconds,.000001)
            clock.waited+=max(seconds,.000001)
            return not self.scan._active_stop().is_set()
        self.stack.enter_context(patch.object(self.scan,'_wait',side_effect=wait))
        return clock

    def test_failed_activation_waits_for_switch_without_restarting_task(self):
        clock=self.fake_focus_clock()
        self.front.side_effect=lambda hwnd:clock.now>=.4
        messages=[];self.scan.on_message=messages.append
        with patch.object(CONFIG,'focus_wait_seconds',3),patch.object(CONFIG,'bring_to_front',True), \
             patch.object(window,'bring_to_front',return_value=False) as activate:
            self.assertTrue(self.scan._wait_for_game_foreground(activate=True))
        activate.assert_called_once_with(123)
        self.assertTrue(any('无需再次点击开始' in msg for msg in messages))
        self.assertTrue(any('继续当前任务' in msg for msg in messages))
        self.assertLess(clock.now,3)
        self.shot.assert_not_called()
        self.assertEqual(self.keys,[])

    def test_focus_wait_timeout_sends_no_inputs(self):
        clock=self.fake_focus_clock();self.front.return_value=False
        with patch.object(CONFIG,'focus_wait_seconds',.3):
            self.assertFalse(self.scan._wait_for_game_foreground())
        self.assertIn('超时',self.scan._navigation_error)
        self.assertGreaterEqual(clock.now,.3)
        self.assertLess(clock.now,.5)
        self.shot.assert_not_called();self.assertEqual(self.keys,[])

    def test_stop_cancels_focus_wait_immediately(self):
        self.front.return_value=False
        def stop_wait(seconds):
            self.scan.stop()
            return False
        with patch.object(CONFIG,'focus_wait_seconds',30),patch.object(self.scan,'_wait',side_effect=stop_wait):
            self.assertFalse(self.scan._wait_for_game_foreground())
        self.assertIn('停止',self.scan._navigation_error)
        self.assertEqual(self.keys,[])

    def test_pause_time_does_not_consume_focus_wait_timeout(self):
        clock=self.fake_focus_clock()
        self.front.side_effect=lambda hwnd:clock.waited>=.2
        checks=[0]
        def pause():
            checks[0]+=1
            if checks[0]==2:clock.now+=100
            return True
        with patch.object(CONFIG,'focus_wait_seconds',1),patch.object(self.scan,'_check_pause',side_effect=pause):
            self.assertTrue(self.scan._wait_for_game_foreground())
        self.assertGreater(clock.now,100)
        self.assertLess(clock.waited,1)

    def test_focus_wait_detects_destroyed_window(self):
        self.front.return_value=False
        with patch.object(window,'get_window_rect',return_value=None),patch.object(CONFIG,'focus_wait_seconds',30):
            self.assertFalse(self.scan._wait_for_game_foreground())
        self.assertIn('失效',self.scan._navigation_error)
        self.assertEqual(self.keys,[])

    def test_already_focused_game_needs_no_wait_or_activation(self):
        with patch.object(self.scan,'_wait') as wait,patch.object(window,'bring_to_front') as activate:
            self.assertTrue(self.scan._wait_for_game_foreground(activate=True))
        wait.assert_not_called();activate.assert_not_called()

    def test_startup_countdown_gives_time_without_inputs(self):
        clock=self.fake_focus_clock();messages=[];self.scan.on_message=messages.append
        with patch.object(CONFIG,'startup_delay_seconds',3):
            self.assertTrue(self.scan._startup_countdown())
        self.assertAlmostEqual(clock.waited,3,places=5)
        self.assertLess(clock.waited,3.1)
        self.assertTrue(any('3 秒' in msg for msg in messages))
        self.assertTrue(any('1 秒' in msg for msg in messages))
        self.shot.assert_not_called();self.assertEqual(self.keys,[])

    def test_cancelled_countdown_never_starts_scan(self):
        with patch.object(self.scan,'_startup_countdown',return_value=False),patch.object(self.scan,'scan_once') as scan:
            self.scan._run()
        scan.assert_not_called()
        self.shot.assert_not_called();self.assertEqual(self.keys,[])

    def test_uid_handoff_also_obeys_countdown_cancellation(self):
        with patch.object(self.scan,'_startup_countdown',return_value=False),patch.object(self.scan,'scan_uids_once') as scan:
            self.scan._run_uid_scan()
        scan.assert_not_called()
        self.assertEqual(self.keys,[])

    def test_countdown_runs_once_for_repeated_scans(self):
        calls=[]
        def scan_once():
            calls.append(1)
            if len(calls)==2:self.scan.stop()
            return self.scan._build_result(T0, True, '')
        with patch.object(self.scan,'_startup_countdown',return_value=True) as countdown, \
             patch.object(CONFIG,'scan_frequency_minutes',0), \
             patch.object(self.scan,'scan_once',side_effect=scan_once),patch.object(self.scan._stop,'wait',return_value=False):
            self.scan._run()
        self.assertEqual(len(calls),2)
        countdown.assert_called_once()

    def test_scan_continues_same_round_after_user_switches_back(self):
        clock=self.fake_focus_clock();self.front.side_effect=lambda hwnd:clock.now>=.5
        page=friend_screen.FriendPage(confirmed=True,expected_total=0)
        shots=[]
        def screenshot(*args):
            shots.append(clock.now)
            return self.frame
        with patch.object(CONFIG,'focus_wait_seconds',3),patch.object(CONFIG,'bring_to_front',True), \
             patch.object(window,'bring_to_front',return_value=False) as activate, \
             patch.object(self.scan,'_ensure_friend_list',return_value=True), \
             patch.object(friend_screen,'read_page',return_value=page), \
             patch.object(capture,'capture_region',side_effect=screenshot), \
             patch.object(scroller,'scroll_to_top',return_value=True), \
             patch.object(scroller,'scroll_down',return_value=True):
            result=self.scan.scan_once()
        self.assertTrue(result.complete)
        self.assertTrue(shots and all(t>=.5 for t in shots))
        activate.assert_called_once()

    def test_uid_scan_continues_after_failed_activation(self):
        clock=self.fake_focus_clock();self.front.side_effect=lambda hwnd:clock.now>=.5
        page=friend_screen.FriendPage(confirmed=True,expected_total=0)
        with patch.object(CONFIG,'focus_wait_seconds',3),patch.object(CONFIG,'bring_to_front',True), \
             patch.object(window,'bring_to_front',return_value=False) as activate, \
             patch.object(self.scan,'_ensure_friend_list',return_value=True), \
             patch.object(friend_screen,'read_page',return_value=page), \
             patch.object(scroller,'scroll_to_top',return_value=True), \
             patch.object(scroller,'scroll_down',return_value=True):
            summary=self.scan.scan_uids_once()
        self.assertTrue(summary['complete'])
        activate.assert_called_once()
        self.assertEqual(self.keys,[])

    def test_foreground_flicker_cannot_extend_timeout_indefinitely(self):
        clock=self.fake_focus_clock();self.front.side_effect=[False,True,False,True,False,False]
        with patch.object(CONFIG,'focus_wait_seconds',.5):
            self.assertFalse(self.scan._wait_for_game_foreground())
        self.assertLessEqual(clock.waited,.50001)
        self.assertIn('超时',self.scan._navigation_error)

    def test_running_focus_wait_can_be_stopped_without_waiting_for_timeout(self):
        waiting=threading.Event();self.front.return_value=False
        self.scan.on_message=lambda msg:waiting.set() if '等待游戏进入前台' in msg else None
        with patch.object(CONFIG,'focus_wait_seconds',30),patch.object(CONFIG,'startup_delay_seconds',0):
            try:
                self.assertTrue(self.scan.start())
                self.assertTrue(waiting.wait(1))
            finally:
                self.scan.stop()
                if self.scan._thread is not None:self.scan._thread.join(1)
            self.assertFalse(self.scan.is_running)
        self.shot.assert_not_called();self.assertEqual(self.keys,[])

    def test_client_area_excludes_window_title_and_frame(self):
        import win32gui
        # 使用未被 setUp 替换的实际函数副本。
        from importlib import util
        spec=util.spec_from_file_location('window_layout_copy',ROOT/'app/window.py')
        module=util.module_from_spec(spec);spec.loader.exec_module(module)
        with patch.object(win32gui,'GetClientRect',return_value=(0,0,1920,1080)), \
             patch.object(win32gui,'ClientToScreen',side_effect=[(108,131),(2028,1211)]):
            self.assertEqual(module.get_window_rect(123),(108,131,2028,1211))



class FastScanRegressions(unittest.TestCase):
    def setUp(self):
        from app import fast_layout, fast_scan
        self.layout, self.fast = fast_layout, fast_scan
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.work = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.work.cleanup)
        for key, value in dict(fast_scan=True, fast_ocr=True, save_scan_evidence=False,
                               log_dir=self.work.name, startup_delay_seconds=0,
                               focus_wait_seconds=0, max_scrolls=80).items():
            self.stack.enter_context(patch.object(CONFIG, key, value))
        self.db = Database(str(Path(self.work.name)/'test.db'))
        self.addCleanup(self.db.close)
        self.scan = Scanner(self.db)
        self.scan.set_manual_rect((0,0,1920,1080))
        self.scan._check_pause = lambda: not self.scan._stop.is_set()
        self.scan._wait_for_game_foreground = lambda **kw: not self.scan._stop.is_set()
        self.scan._say = MagicMock()
        # Synthetic list frames intentionally have no account HUD; account OCR and
        # cross-account rejection are covered separately with explicit owner UIDs.
        self.stack.enter_context(patch.object(self.scan, '_verify_account_frame', return_value=None))

    def simulation(self, count=148, move_rows=6, reject=None, variant=False, drag_overshoot=None):
        import cv2
        L=self.layout
        state={'offset':0., 'page':0, 'reads':0, 'commands':[], 'corrections':0}
        maximum=max(0.,116+(count-1)*L.ROW_PITCH+L.ROW_HEIGHT-L.VIEW[3])
        def frame():
            img=np.full((1080,1920,3),60,dtype=np.uint8)
            for index in range(count):
                y=round(116+index*L.ROW_PITCH-state['offset'])
                if y+115<112 or y>958: continue
                card=np.full((115,1652,3),205,dtype=np.uint8)
                rng=np.random.default_rng(index)
                card[10:105,22:96]=rng.integers(0,255,(95,74,3),dtype=np.uint8)
                cv2.putText(card,'friend%03d'%index,(142,47),cv2.FONT_HERSHEY_SIMPLEX,.9,(60,60,70),2)
                cv2.putText(card,'Lv.60',(142,88),cv2.FONT_HERSHEY_SIMPLEX,.6,(60,60,70),1)
                y1,y2=max(112,y),min(958,y+115)
                if y2>y1: img[y1:y2,136:1788]=card[y1-y:y2-y]
            top=round(118+(state['offset']/maximum if maximum else 0)*771)
            img[top:top+54,1802:1807]=195
            state['frame']=img
            return img
        def read(image,**kwargs):
            state['reads']+=1
            page=friend_screen.FriendPage(confirmed=True,expected_total=count)
            for index in range(count):
                y=round(116+index*L.ROW_PITCH-state['offset'])
                if y<112 or y+115>958: continue
                band=friend_screen.CardBand(y,y+115)
                name='好友%03d'%index
                if variant and index==143 and state['offset']>17000: name+='.'
                ent=entry(name) if index != reject else None
                obs=L.CardObservation(band,ent,L.crop(image,L.field_rect(band,L.NAME)),
                                      L.crop(image,(158,y+10,232,y+105)),issue='' if ent else 'mock rejected')
                page.observations.append(obs)
                page.bands.append(band)
                if ent: page.entries.append(ent)
            return page
        def down(*args,**kw):
            state['page']+=1
            distance=kw.get('distance') or 6*L.ROW_PITCH
            state['commands'].append(distance)
            amount=(distance+drag_overshoot if drag_overshoot is not None else move_rows*L.ROW_PITCH)
            state['offset']=max(0.,min(maximum,state['offset']+amount))
            return True
        def reverse(rect,start,end,**kw):
            if drag_overshoot is not None and start[0] == L.DRAG_X:
                state['corrections']+=1
                state['offset']=max(0.,min(maximum,state['offset']-(end[1]-start[1])))
            return True
        self.stack.enter_context(patch.object(capture,'capture_region',side_effect=lambda *a:frame()))
        self.stack.enter_context(patch.object(L,'is_friend_list',return_value=True))
        self.stack.enter_context(patch.object(L,'read_page',side_effect=read))
        self.stack.enter_context(patch.object(scroller,'drag_six_rows',side_effect=down))
        self.stack.enter_context(patch.object(scroller,'drag_client',side_effect=reverse))
        self.stack.enter_context(patch.object(ocr_engine,'recognize',return_value=[]))
        return state

    def test_rejected_nickname_crop_is_saved_even_without_full_page_evidence(self):
        run=self.fast.FastScan(self.scan)
        name=np.random.default_rng(2).integers(0,255,(57,564,3),dtype=np.uint8)
        audit={'method':'rejected','original':['name.',.82],
               'retries':[['name。',.86],['name.',.84]],'reason':'ambiguous punctuation'}
        obs=self.layout.CardObservation(friend_screen.CardBand(116,231),None,name,name,
            fields=[('',0.),('Lv.60',.99),('在线',.99)],issue='nickname rejected',name_audit=audit)
        page=friend_screen.FriendPage(confirmed=True,expected_total=1,observations=[obs])
        run.expected=1;run.slots[0]=obs
        run.record_page(np.full((1080,1920,3),205,np.uint8),page,[0],None)
        self.assertFalse((run.evidence/'page-000.png').exists())
        self.assertEqual(len(run.nickname_reviews),1)
        paths=run.nickname_reviews[0]
        np.testing.assert_array_equal(self.fast._decode(Path(paths['image']).read_bytes()),name)
        record=json.loads(Path(paths['record']).read_text(encoding='utf8'))
        self.assertEqual((record['page'],record['traversal_position']),(1,1))
        self.assertEqual(record['name_audit']['retries'],audit['retries'])
        self.assertEqual(record['status'],'rejected')
        self.assertIsNone(obs.entry)
        result=run.finish('nickname rejected')
        self.assertFalse(result.complete)
        self.assertEqual(self.db.get_all_friends(),[])
        report=json.loads((run.evidence/'result.json').read_text(encoding='utf8'))
        self.assertEqual(report['nickname_reviews'],[paths])

    def test_confirmed_nicknames_do_not_create_review_crops(self):
        run=self.fast.FastScan(self.scan)
        image=np.zeros((57,564,3),np.uint8)
        obs=self.layout.CardObservation(friend_screen.CardBand(116,231),entry('name'),image,image,
            fields=[('name',.99),('Lv.60',.99),('在线',.99)])
        page=friend_screen.FriendPage(observations=[obs])
        run.record_nickname_reviews(page,[0],0)
        self.assertEqual(run.nickname_reviews,[])
        self.assertFalse((Path(CONFIG.log_dir)/'nickname-review').exists())

    def test_accepted_but_low_confidence_nickname_keeps_review_evidence(self):
        run=self.fast.FastScan(self.scan)
        image=np.zeros((57,564,3),np.uint8)
        obs=self.layout.CardObservation(friend_screen.CardBand(116,231),entry('name',confidence=.82),image,image,
            fields=[('name',.82),('Lv.60',.99),('在线',.99)],name_audit={'method':'tight_ocr'})
        run.record_nickname_reviews(friend_screen.FriendPage(observations=[obs]),[4],2)
        record=json.loads(Path(run.nickname_reviews[0]['record']).read_text(encoding='utf8'))
        self.assertEqual(record['status'],'low_confidence')
        self.assertEqual((record['page'],record['traversal_position']),(3,5))
        self.assertEqual(obs.entry.nickname,'name')

    def test_real_image_registration_covers_148_rows_in_25_pages(self):
        state=self.simulation()
        result=self.fast.FastScan(self.scan).run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(result.total,148)
        self.assertEqual(len(self.db.get_display_friends()),148)
        self.assertEqual(state['reads'],25)
        self.assertEqual(len({r['id'] for r in self.db.get_display_friends()}),148)

    def test_zero_movement_stops_without_repeating_ocr_or_writing_records(self):
        state=self.simulation(move_rows=0)
        with patch.object(self.scan,'_wait',return_value=True):
            result=self.fast.FastScan(self.scan).run()
        self.assertFalse(result.complete)
        self.assertEqual(state['reads'],1)
        self.assertEqual(self.db.get_all_friends(),[])

    def test_drag_inertia_is_corrected_before_ocr_and_all_148_rows_are_covered(self):
        state=self.simulation(drag_overshoot=50)
        with patch.object(self.scan,'_wait',return_value=True):
            result=self.fast.FastScan(self.scan).run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(result.total,148)
        self.assertLessEqual(state['reads'],32)
        self.assertLessEqual(state['corrections'],1)
        self.assertEqual(len({r['id'] for r in self.db.get_display_friends()}),148)
        self.assertLess(state['commands'][0],6*self.layout.ROW_PITCH)

    def test_clipped_initial_drag_learns_bias_from_measured_reverse_moves(self):
        self.simulation(drag_overshoot=90)
        render=capture.capture_region.side_effect
        def faded_frame(*args):
            image=render(*args)
            image[878:958,:]=225
            return image
        capture.capture_region.side_effect=faded_frame
        run=self.fast.FastScan(self.scan)
        with patch.object(self.scan,'_wait',return_value=True):
            rect,before=run.home()
            page=self.layout.read_page(before)
            run.add_page(before,page)
            _,after,move=run.advance(rect,before)
            run.add_page(after,self.layout.read_page(after))
        self.assertEqual(set(run.slots),set(range(max(run.slots)+1)))
        self.assertAlmostEqual(move['initial']['recovered_pixels']-move['commanded_pixels'],90,delta=2)
        self.assertGreater(run.drag_bias,40)

    def test_hover_horizontal_motion_preserves_vertical_registration(self):
        import cv2
        state=self.simulation()
        before=capture.capture_region(0,0,1920,1080)
        state['offset']=6*self.layout.ROW_PITCH
        after=capture.capture_region(0,0,1920,1080)
        after=cv2.warpAffine(after,np.float32([[1,0,-8],[0,1,0]]),(1920,1080))
        pixels,score=self.layout.scroll_displacement(before,after)
        self.assertAlmostEqual(pixels,6*self.layout.ROW_PITCH,delta=1)
        self.assertGreater(score,.95)

    def test_blank_fragments_cannot_verify_movement(self):
        blank=np.full((1080,1920,3),205,dtype=np.uint8)
        self.assertEqual(self.layout.scroll_displacement(blank,blank),(0.,0.))

    def faded_overlap(self):
        state=self.simulation()
        before=capture.capture_region(0,0,1920,1080)
        state['offset']=554
        after=capture.capture_region(0,0,1920,1080)
        # A fixed viewport overlay erases the former bottom template, while
        # complete shared cards above it still provide identity evidence.
        before[878:958,:]=225
        after[878:958,:]=225
        return before,after

    def test_complete_shared_cards_verify_movement_when_bottom_is_faded(self):
        before,after=self.faded_overlap()
        pixels,score=self.layout.scroll_displacement(before,after,expected=568)
        self.assertAlmostEqual(pixels,554,delta=1)
        self.assertGreater(score,.95)

    def test_avatar_alone_cannot_match_a_faded_overlap(self):
        before,after=self.faded_overlap()
        after[:,266:830]=205
        _,score=self.layout.scroll_displacement(before,after,expected=568)
        self.assertLess(score,.67)

    def test_nickname_alone_cannot_match_a_faded_overlap(self):
        before,after=self.faded_overlap()
        after[:,148:243]=205
        _,score=self.layout.scroll_displacement(before,after,expected=568)
        self.assertLess(score,.67)

    def clipped_overlap_with_avatar_change(self):
        # The fixed 878px template moves partly behind the header. A narrow
        # avatar change prevents the existing two-field fallback from matching.
        rng=np.random.default_rng(10)
        before=np.full((1080,1920,3),205,np.uint8)
        after=before.copy()
        texture=rng.integers(0,255,(80,675,3),dtype=np.uint8)
        before[865:945,160:835]=texture
        after[112:173,160:835]=texture[19:80]
        after[112:173,150:243]=rng.integers(0,255,(61,93,3),dtype=np.uint8)
        return before,after

    def test_clipped_overlap_is_verified_with_a_stable_thumb_bound(self):
        before,after=self.clipped_overlap_with_avatar_change()
        pixels,score=self.layout.scroll_displacement(before,after,expected=780)
        self.assertEqual(pixels,772)
        self.assertGreaterEqual(score,.85)

    def test_short_overlap_cannot_override_a_conflicting_thumb_bound(self):
        before,after=self.clipped_overlap_with_avatar_change()
        _,score=self.layout.scroll_displacement(before,after,expected=650)
        self.assertLess(score,.67)

    def test_short_overlap_without_an_independent_bound_is_rejected(self):
        before,after=self.clipped_overlap_with_avatar_change()
        _,score=self.layout.scroll_displacement(before,after)
        self.assertLess(score,.67)

    def test_status_crop_keeps_light_dark_and_detached_ink(self):
        import cv2
        for ink in (50,235):
            image=np.full((41,445,3),160,np.uint8)
            cv2.putText(image,'7 days',(10,27),cv2.FONT_HERSHEY_SIMPLEX,.65,(ink,ink,ink),1)
            image[9:13,130:134]=ink
            trimmed=self.layout.prepare_status(image)
            self.assertEqual(trimmed.shape[0],41)
            self.assertLess(trimmed.shape[1],150)
            self.assertEqual(np.count_nonzero(trimmed!=160),np.count_nonzero(image!=160))

    def scrollbar_frame(self, top=118, x=1804, pale=False):
        frame=np.full((1080,1920,3),60,np.uint8)
        if pale:
            frame[400:850,1770:1840]=210
        frame[top:top+54,x-2:x+3]=195
        return frame

    def test_pale_background_longer_than_thumb_does_not_hide_top(self):
        thumb=self.layout.scrollbar(self.scrollbar_frame(pale=True))
        self.assertIsNotNone(thumb)
        self.assertTrue(thumb.at_top)
        self.assertEqual((thumb.top,thumb.bottom),(118,172))

    def test_bright_top_background_cannot_confirm_middle_of_list(self):
        frame=self.scrollbar_frame(top=400)
        frame[115:250,1770:1840]=210
        thumb=self.layout.scrollbar(frame)
        self.assertEqual(thumb.top,400)
        self.assertFalse(thumb.at_top)

    def test_thumb_search_tracks_horizontal_offsets_and_resolution_scaling(self):
        import cv2
        for shift in (-6,0,6):
            for size in ((1920,1080),(2560,1440),(3840,2160)):
                with self.subTest(shift=shift,size=size):
                    frame=self.scrollbar_frame(x=1804+shift,pale=True)
                    scaled=friend_screen.normalize(cv2.resize(frame,size))
                    thumb=self.layout.scrollbar(scaled)
                    self.assertTrue(thumb.at_top)
                    self.assertLessEqual(abs(thumb.x-(1804+shift)),1)

    def test_thumb_tail_is_recovered_on_bright_background_without_losing_bottom(self):
        frame=self.scrollbar_frame(top=888)
        frame[913:]=190
        frame[913:943,1802:1807]=205
        thumb=self.layout.scrollbar(frame)
        self.assertIsNotNone(thumb)
        self.assertEqual((thumb.top,thumb.bottom),(888,943))
        self.assertTrue(thumb.at_bottom)

    def test_weak_background_streak_without_strong_thumb_seed_is_rejected(self):
        frame=np.full((1080,1920,3),190,dtype=np.uint8)
        frame[888:943,1802:1807]=205
        self.assertIsNone(self.layout.scrollbar(frame))

    def test_scroll_bound_uses_one_length_and_rejects_large_size_changes(self):
        before=self.layout.Scrollbar(831,886)
        after=self.layout.Scrollbar(863,914)
        expected=self.layout.scrollbar_displacement(before,after,149)
        self.assertAlmostEqual(expected,32*(149*124.8-846)/(827-53))
        self.assertLess(abs(expected-750),60)
        self.assertIsNone(self.layout.scrollbar_displacement(self.layout.Scrollbar(858,912),
                                                          self.layout.Scrollbar(888,914),149))
        self.assertIsNone(self.layout.scrollbar_displacement(None,after,149))

    def test_multiple_thumbs_or_arrow_only_are_not_a_scrollbar(self):
        frame=self.scrollbar_frame(top=300)
        frame[650:704,1802:1807]=195
        self.assertIsNone(self.layout.scrollbar(frame))
        frame[:]=60;frame[117:124,1802:1807]=195
        self.assertIsNone(self.layout.scrollbar(frame))

    def test_already_top_with_pale_background_needs_no_drag(self):
        frame=self.scrollbar_frame(pale=True)
        run=self.fast.FastScan(self.scan)
        with patch.object(run,'shot',return_value=((0,0,1920,1080),frame)), \
             patch.object(self.layout,'is_friend_list',return_value=True), \
             patch.object(self.scan,'_wait',return_value=True),patch.object(run,'drag') as drag:
            run.home()
        self.assertTrue(run.top_verified)
        self.assertEqual(len(run.home_observations),2)
        drag.assert_not_called()

    def test_home_waits_for_delayed_game_frames_without_another_drag(self):
        run=self.fast.FastScan(self.scan)
        frames=[self.scrollbar_frame(top=y,x=1810) for y in (600,300,118,118)]
        with patch.object(run,'shot',side_effect=[((0,0,1920,1080),f) for f in frames]), \
             patch.object(self.layout,'is_friend_list',return_value=True), \
             patch.object(self.scan,'_wait',return_value=True),patch.object(run,'drag') as drag:
            run.home()
        self.assertTrue(run.top_verified)
        drag.assert_called_once()
        self.assertEqual(drag.call_args.args[1],(1810,627.))

    def test_single_transient_top_frame_is_not_accepted(self):
        run=self.fast.FastScan(self.scan)
        frames=[self.scrollbar_frame(top=y) for y in (118,250,250,118,118)]
        with patch.object(run,'shot',side_effect=[((0,0,1920,1080),f) for f in frames]), \
             patch.object(self.layout,'is_friend_list',return_value=True), \
             patch.object(self.scan,'_wait',return_value=True),patch.object(run,'drag') as drag:
            run.home()
        self.assertTrue(run.top_verified)
        drag.assert_called_once()

    def test_home_failure_saves_all_observations_and_last_frame(self):
        run=self.fast.FastScan(self.scan);frame=self.scrollbar_frame(top=500)
        with patch.object(CONFIG,'save_scan_evidence',True), \
             patch.object(run,'shot',return_value=((0,0,1920,1080),frame)), \
             patch.object(self.layout,'is_friend_list',return_value=True), \
             patch.object(self.scan,'_wait',return_value=True),patch.object(run,'drag') as drag:
            with self.assertRaises(self.fast.ScanIncomplete):run.home()
        self.assertFalse(run.top_verified)
        self.assertEqual(drag.call_count,2)
        self.assertTrue((run.evidence/'top-unconfirmed.png').is_file())
        records=[json.loads(s) for s in (run.evidence/'home.jsonl').read_text(encoding='utf8').splitlines()]
        self.assertGreaterEqual(len(records),3)
        self.assertTrue(all(not r['at_top'] and (run.evidence/r['frame']).is_file() for r in records))

    def test_cancel_during_top_settle_never_marks_top_or_drags(self):
        run=self.fast.FastScan(self.scan);frame=self.scrollbar_frame()
        with patch.object(run,'shot',return_value=((0,0,1920,1080),frame)), \
             patch.object(self.layout,'is_friend_list',return_value=True), \
             patch.object(self.scan,'_wait',return_value=False),patch.object(run,'drag') as drag:
            with self.assertRaises(self.fast.ScanIncomplete):run.home()
        self.assertFalse(run.top_verified)
        drag.assert_not_called()

    def test_skipped_row_is_never_accepted_as_complete(self):
        self.simulation(move_rows=7)
        result=self.fast.FastScan(self.scan).run()
        self.assertFalse(result.complete)
        self.assertEqual(self.db.get_all_friends(),[])

    def test_unreadable_friend_does_not_publish_partial_roster(self):
        old=self.db.add_friend(None,'原有档案',60,None,'offline',T0)
        self.simulation(reject=137)
        result=self.fast.FastScan(self.scan).run()
        self.assertFalse(result.complete)
        self.assertEqual([r['id'] for r in self.db.get_all_friends()],[old])

    def test_overlap_punctuation_change_does_not_add_extra_friend(self):
        self.simulation(variant=True)
        result=self.fast.FastScan(self.scan).run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(len(self.db.get_all_friends()),148)

    def test_batch_fields_accept_latin_nickname_without_ocr_box_height_gate(self):
        band=friend_screen.CardBand(116,231)
        outputs=[('好友/我的好友',.99),('我的好友 148/200',.99),('SampleLatinName',.99),('Lv.60',.99),('今日内',.99)]
        with patch.object(self.layout,'bands',return_value=[band]), patch.object(ocr_engine,'recognize_lines',return_value=outputs):
            page=self.layout.read_page(np.zeros((1080,1920,3),dtype=np.uint8))
        self.assertEqual([(e.nickname,e.status) for e in page.entries],[('SampleLatinName','offline')])

    def test_unknown_status_never_defaults_to_offline(self):
        band=friend_screen.CardBand(116,231)
        outputs=[('好友/我的好友',.99),('我的好友 148/200',.99),('旅行者',.99),('Lv.60',.99),('无法辨认',.99)]
        with patch.object(self.layout,'bands',return_value=[band]), patch.object(ocr_engine,'recognize_lines',return_value=outputs):
            page=self.layout.read_page(np.zeros((1080,1920,3),dtype=np.uint8))
        self.assertEqual(page.entries,[])
        self.assertTrue(page.observations[0].issue)

    def test_snapshot_commit_rolls_back_status_profiles_and_roster_together(self):
        fid=self.db.add_friend(None,'旅行者',60,None,'offline',T0)
        rec={'id':fid,'entry':entry('旅行者'),'name_image':b'name','avatar_image':b'avatar'}
        with self.assertRaises(ValueError):
            self.db.apply_verified_scan([rec,rec],T1,.1,self.work.name)
        self.assertEqual(self.db.get_friend(fid)['current_status'],'offline')
        self.assertEqual(self.db.get_visual_profiles(),[])
        self.assertEqual(self.db._conn.execute('SELECT COUNT(*) FROM verified_scan').fetchone()[0],0)

    def test_visual_identity_reuses_existing_id_when_ocr_punctuation_changes(self):
        rng=np.random.default_rng(85)
        name=rng.integers(0,255,(49,552,3),dtype=np.uint8)
        avatar=rng.integers(0,255,(95,74,3),dtype=np.uint8)
        first={'entry':entry('示例昵称(><)'), 'name_image':self.fast._png(name),
               'avatar_image':self.fast._png(avatar)}
        fid=self.db.apply_verified_scan([first],T0,.1,self.work.name)[0]
        scan=self.fast.FastScan(self.scan)
        scan.expected=1
        scan.slots[0]=self.layout.CardObservation(friend_screen.CardBand(116,231),
                                                  entry('示例昵称(>.<)'),name,avatar)
        prepared=scan.prepare_records()
        self.assertEqual(prepared[0]['id'],fid)
        self.db.apply_verified_scan(prepared,T1,.1,self.work.name)
        self.assertEqual(len(self.db.get_all_friends()),1)

    def test_same_name_and_level_with_different_avatars_remain_separate(self):
        records=[{'entry':entry('旅行者'),'name_image':b'n','avatar_image':b'a'},
                 {'entry':entry('旅行者'),'name_image':b'n','avatar_image':b'b'}]
        ids=self.db.apply_verified_scan(records,T0,.1,self.work.name)
        self.assertEqual(len(set(ids)),2)
        self.assertEqual(len(self.db.get_display_friends()),2)

    def test_stopped_fast_scan_does_not_read_or_move_game(self):
        self.scan._stop.set()
        with patch.object(capture,'capture_region') as shot, patch.object(scroller,'drag_six_rows') as drag:
            result=self.fast.FastScan(self.scan).run()
        self.assertFalse(result.complete)
        shot.assert_not_called();drag.assert_not_called()

    def test_single_scan_reports_startup_exception_and_does_not_retry(self):
        results=[]
        self.scan.on_result=results.append
        self.assertIs(CONFIG, self.scan._run.__globals__['CONFIG'])
        with patch.object(CONFIG,'scan_once_only',True), \
             patch.object(CONFIG,'scan_frequency_minutes',None), \
             patch.object(self.scan,'_startup_countdown',return_value=True), \
             patch.object(self.scan,'scan_once',side_effect=RuntimeError('model failed')) as scan, \
             patch.object(self.scan._stop,'wait', side_effect=AssertionError('Single scan unexpectedly retried')) as wait:
            self.scan._run()
        scan.assert_called_once();wait.assert_not_called()
        self.assertEqual(len(results),1)
        self.assertFalse(results[0].complete)
        self.assertIn('model failed',results[0].message)

    def test_history_is_preserved_but_not_counted_in_verified_roster(self):
        self.db.add_friend(None,'旧OCR草稿',60,None,'offline',T0)
        self.db.apply_verified_scan([{'entry':entry('当前好友'),'name_image':b'n','avatar_image':b'a'}],T1,.2,self.work.name)
        self.assertEqual(len(self.db.get_all_friends()),2)
        self.assertEqual([r['nickname'] for r in self.db.get_display_friends()],['当前好友'])
        self.assertEqual(self.db.get_status_summary()['total'],1)

    def test_drag_releases_button_when_canceled_mid_motion(self):
        states=iter([False,False,False,True])
        with patch.object(game_input,'move_mouse') as move, patch.object(game_input,'mouse_event') as mouse, \
             patch.object(scroller.time,'sleep'), patch.object(CONFIG,'drag_duration_seconds',.1):
            result=scroller.drag_client((0,0,1920,1080),(800,925),(800,176),should_stop=lambda:next(states,True))
        self.assertFalse(result)
        self.assertEqual([c.args[0] for c in mouse.call_args_list],[2,4])
        self.assertTrue(all(0<=c.args[0]<1920 and 0<=c.args[1]<1080 for c in move.call_args_list))


class NicknameRegressions(unittest.TestCase):
    def setUp(self):
        from app import nickname_ocr
        self.names = nickname_ocr
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.work = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.work.cleanup)
        self.stack.enter_context(patch.object(CONFIG, 'db_path', str(Path(self.work.name)/'test.db')))
        self.stack.enter_context(patch.object(CONFIG, 'nickname_font_path', ''))

    def image(self, text='ABC'):
        from PIL import Image, ImageDraw, ImageFont
        image = Image.new('RGB', (280, 60), (225, 225, 225))
        ImageDraw.Draw(image).text((12, 8), text, font=ImageFont.load_default(size=32), fill=(55, 55, 65))
        return np.array(image)

    def test_padded_crop_keeps_detached_accents_and_punctuation(self):
        image = self.image('^ABC%')
        original = self.names.ink_mask(image)
        prepared = self.names.ink_mask(self.names.prepare(image))
        self.assertGreater(np.count_nonzero(prepared), 100)
        self.assertEqual(np.count_nonzero(original), np.count_nonzero(prepared))

    def test_high_confidence_partial_retry_cannot_replace_whole_nickname(self):
        matcher = MagicMock()
        matcher.recognize.return_value = None
        matcher.line_score.return_value = 0.
        with patch.object(self.names, 'get_matcher', return_value=matcher), \
             patch.object(ocr_engine, 'recognize_lines', return_value=[('^', .999), ('^', .999)]):
            outputs, audit = self.names.refine([self.image('^ABC^')], [('', 0.)])
        self.assertEqual(outputs, [('', 0.)])
        self.assertEqual(audit[0]['method'], 'rejected')

    def test_local_correction_requires_the_whole_reviewed_shape(self):
        image = self.image('^ABC^')
        self.names.add_correction(image, '^ABC^', 'test screenshot', 'test-only review')
        match = self.names.reviewed_name(self.names.ink_mask(image))
        self.assertEqual(match[0], '^ABC^')
        self.assertLessEqual(match[1], 1.)
        self.assertIsNone(self.names.reviewed_name(self.names.ink_mask(self.image('^XYZ^'))))

    def test_small_punctuation_needs_both_ocr_agreement_and_full_geometry(self):
        matcher = MagicMock()
        matcher.recognize.return_value = None
        matcher.line_score.return_value = .875
        with patch.object(self.names, 'get_matcher', return_value=matcher), \
             patch.object(ocr_engine, 'recognize_lines', return_value=[('Oo。。样例', .87), ('Oo。。样例', .99)]):
            outputs, _ = self.names.refine([self.image('Oo..ABC')], [('00。。样例', .89)])
        self.assertEqual(outputs[0][0], 'Oo。。样例')
        with patch.object(self.names, 'get_matcher', return_value=matcher), \
             patch.object(ocr_engine, 'recognize_lines', return_value=[('00。。样例', .87), ('Oo。。样例', .99)]):
            outputs, _ = self.names.refine([self.image('Oo..ABC')], [('00。。样例', .89)])
        self.assertEqual(outputs[0], ('',0.))

    def test_bracket_width_needs_agreement_and_a_distinct_whole_font_match(self):
        matcher=MagicMock()
        matcher.line_score.side_effect=lambda mask,text: .89 if text=='name（A）' else .81
        candidates=[('name(A）',.92),('name(A)',.94)]
        self.assertEqual(self.names.punctuation_candidate(matcher,None,candidates),('name（A）',.89))
        matcher.line_score.return_value=.90
        matcher.line_score.side_effect=None
        self.assertIsNone(self.names.punctuation_candidate(matcher,None,candidates))

    def test_strong_bracket_read_can_corroborate_a_weaker_agreeing_read(self):
        matcher=MagicMock()
        matcher.line_score.side_effect=lambda mask,text: .94 if text=='name（A）' else .75
        candidates=[('name(A）',.88),('name(A)',.956)]
        self.assertEqual(self.names.punctuation_candidate(matcher,None,candidates),('name（A）',.88))

    def test_weak_bracket_reads_do_not_pass_without_strong_corroboration(self):
        matcher=MagicMock()
        # High scores shared by every width variant are ambiguous evidence.
        matcher.line_score.return_value=.99
        self.assertIsNone(self.names.punctuation_candidate(matcher,None,
            [('name(A)',.88),('name(A)',.94)]))
        matcher.line_score.reset_mock()
        self.assertIsNone(self.names.punctuation_candidate(matcher,None,
            [('name(A)',.84),('name(A)',.99)]))
        matcher.line_score.assert_not_called()

    def test_strong_whole_glyph_can_resolve_weaker_ocr_bracket_reads(self):
        matcher=MagicMock()
        matcher.line_score.side_effect=lambda mask,text: .95 if text=='name（A）' else .78
        evidence={}
        result=self.names.punctuation_candidate(matcher,None,
            [('name（A）',.88788),('name(A)',.94372)],audit=evidence)
        self.assertEqual(result,('name（A）',.88788))
        self.assertEqual(evidence['rule'],'strong_whole_glyph')
        self.assertGreaterEqual(evidence['margin'],.06)
        self.assertEqual(evidence['ranked_variants'][0]['text'],'name（A）')

    def test_dot_form_is_selected_by_image_not_silently_normalized(self):
        matcher=MagicMock()
        candidates=[('name。',.85771),('name.',.87245)]
        for winner in ('name.','name。','name．'):
            matcher.line_score.side_effect=lambda mask,text: .95 if text==winner else .82
            self.assertEqual(self.names.punctuation_candidate(matcher,None,candidates),
                             (winner,.85771))

    def test_weaker_ocr_requires_stronger_glyph_score_and_margin(self):
        matcher=MagicMock()
        for best,second in ((.929,.70),(.95,.90)):
            matcher.line_score.side_effect=lambda mask,text: best if text=='name.' else second
            self.assertIsNone(self.names.punctuation_candidate(matcher,None,
                [('name。',.86),('name.',.87)]))

    def test_punctuation_cannot_fill_missing_marks_or_change_digits(self):
        matcher=MagicMock()
        matcher.line_score.return_value=.99
        for reads in ([('name.',.94),('name',.93)],
                      [('name12.',.94),('name21。',.93)]):
            self.assertIsNone(self.names.punctuation_candidate(matcher,None,reads))
        matcher.line_score.assert_not_called()

    def test_rejected_name_records_candidate_geometry_and_punctuation_reason(self):
        matcher=MagicMock()
        matcher.recognize.return_value=None
        matcher.line_score.return_value=.2
        with patch.object(self.names,'get_matcher',return_value=matcher), \
             patch.object(ocr_engine,'recognize_lines',return_value=[('A.',.88),('A。',.87)]):
            outputs,audit=self.names.refine([self.image('A.')],[('A.',.86)])
        self.assertEqual(outputs,[('',0.)])
        self.assertFalse(audit[0]['punctuation_check']['accepted'])
        self.assertIn('差距',audit[0]['punctuation_check']['reason'])
        self.assertEqual({c['text'] for c in audit[0]['font_candidates']},{'A.','A。'})

    def test_three_identical_reads_use_original_to_corroborate_a_weak_retry(self):
        matcher=MagicMock()
        matcher.line_score.side_effect=lambda mask,text: .952 if text=='name.' else .85
        audit={}
        result=self.names.punctuation_candidate(matcher,None,
            [('name.',.85511),('name.',.84581)],original=('name.',.87052),audit=audit)
        self.assertEqual(result,('name.',.85511))
        self.assertEqual(audit['rule'],'three_reads_and_whole_glyph')
        self.assertEqual(audit['three_read_consensus']['supporting_reads'],2)

    def test_original_vote_cannot_override_conflicting_or_insufficient_reads(self):
        matcher=MagicMock()
        matcher.line_score.return_value=.99
        for original,reads in [
                (('other.',.99),[('name.',.855),('name.',.845)]),
                (('name.',.99),[('name.',.84),('name.',.83)]),
                (('name.',.87),[('name.',.86),('name。',.84)])]:
            self.assertIsNone(self.names.punctuation_candidate(matcher,None,reads,original=original))
        matcher.line_score.assert_not_called()

    def test_three_read_consensus_still_requires_strong_complete_geometry(self):
        matcher=MagicMock()
        matcher.line_score.side_effect=lambda mask,text: .92 if text=='name.' else .70
        self.assertIsNone(self.names.punctuation_candidate(matcher,None,
            [('name.',.99),('name.',.84)],original=('name.',.99)))

    def contextual_glyphs(self, upper=.969548, lower=.811614):
        matcher=self.names.FontMatcher.__new__(self.names.FontMatcher)
        matcher.line_score=MagicMock(side_effect=lambda mask,text: upper if text=='♥测Ψ' else lower)
        runs=[(0,17),(18,48),(50,75)]
        certain={(0,1):('♥',.9723),(1,2):('测',.9463)}
        ambiguous={(2,3):[('Ψ',.9514),('ψ',.9204)]}
        return matcher,runs,certain,ambiguous

    def test_whole_line_baseline_resolves_ambiguous_greek_case(self):
        matcher,runs,certain,ambiguous=self.contextual_glyphs()
        audit={}
        result=matcher._resolve_ambiguous(None,runs,certain,ambiguous,audit=audit)
        self.assertEqual(result[0],'♥测Ψ')
        self.assertEqual(result[1],[.9723,.9463,.9514])
        self.assertEqual({r['text'] for r in audit['ranked_candidates']},{'♥测Ψ','♥测ψ'})
        self.assertTrue(audit['accepted'])

    def test_contextual_glyphs_reject_a_close_whole_line_tie(self):
        matcher,runs,certain,ambiguous=self.contextual_glyphs(upper=.96,lower=.93)
        self.assertIsNone(matcher._resolve_ambiguous(None,runs,certain,ambiguous))

    def test_contextual_glyphs_require_strong_absolute_geometry(self):
        matcher,runs,certain,ambiguous=self.contextual_glyphs(upper=.92,lower=.70)
        self.assertIsNone(matcher._resolve_ambiguous(None,runs,certain,ambiguous))

    def test_contextual_glyphs_cannot_skip_an_unmatched_component(self):
        matcher,runs,certain,ambiguous=self.contextual_glyphs()
        del certain[1,2]
        self.assertIsNone(matcher._resolve_ambiguous(None,runs,certain,ambiguous))
        matcher.line_score.assert_not_called()

    def test_contextual_search_does_not_prune_rivals_then_claim_a_winner(self):
        matcher=self.names.FontMatcher.__new__(self.names.FontMatcher)
        matcher.line_score=MagicMock(return_value=.99)
        runs=[(i*20,i*20+18) for i in range(7)]
        ambiguous={(i,i+1):[('A',.95),('B',.93)] for i in range(7)}
        audit={}
        self.assertIsNone(matcher._resolve_ambiguous(None,runs,{},ambiguous,audit=audit))
        matcher.line_score.assert_not_called()
        self.assertIn('候选过多',audit['reason'])

    def touching_glyphs(self):
        import cv2
        mask=cv2.imdecode(np.frombuffer((private_fixture('touching-nickname-mask.png')).read_bytes(),np.uint8),cv2.IMREAD_GRAYSCALE)
        matcher=self.names.FontMatcher.__new__(self.names.FontMatcher)
        # Independently labelled glyphs supply an ideal cache so this test
        # isolates segmentation; it does not depend on an installed game font.
        glyphs=[mask[:,:17],mask[:,17:47],mask[:,50:74]]
        features=[self.names._feature(glyph) for glyph in glyphs]
        matcher.codes=np.array([ord(c) for c in '♥测Ψ'])
        matcher.vectors=np.array([value[0] for value in features])
        matcher.sizes=np.array([value[1]*1.5 for value in features])
        rendered=cv2.resize(mask,None,fx=1.5,fy=1.5,interpolation=cv2.INTER_LINEAR)
        matcher.render=lambda text: rendered if text=='♥测Ψ' else np.zeros((0,0),np.uint8)
        return matcher,mask

    def test_touching_symbol_and_ideograph_keep_all_glyphs(self):
        matcher,mask=self.touching_glyphs()
        result=matcher.recognize(mask)
        self.assertIsNotNone(result)
        self.assertEqual(result[0],'♥测Ψ')

    def test_native_1080p_bridge_recovers_complete_nickname_at_strong_glyph_score(self):
        import cv2
        matcher,_=self.touching_glyphs()
        mask=cv2.imdecode(np.frombuffer((private_fixture('native-1080p-touching-nickname-mask.png')).read_bytes(),np.uint8),cv2.IMREAD_GRAYSCALE)
        result=matcher.recognize(mask)
        self.assertIsNotNone(result)
        self.assertEqual(result[0],'♥测Ψ')
        # The old split contaminated the ideograph and scored only .909 against
        # this independent fixture (.887 against the installed game font).
        self.assertGreaterEqual(result[1],.93)
        extended=np.pad(mask,((0,0),(0,10)))
        extended[0:3,-4:-1]=255
        self.assertIsNone(matcher.recognize(extended))

    def test_touching_glyph_recovery_cannot_discard_an_unmatched_mark(self):
        matcher,mask=self.touching_glyphs()
        extended=np.pad(mask,((0,0),(0,10)))
        extended[0:3,-4:-1]=255
        self.assertIsNone(matcher.recognize(extended))

    def test_bracket_rule_cannot_change_letters_or_explain_partial_ink(self):
        matcher=MagicMock()
        matcher.line_score.return_value=.99
        self.assertIsNone(self.names.punctuation_candidate(matcher,None,[('name(A)',.98),('name(B)',.99)]))
        matcher.line_score.return_value=0.
        self.assertIsNone(self.names.punctuation_candidate(matcher,None,[('(A)',.99),('(A)',.99)]))
        matcher.line_score.assert_called()

    def test_batch_padding_is_restored_after_inference_error(self):
        recognizer=MagicMock()
        recognizer.rec_image_shape=[3,48,320]
        observed=[]
        def fail(*args):
            observed.append(list(recognizer.rec_image_shape))
            raise RuntimeError('inference failed')
        recognizer.side_effect=fail
        with patch.object(ocr_engine,'get_engine',return_value=SimpleNamespace(text_rec=recognizer)):
            with self.assertRaisesRegex(RuntimeError,'inference failed'):
                ocr_engine.recognize_lines([self.image()])
        self.assertEqual(observed,[[3,48,160]])
        self.assertEqual(recognizer.rec_image_shape,[3,48,320])

    def test_reviewed_shape_survives_card_background_brightness(self):
        image = self.image('^ABC^')
        self.names.add_correction(image, '^ABC^', 'test screenshot', 'test-only review')
        darker = np.clip(image.astype(int)-25, 0, 255).astype(np.uint8)
        self.assertEqual(self.names.reviewed_name(self.names.ink_mask(darker))[0], '^ABC^')

    def test_conflicting_local_labels_are_not_silently_selected(self):
        image = self.image('ABC')
        record = self.names.add_correction(image, 'ABC', 'test screenshot', 'test-only review')
        directory = Path(CONFIG.db_path).parent/'nickname-corrections'
        record['text'] = 'different'
        (directory/'conflict.json').write_text(json.dumps(record), encoding='utf-8')
        self.assertIsNone(self.names.reviewed_name(self.names.ink_mask(image)))

    def test_font_geometry_rejects_a_lone_accent_for_a_long_name(self):
        from PIL import ImageFont
        matcher = self.names.FontMatcher.__new__(self.names.FontMatcher)
        matcher.font = ImageFont.load_default(size=48)
        self.assertEqual(matcher.line_score(self.names.ink_mask(self.image('^ABC^')), '^'), 0.)

    def test_blank_slot_does_not_get_a_placeholder_or_call_ocr(self):
        with patch.object(self.names, 'get_matcher', return_value=None), \
             patch.object(ocr_engine, 'recognize_lines') as ocr:
            outputs, audit = self.names.refine([np.full((60,280,3),220,np.uint8)], [('',0.)])
        self.assertEqual(outputs, [('',0.)])
        self.assertEqual(audit[0]['method'], 'rejected')
        ocr.assert_not_called()

    def test_rejected_nickname_is_not_replaced_by_detector_partial_text(self):
        from app import fast_scan, fast_layout
        observation = fast_layout.CardObservation(friend_screen.CardBand(116,231), None,
            self.image(), self.image(), name_audit={'method':'rejected','reason':'partial name'})
        page = friend_screen.FriendPage(observations=[observation])
        run = fast_scan.FastScan.__new__(fast_scan.FastScan)
        run.interface_mode = 'normal'
        run.check = MagicMock()
        with patch.object(ocr_engine, 'recognize') as ocr:
            run.recover_fields(np.zeros((1080,1920,3),np.uint8),page)
        self.assertIsNone(observation.entry)
        ocr.assert_not_called()


class KeyboardInputRegressions(unittest.TestCase):
    def setUp(self):
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.api=MagicMock()
        self.api.MapVirtualKeyW.return_value=0x18
        self.api.GetAsyncKeyState.return_value=0
        self.events=[]
        self.responses=[]
        def send(count,pointer,size):
            import ctypes
            event=ctypes.cast(pointer,ctypes.POINTER(game_input.INPUT)).contents
            self.events.append((count,size,event.type,event.ki.wVk,event.ki.wScan,event.ki.dwFlags))
            return self.responses.pop(0) if self.responses else 1
        self.api.SendInput.side_effect=send
        self.stack.enter_context(patch.object(game_input,'_user32',return_value=self.api))
        self.sleep=self.stack.enter_context(patch.object(game_input.time,'sleep'))

    def test_keyboard_input_has_correct_native_layout_and_scancodes(self):
        import ctypes
        expected_size=40 if ctypes.sizeof(ctypes.c_void_p)==8 else 28
        self.assertEqual(ctypes.sizeof(game_input.INPUT),expected_size)
        self.assertTrue(game_input.press_key(0x4F))
        self.assertEqual(self.events,[(1,expected_size,1,0,0x18,0x08),(1,expected_size,1,0,0x18,0x0A)])
        self.api.MapVirtualKeyW.assert_called_once_with(0x4F,4)
        self.assertGreaterEqual(self.sleep.call_args_list[0].args[0],.04)

    def test_esc_uses_same_checked_input_path(self):
        self.api.MapVirtualKeyW.return_value=1
        self.assertTrue(game_input.press_esc())
        self.api.MapVirtualKeyW.assert_called_once_with(0x1B,4)
        self.assertEqual([event[4:] for event in self.events],[(1,8),(1,10)])

    def test_extended_scan_code_preserves_extended_flag(self):
        self.api.MapVirtualKeyW.return_value=0xE04D
        game_input.press_key(0x27)
        self.assertEqual([event[4:] for event in self.events],[(0x4D,9),(0x4D,11)])

    def test_zero_sendinput_is_failure_even_with_zero_last_error(self):
        self.responses=[0]
        with self.assertRaises(game_input.InputError) as caught:
            game_input.press_key(0x4F)
        self.assertIn('SendInput=0',str(caught.exception))
        self.assertIn('错误码=0',str(caught.exception))
        self.assertEqual(len(self.events),1)
        self.sleep.assert_not_called()

    def test_key_is_released_when_hold_is_interrupted(self):
        self.sleep.side_effect=RuntimeError('hold interrupted')
        with self.assertRaisesRegex(RuntimeError,'hold interrupted'):
            game_input.press_key(0x4F)
        self.assertEqual([event[-1] for event in self.events],[8,10])

    def test_failed_key_up_is_retried_once(self):
        self.responses=[1,0,1]
        self.assertTrue(game_input.press_key(0x4F))
        self.assertEqual([event[-1] for event in self.events],[8,10,10])

    def test_repeated_key_up_failure_is_reported(self):
        self.responses=[1,0,0]
        with self.assertRaisesRegex(game_input.InputError,'抬起失败'):
            game_input.press_key(0x4F)
        self.assertEqual(len(self.events),3)

    def test_held_modifier_does_not_turn_o_into_another_shortcut(self):
        self.api.GetAsyncKeyState.side_effect=lambda key: 0x8000 if key==0x11 else 0
        with self.assertRaisesRegex(game_input.InputError,'松开'):
            game_input.press_key(0x4F)
        self.assertEqual(self.events,[])

    def test_unmapped_key_is_not_injected(self):
        self.api.MapVirtualKeyW.return_value=0
        with self.assertRaisesRegex(game_input.InputError,'扫描码'):
            game_input.press_key(0x4F)
        self.assertEqual(self.events,[])

    def test_target_permissions_are_checked_before_injection(self):
        with patch.object(window,'input_permission_error',return_value='权限不足'), \
             patch.object(window,'is_foreground',return_value=True):
            with self.assertRaisesRegex(game_input.InputError,'权限不足'):
                game_input.press_key(0x4F,hwnd=123)
        self.assertEqual(self.events,[])

    def test_target_focus_is_checked_before_injection(self):
        with patch.object(window,'input_permission_error',return_value=None), \
             patch.object(window,'is_foreground',return_value=False):
            with self.assertRaisesRegex(game_input.InputError,'前台'):
                game_input.press_key(0x4F,hwnd=123)
        self.assertEqual(self.events,[])

    def test_integrity_mismatch_is_distinct_from_equal_or_unknown(self):
        with patch('win32process.GetWindowThreadProcessId',return_value=(1,123)):
            for levels,blocked in [([0x2000,0x3000],True),([0x3000,0x3000],False),
                                   ([0x3000,0x2000],False),([None,0x3000],False)]:
                with self.subTest(levels=levels),patch.object(window,'process_integrity_level',side_effect=levels):
                    error=window.input_permission_error(123)
                    self.assertEqual(bool(error),blocked)
                    if blocked:self.assertIn('start-admin.bat',error)

    def test_integrity_query_closes_handles_on_failure(self):
        import win32security
        process=MagicMock();token=MagicMock()
        with patch('win32api.OpenProcess',return_value=process), \
             patch.object(win32security,'OpenProcessToken',return_value=token), \
             patch.object(win32security,'GetTokenInformation',side_effect=OSError('query failed')):
            self.assertIsNone(window.process_integrity_level(123))
        token.Close.assert_called_once();process.Close.assert_called_once()

    def test_already_foreground_window_is_not_reactivated(self):
        with patch.object(window,'is_foreground',return_value=True), \
             patch('win32gui.SetForegroundWindow') as activate,patch('win32gui.ShowWindow') as show:
            self.assertTrue(window.bring_to_front(123))
        activate.assert_not_called();show.assert_not_called()

    def test_failed_activation_is_reported_without_throwing(self):
        with patch.object(window,'is_foreground',return_value=False), \
             patch('win32gui.SetForegroundWindow',side_effect=OSError('denied')), \
             patch('win32gui.ShowWindow'),patch('win32gui.IsIconic',return_value=False):
            self.assertFalse(window.bring_to_front(123))

    def test_admin_launcher_requests_uac_for_project_only(self):
        import admin_start
        import subprocess
        directory=WORK/'admin launch space'
        (directory/'.venv/Scripts').mkdir(parents=True,exist_ok=True)
        (directory/'.venv/Scripts/pythonw.exe').touch()
        (directory/'main.py').touch()
        with patch.object(admin_start,'_run_as',return_value=33) as launch:
            self.assertEqual(admin_start.launch_elevated(directory),0)
        launch.assert_called_once_with(str(directory/'.venv/Scripts/pythonw.exe'),
                                       subprocess.list2cmdline([str(directory/'main.py')]),str(directory))

    def test_uac_launch_uses_runas_and_shows_the_gui(self):
        import admin_start
        shell=MagicMock();shell.ShellExecuteW.return_value=33
        with patch.object(admin_start.ctypes,'WinDLL',return_value=shell):
            self.assertEqual(admin_start._run_as('pythonw.exe','main.py','workspace'),33)
        shell.ShellExecuteW.assert_called_once_with(None,'runas','pythonw.exe','main.py','workspace',1)

    def test_admin_launcher_reports_cancel_without_fallback(self):
        import admin_start
        with patch.object(admin_start,'_run_as',return_value=5) as launch:
            self.assertEqual(admin_start.launch_elevated(ROOT),1)
        launch.assert_called_once()

    def test_admin_verification_launch_passes_only_single_scan_switch(self):
        import admin_start
        import subprocess
        with patch.object(admin_start,'_run_as',return_value=33) as launch:
            self.assertEqual(admin_start.launch_elevated(ROOT,scan_once=True),0)
        self.assertEqual(launch.call_args.args[1],subprocess.list2cmdline([str(ROOT/'main.py'),'--scan-once']))

    def test_admin_launcher_refuses_missing_install(self):
        import admin_start
        with patch.object(admin_start,'_run_as') as launch:
            self.assertEqual(admin_start.launch_elevated(WORK/'missing_install'),1)
        launch.assert_not_called()


class WorkflowRegressions(unittest.TestCase):
    def setUp(self):
        from app import scan_workflow as flow
        self.flow = flow
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.work = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.work.cleanup)
        self.stack.enter_context(patch.object(CONFIG, 'log_dir', self.work.name))
        self.stack.enter_context(patch.object(CONFIG, 'save_scan_evidence', False))
        self.stack.enter_context(patch.object(CONFIG, 'roster_restart_attempts', 3))
        self.stack.enter_context(patch.object(game_input, 'move_client', return_value=True))
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        self.scan = Scanner(self.db)
        self.scan._check_pause = lambda: not self.scan._stop.is_set()
        self.scan._say = MagicMock()
        self.scan._ensure_friend_list = MagicMock(return_value=True)
        self.scan.set_manual_rect((0,0,1920,1080))
        self.stack.enter_context(patch.object(flow.layout, 'read_header', side_effect=lambda image: (True, int(image[0]))))

    def items(self, specs):
        result = []
        for i, spec in enumerate(specs):
            name, uid, seed = spec
            rng = np.random.default_rng(seed)
            pixels = rng.integers(0,255,(40,120,3),dtype=np.uint8)
            result.append(self.flow.layout.CardObservation(friend_screen.CardBand(116+i*125,231+i*125),
                          entry(name), pixels, pixels.copy()))
        return result

    def seed(self, specs, stamp=T0):
        records = self.flow.records_from_observations(self.items(specs))
        for r, (_, uid, _) in zip(records, specs):
            r['uid'] = uid
        return self.db.apply_verified_scan(records, stamp, 1., self.work.name, identity_complete=True)

    def workflow(self, scripts, *, force=False):
        flow = self.flow.ScanWorkflow(self.scan, force_uid=force)
        calls = []
        queue = iter(scripts)
        def collect(uid=False, expected=None):
            calls.append('uid' if uid else 'quick')
            spec = next(queue)
            if isinstance(spec, Exception):
                raise spec
            folder = Path(self.work.name)/str(len(calls));folder.mkdir(exist_ok=True)
            result = SimpleNamespace(expected=len(spec), slots=dict(enumerate(self.items(spec))),
                                     evidence=folder, check=MagicMock(),
                                     shot=lambda: (None,[len(spec)]),
                                     uids={i:row[1] for i,row in enumerate(spec)})
            flow.last = result
            flow.passes.append(str(folder))
            if uid: flow.uid_seconds += 2
            else: flow.quick_seconds += 1
            return result
        flow.collect = collect
        return flow, calls

    def test_first_scan_collects_uids_and_skips_comparison_without_complete_baseline(self):
        self.db.add_friend(None, '旧草稿', 60, None, 'offline', T0)
        specs=[('甲','111111111',1),('乙','222222222',2)]
        flow,calls=self.workflow([specs,specs])
        result=flow.run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(calls,['quick','uid'])
        self.assertEqual(result.comparison['basis'],'skipped')
        self.assertEqual(result.uid_seconds,2)
        self.assertEqual([r['uid'] for r in self.db.get_display_friends()],['111111111','222222222'])
        self.assertIsNotNone(self.db.find_by_nickname('旧草稿'))

    def test_reordering_reuses_uids_without_opening_profiles_or_changing_ids(self):
        before=[('甲','111111111',1),('乙','222222222',2)]
        ids=self.seed(before)
        flow,calls=self.workflow([list(reversed(before))])
        result=flow.run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(calls,['quick'])
        self.assertEqual([r['id'] for r in self.db.get_display_friends()],list(reversed(ids)))
        self.assertEqual(result.comparison['added'],[])
        self.assertEqual(result.comparison['removed'],[])
        self.assertEqual(result.comparison['renamed'],[])

    def test_renaming_rechecks_quick_then_uid_and_preserves_history(self):
        old=[('旧名','111111111',1),('另一位','222222222',2)]
        ids=self.seed(old)
        self.db.update_status(ids[0],'online','旧名',60,T1)
        before=self.db.get_friend(ids[0])['cumulative_online_seconds']
        new=[('另一位','222222222',2),('新名','111111111',1)]
        flow,calls=self.workflow([new,new,new])
        with patch.object(self.flow,'now_str',return_value=T2):
            result=flow.run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(calls,['quick','quick','uid'])
        renamed=self.db.find_by_uid('111111111')
        self.assertEqual((renamed['id'],renamed['nickname']),(ids[0],'新名'))
        self.assertGreaterEqual(renamed['cumulative_online_seconds'],before)
        self.assertEqual(len(self.db.get_all_friends()),2)
        self.assertEqual(result.comparison['renamed'],[{'uid':'111111111','before':'旧名','after':'新名'}])
        self.assertEqual(self.db.get_roster_comparisons()[0],result.comparison)

    def test_count_change_restarts_and_compares_to_original_complete_roster(self):
        old=[('甲','111111111',1),('乙','222222222',2)]
        self.seed(old)
        new=[old[0]]
        flow,calls=self.workflow([self.flow.RosterChanged('人数变化','count_changed'),new,new,new])
        result=flow.run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(result.restarts,1)
        self.assertEqual(calls,['quick','quick','quick','uid'])
        self.assertEqual(result.comparison['removed'],[{'uid':'222222222','nickname':'乙'}])
        self.assertEqual(self.db.find_by_uid('222222222')['current_status'],'offline')
        removed=[e for e in self.db.get_recent_events() if e['event_type']=='removed']
        self.assertEqual([e['uid'] for e in removed],['222222222'])
        self.scan._ensure_friend_list.assert_called_once()

    def test_mid_scan_reordering_restarts_quick_pass_without_rereading_unchanged_uids(self):
        specs=[('甲','111111111',1),('乙','222222222',2)]
        self.seed(specs)
        flow,calls=self.workflow([self.flow.RosterChanged('顺序改变'),list(reversed(specs))])
        result=flow.run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(calls,['quick','quick'])
        self.assertEqual(result.uid_seconds,0)
        self.assertEqual(result.restarts,1)

    def test_repeated_count_changes_are_bounded_and_preserve_previous_baseline(self):
        old=[('甲','111111111',1)]
        self.seed(old); baseline=self.db.get_complete_roster()
        flow,calls=self.workflow([self.flow.RosterChanged('人数变化','count_changed') for _ in range(4)])
        result=flow.run()
        self.assertFalse(result.complete)
        self.assertEqual(result.restarts,3)
        self.assertEqual(len(calls),4)
        self.assertEqual(self.db.get_complete_roster(),baseline)
        self.assertEqual(self.db.get_roster_comparisons(),[])

    def test_uid_failure_after_new_names_does_not_publish_partial_roster(self):
        old=[('甲','111111111',1)];new=[('新名','111111111',1)]
        self.seed(old); baseline=self.db.get_complete_roster()
        flow,calls=self.workflow([new,new,self.flow.ScanIncomplete('UID 未确认')])
        result=flow.run()
        self.assertFalse(result.complete)
        self.assertEqual(self.db.get_complete_roster(),baseline)
        self.assertEqual(self.db.find_by_uid('111111111')['nickname'],'甲')
        self.assertEqual(self.db.get_roster_comparisons(),[])

    def test_duplicate_uids_never_replace_a_valid_roster(self):
        old=[('甲','111111111',1)]; new=[('甲','111111111',1),('乙','111111111',2)]
        self.seed(old); baseline=self.db.get_complete_roster()
        flow,calls=self.workflow([new,new,new])
        self.assertFalse(flow.run().complete)
        self.assertEqual(self.db.get_complete_roster(),baseline)

    def test_same_name_different_uid_are_distinct_and_can_be_reordered_visually(self):
        specs=[('同名','111111111',1),('同名','222222222',2)]
        self.seed(specs)
        flow,calls=self.workflow([list(reversed(specs))])
        result=flow.run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(calls,['quick'])
        self.assertEqual([r['uid'] for r in self.db.get_display_friends()],['222222222','111111111'])
        self.assertEqual(len(self.db.get_all_friends()),2)

    def test_indistinguishable_same_names_require_uid_evidence_again(self):
        specs=[('同名','111111111',1),('同名','222222222',1)]
        self.seed(specs)
        flow,calls=self.workflow([specs,specs])
        self.assertTrue(flow.run().complete)
        self.assertEqual(calls,['quick','uid'])
        self.assertEqual(len(self.db.get_display_friends()),2)

    def test_uid_pass_can_have_a_different_order_than_the_quick_pass(self):
        a=('甲','111111111',1);b=('乙','222222222',2)
        flow,calls=self.workflow([[a,b],[b,a]])
        result=flow.run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual([(r['nickname'],r['uid']) for r in result.friends],
                         [('乙','222222222'),('甲','111111111')])

    def test_exchanging_nicknames_cannot_exchange_persistent_uids(self):
        self.seed([('甲','111111111',1),('乙','222222222',2)])
        new=[('甲','222222222',2),('乙','111111111',1)]
        flow,calls=self.workflow([new,new])
        result=flow.run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(calls,['quick','uid'])
        self.assertEqual(self.db.find_by_uid('111111111')['nickname'],'乙')
        self.assertEqual(len(result.comparison['renamed']),2)

    def test_atomic_commit_rolls_back_uid_rename_and_baseline_on_failure(self):
        specs=[('原名','111111111',1)]
        self.seed(specs); baseline=self.db.get_complete_roster()
        records=self.flow.records_from_observations(self.items([('新名','111111111',1),('新增','222222222',2)]))
        for record,uid in zip(records,['111111111','222222222']):record['uid']=uid
        records[-1]['name_image']=None
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.apply_verified_scan(records,T1,1.,self.work.name,identity_complete=True,comparison={'basis':'uid'})
        self.assertEqual(self.db.find_by_uid('111111111')['nickname'],'原名')
        self.assertIsNone(self.db.find_by_uid('222222222'))
        self.assertEqual(self.db.get_complete_roster(),baseline)
        self.assertEqual(self.db.get_roster_comparisons(),[])

    def test_legacy_complete_roster_without_uids_compares_local_ids_without_guessing_renames(self):
        records=self.flow.records_from_observations(self.items([('旧名',None,1)]))
        self.db.apply_verified_scan(records,T0,1.,self.work.name)
        specs=[('新名','111111111',1)]
        flow,calls=self.workflow([specs,specs,specs])
        result=flow.run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(result.comparison['basis'],'local_id')
        self.assertEqual(result.comparison['renamed'],[])
        self.assertEqual(result.comparison['added'],[])
        self.assertEqual(result.comparison['removed'],[])

    def test_empty_verified_roster_is_a_real_baseline(self):
        self.db.apply_verified_scan([],T0,1.,self.work.name,identity_complete=True)
        flow,calls=self.workflow([[]])
        result=flow.run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(calls,['quick'])
        self.assertEqual(result.comparison['basis'],'uid')

    def test_uid_return_sends_only_one_esc_when_list_is_restored(self):
        scan=self.flow.UidPass(self.scan,1)
        frame=np.random.default_rng(1).integers(0,255,(1080,1920,3),dtype=np.uint8)
        with patch.object(scan,'check'),patch.object(scan,'wait'),patch.object(scan,'shot',return_value=(None,frame)), \
             patch.object(self.flow.layout,'read_header',return_value=(True,1)), \
             patch.object(self.flow.layout,'bands',return_value=[friend_screen.CardBand(116,231)]), \
             patch.object(game_input,'press_esc',return_value=True) as keys:
            scan.restore_list(frame)
        keys.assert_called_once_with(hwnd=None)

    def test_returning_to_reordered_list_restarts_instead_of_pressing_esc_again(self):
        scan=self.flow.UidPass(self.scan,1)
        before=np.random.default_rng(1).integers(0,255,(1080,1920,3),dtype=np.uint8)
        after=np.random.default_rng(2).integers(0,255,(1080,1920,3),dtype=np.uint8)
        with patch.object(scan,'check'),patch.object(scan,'wait'),patch.object(scan,'shot',return_value=(None,after)), \
             patch.object(self.flow.layout,'read_header',return_value=(True,1)), \
             patch.object(self.flow.layout,'bands',return_value=[friend_screen.CardBand(116,231)]), \
             patch.object(game_input,'press_esc',return_value=True) as keys:
            with self.assertRaises(self.flow.RosterChanged):scan.restore_list(before)
        keys.assert_called_once_with(hwnd=None)

    def test_uid_profiles_require_two_matching_reads_and_always_restore_list(self):
        scan=self.flow.UidPass(self.scan,1)
        frame=np.zeros((1080,1920,3),dtype=np.uint8)
        with patch.object(scan,'click'),patch.object(scan,'wait'),patch.object(scan,'shot',return_value=((0,0,1920,1080),frame)), \
             patch.object(ocr_engine,'recognize',return_value=[text_box('查看资料')]), \
             patch.object(self.flow.profile_layout,'read',side_effect=[
                 self.flow.profile_layout.ProfileRead(True,'123456789',[]),
                 self.flow.profile_layout.ProfileRead(True,'223456789',[])]), \
             patch.object(scan,'profile_targets',return_value=[text_box('查看资料')]), \
             patch.object(scan,'restore_list') as close:
            with self.assertRaises(self.flow.ScanIncomplete):
                scan.read_uid((0,0,1920,1080),frame,friend_screen.CardBand(116,231),1)
        close.assert_called_once()

    def test_matching_uid_reads_save_both_crops_and_restore_before_binding(self):
        scan=self.flow.UidPass(self.scan,1)
        frame=np.zeros((1080,1920,3),dtype=np.uint8)
        expected=self.flow.profile_layout.ProfileRead(True,'555555555',[
            ('UID 555555555',.97),('角色展柜',.99),('名片',.99)])
        with patch.object(scan,'click') as click,patch.object(scan,'wait'), \
             patch.object(scan,'shot',return_value=((0,0,1920,1080),frame)) as shots, \
             patch.object(self.flow.profile_layout,'menu_changed',return_value=True), \
             patch.object(ocr_engine,'recognize',return_value=[text_box('查看资料')]) as menu_ocr, \
             patch.object(self.flow.profile_layout,'read',return_value=expected) as profile_ocr, \
             patch.object(scan,'profile_targets',return_value=[text_box('查看资料')]), \
             patch.object(scan,'restore_list') as close:
            uid=scan.read_uid((0,0,1920,1080),frame,friend_screen.CardBand(116,231),1)
        self.assertEqual(uid,'555555555')
        self.assertEqual(click.call_count,2)
        self.assertEqual(shots.call_count,4)  # Includes post-OCR menu freshness capture.
        menu_ocr.assert_called_once()
        self.assertEqual(profile_ocr.call_count,2)
        close.assert_called_once()
        self.assertEqual(len(list(scan.evidence.glob('uid-001-crop-*.png'))),2)
        audit=json.loads((scan.evidence/'uid-001-read.json').read_text(encoding='utf-8'))
        self.assertTrue(audit['matched'])
        self.assertEqual(self.db.get_all_friends(),[])

    def test_profile_menu_requires_new_exact_button_and_rejects_unchanged_background_label(self):
        before=np.random.default_rng(11).integers(0,255,(1080,1920,3),dtype=np.uint8)
        menu=before.copy()
        label=text_box('查看资料',400,300)
        other=text_box('我的签名里有查看资料',500,600)
        self.assertEqual(self.flow.UidPass.profile_targets(before,menu,[label]),[])
        menu[296:335,396:505]=255-before[296:335,396:505]
        targets=self.flow.UidPass.profile_targets(before,menu,[label,other])
        self.assertEqual(targets,[label])

    def test_uid_click_is_not_sent_if_foreground_is_lost(self):
        with patch.object(window,'is_foreground',return_value=False), \
             patch.object(game_input,'move_mouse') as move,patch.object(game_input,'mouse_event') as mouse:
            self.assertFalse(game_input.click_client((0,0,1920,1080),(194,170),hwnd=123))
        move.assert_not_called();mouse.assert_not_called()

    def test_drag_holds_at_destination_for_one_second_and_cancellation_releases(self):
        clock=[0.];events=[];moves=[]
        def sleep(seconds):clock[0]+=seconds
        with patch.object(CONFIG,'drag_hold_seconds',1.),patch.object(CONFIG,'fast_settle_seconds',0.), \
             patch.object(scroller.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(scroller.time,'sleep',side_effect=sleep), \
             patch.object(game_input,'move_mouse',side_effect=lambda x,y:moves.append((x,y,clock[0]))), \
             patch.object(game_input,'mouse_event',side_effect=lambda flag:events.append((flag,clock[0]))):
            self.assertTrue(scroller.drag_client((0,0,1920,1080),(800,925),(800,200)))
            arrival=next(t for x,y,t in moves if (x,y)==(800,200))
            release=next(t for flag,t in events if flag==4)
            self.assertGreaterEqual(release-arrival,1.)
            clock[0]=0.;events.clear()
            self.assertFalse(scroller.drag_client((0,0,1920,1080),(800,925),(800,200),should_stop=lambda:clock[0]>.5))
            self.assertEqual([e[0] for e in events],[2,4])
            self.assertLess(events[-1][1],.6)

    def test_start_defaults_keep_window_visible_and_allow_ten_esc_four_o(self):
        defaults=config.Config()
        self.assertFalse(defaults.minimize_on_start)
        self.assertEqual((defaults.navigation_max_esc,defaults.friend_open_attempts,defaults.drag_hold_seconds),(10,4,1.))


class UidTraversalRegressions(unittest.TestCase):
    def setUp(self):
        FastScanRegressions.setUp(self)
        self.stack.enter_context(patch.object(game_input, 'move_client', return_value=True))
        self.stack.enter_context(patch.object(self.scan, '_wait', return_value=True))

    simulation = FastScanRegressions.simulation

    def test_real_148_card_traversal_enrolls_every_uid_then_next_scan_is_quick_only(self):
        from app import scan_workflow as flow
        state=self.simulation()
        reverse=scroller.drag_client.side_effect
        def reset_or_reverse(rect,start,end,**kwargs):
            if start[0] != self.layout.DRAG_X:
                state['offset']=0.
                return True
            return reverse(rect,start,end,**kwargs)
        scroller.drag_client.side_effect=reset_or_reverse
        def read_uid(scan,rect,frame,band,serial):
            actual=round((state['offset']+band.top-116)/self.layout.ROW_PITCH)+1
            self.assertEqual(actual,serial)
            return str(100000000+actual)
        with patch.object(self.layout,'read_header',return_value=(True,148)), \
             patch.object(flow.UidPass,'read_uid',autospec=True,side_effect=read_uid) as profiles, \
             patch.object(self.scan,'_wait',return_value=True):
            result=flow.ScanWorkflow(self.scan).run()
            self.assertTrue(result.complete,result.message)
            self.assertEqual(profiles.call_count,148)
            self.assertEqual(result.total,148)
            self.assertEqual(len({r['uid'] for r in result.friends}),148)
            ids=[r['id'] for r in result.friends]
            second=flow.ScanWorkflow(self.scan).run()
            self.assertTrue(second.complete,second.message)
            self.assertEqual(profiles.call_count,148)
            self.assertEqual(second.uid_seconds,0)
            self.assertEqual([r['id'] for r in second.friends],ids)

    def test_uid_traversal_reads_two_same_names_separately(self):
        from app import scan_workflow as flow
        self.simulation(count=2)
        read=self.layout.read_page.side_effect
        def same_names(image,**kwargs):
            page=read(image,**kwargs)
            for obs in page.observations:obs.entry.nickname='同名'
            return page
        self.layout.read_page.side_effect=same_names
        with patch.object(self.layout,'read_header',return_value=(True,2)), \
             patch.object(flow.UidPass,'read_uid',side_effect=['111111111','222222222']) as profiles, \
             patch.object(self.scan,'_wait',return_value=True):
            result=flow.ScanWorkflow(self.scan).run()
        self.assertTrue(result.complete,result.message)
        self.assertEqual(profiles.call_count,2)
        self.assertEqual([r['uid'] for r in result.friends],['111111111','222222222'])

    def test_capture_only_pass_does_not_publish_quick_draft(self):
        self.simulation(count=6)
        result=self.fast.FastScan(self.scan).run(commit=False)
        self.assertTrue(result.complete,result.message)
        self.assertEqual(self.db.get_all_friends(),[])
        self.assertIsNone(self.db.get_complete_roster())

    def test_uid_click_refuses_a_reordered_visible_page(self):
        from app import scan_workflow as flow
        self.simulation(count=2)
        rect=(0,0,1920,1080)
        frame=capture.capture_region(*rect)
        page=self.layout.read_page(frame)
        page.observations.reverse()
        scan=flow.UidPass(self.scan,2)
        with patch.object(self.layout,'read_header',return_value=(True,2)), \
             patch.object(scan,'read_uid') as read:
            with self.assertRaises(flow.RosterChanged):
                scan.after_page(rect,frame,page,[0,1])
        read.assert_not_called()

    def test_profile_return_waits_for_fade_without_extra_escape(self):
        from app import scan_workflow as flow
        self.simulation(count=2)
        rect=(0,0,1920,1080)
        frame=capture.capture_region(*rect)
        transition=frame.copy()
        transition[112:958,136:1788]=60
        scan=flow.UidPass(self.scan,2)
        with patch.object(scan,'shot',side_effect=[(rect,transition),(rect,frame)]), \
             patch.object(self.layout,'read_header',return_value=(True,2)), \
             patch.object(game_input,'press_esc',return_value=True) as escape:
            scan.restore_list(frame)
        escape.assert_called_once()
        checks=[json.loads(line) for line in (scan.evidence/'uid-checks.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([v['matched'] for v in checks],[False,True])
        self.assertFalse(list(scan.evidence.glob('uid-failed-*.png')))

    def test_cursor_is_parked_before_restoring_or_checking_the_next_friend(self):
        from app import scan_workflow as flow
        self.simulation(count=2)
        rect=(0,0,1920,1080)
        frame=capture.capture_region(*rect)
        page=self.layout.read_page(frame)
        scan=flow.UidPass(self.scan,2)
        actions=[]
        with patch.object(game_input,'move_client',side_effect=lambda *a,**kw: actions.append('park') or True), \
             patch.object(game_input,'press_esc',side_effect=lambda **kw: actions.append('esc') or True), \
             patch.object(scan,'shot',side_effect=lambda: actions.append('capture') or (rect,frame)), \
             patch.object(self.layout,'read_header',return_value=(True,2)):
            scan.restore_list(frame)
            scan.verify_visible(page)
        self.assertEqual(actions,['park','esc','capture','park','capture'])

    def test_uid_reordered_page_keeps_failure_evidence_without_full_page_logging(self):
        from app import scan_workflow as flow
        self.simulation(count=2)
        rect=(0,0,1920,1080)
        frame=capture.capture_region(*rect)
        page=self.layout.read_page(frame)
        # Same geometry and nickname but another person's avatar: still reject.
        page.observations[0].avatar_image=page.observations[1].avatar_image.copy()
        scan=flow.UidPass(self.scan,2)
        with patch.object(self.layout,'read_header',return_value=(True,2)), \
             patch.object(scan,'read_uid') as read:
            with self.assertRaises(flow.RosterChanged):
                scan.after_page(rect,frame,page,[0,1])
        read.assert_not_called()
        evidence=list(scan.evidence.glob('uid-failed-*-visible.json'))
        self.assertEqual(len(evidence),1)
        events=json.loads(evidence[0].read_text(encoding='utf-8'))
        self.assertEqual(len(events),5)
        self.assertLess(events[-1]['rows'][0]['avatar_similarity'],.80)
        image_record=evidence[0].with_name(evidence[0].stem+'-image.json')
        saved_path=Path(json.loads(image_record.read_text(encoding='utf-8'))['image'])
        self.assertEqual(saved_path.parent,self.scan.evidence_root/'uid-failures')
        self.assertFalse(evidence[0].with_suffix('.png').exists())
        saved=self.fast._decode(saved_path.read_bytes())
        self.assertEqual(saved.shape,frame.shape)
        # The bounded diagnostic archive uses JPEG, so compare decoded content
        # with a small compression tolerance instead of byte-exact PNG pixels.
        self.assertLess(float(np.mean(np.abs(saved.astype(np.int16)-frame.astype(np.int16)))),5.)
        self.assertEqual(self.db.get_all_friends(),[])

    def test_uid_count_change_restarts_immediately_without_clicking(self):
        from app import scan_workflow as flow
        self.simulation(count=2)
        rect=(0,0,1920,1080)
        frame=capture.capture_region(*rect)
        page=self.layout.read_page(frame)
        scan=flow.UidPass(self.scan,2)
        with patch.object(self.layout,'read_header',return_value=(True,3)), \
             patch.object(scan,'read_uid') as read:
            with self.assertRaises(flow.RosterChanged) as error:
                scan.after_page(rect,frame,page,[0,1])
        self.assertEqual(error.exception.reason,'count_changed')
        read.assert_not_called()


class ProfileLayoutRegressions(unittest.TestCase):
    def test_fixed_fields_never_include_own_uid_or_neighboring_list_text(self):
        from app import profile_layout as P
        frame=np.random.default_rng(19).integers(0,255,(1080,1920,3),dtype=np.uint8)
        with patch.object(ocr_engine,'recognize_lines',return_value=[
                ('UID 555555555',.97),('角色展柜',.99),('名片',.99)]) as recognize:
            result=P.read(frame)
        self.assertTrue(result.visible)
        self.assertEqual(result.uid,'555555555')
        crops=recognize.call_args.args[0]
        self.assertEqual(len(crops),3)
        for roi,image in zip(P.FIELDS,crops):
            x1,y1,x2,y2=roi
            np.testing.assert_array_equal(image,frame[y1:y2,x1:x2])
        self.assertEqual(crops[0].shape,(40,180,3))

    def test_labelled_nine_digits_and_confident_profile_tabs_are_required(self):
        from app import profile_layout as P
        for text in ['123456789','UID 12345678','UID 1234567890','UID 012345678',
                     '签名UID123456789','UID 12345678O','UID123456789 UID223456789']:
            with self.subTest(text=text):self.assertIsNone(P.uid_from_text(text,.99))
        self.assertIsNone(P.uid_from_text('UID123456789',.84))
        self.assertEqual(P.uid_from_text('uid：１２３４５６７８９',.99),'123456789')
        frame=np.zeros((1080,1920,3),dtype=np.uint8)
        for fields in [[('UID123456789',.99),('好友',.99),('我的好友',.99)],
                       [('UID123456789',.99),('角色展柜',.84),('名片',.99)]]:
            with patch.object(ocr_engine,'recognize_lines',return_value=fields):
                result=P.read(frame)
                self.assertFalse(result.visible)
                self.assertIsNone(result.uid)

    def test_cursor_move_checks_focus_stop_geometry_and_never_clicks(self):
        rect=(100,200,2660,1640)
        with patch.object(window,'is_foreground',return_value=True), \
             patch.object(window,'input_permission_error',return_value=None), \
             patch.object(window,'get_window_rect',return_value=rect), \
             patch.object(game_input,'move_mouse') as move, \
             patch.object(game_input,'mouse_event') as buttons:
            self.assertTrue(game_input.move_client(rect,(1860,1000),hwnd=7))
            move.assert_called_once_with(2580,1533)
            buttons.assert_not_called()
            move.reset_mock()
            self.assertFalse(game_input.move_client(rect,(1860,1000),hwnd=7,should_stop=lambda:True))
            with patch.object(window,'is_foreground',return_value=False):
                self.assertFalse(game_input.move_client(rect,(1860,1000),hwnd=7))
            with patch.object(window,'get_window_rect',return_value=(0,0,1920,1080)):
                with self.assertRaises(game_input.InputError):game_input.move_client(rect,(1860,1000),hwnd=7)
            move.assert_not_called()



class NavigationLimitRegressions(unittest.TestCase):
    setUp = ScreenLayoutRegressions.setUp

    def test_ten_esc_attempts_are_actually_sent(self):
        with patch.object(CONFIG,'navigation_max_esc',10), \
             patch.object(friend_screen,'is_friend_list',return_value=False), \
             patch.object(friend_screen,'is_gameplay_hud',return_value=False):
            self.assertFalse(self.scan._ensure_friend_list((0,0,1920,1080)))
        self.assertEqual(self.keys,['ESC']*10)

    def test_four_o_attempts_are_actually_sent(self):
        with patch.object(CONFIG,'friend_open_attempts',4), \
             patch.object(friend_screen,'is_friend_list',return_value=False), \
             patch.object(friend_screen,'is_gameplay_hud',return_value=True):
            self.assertFalse(self.scan._ensure_friend_list((0,0,1920,1080)))
        self.assertEqual(self.keys,['O']*4)


class UidHoverRegressions(unittest.TestCase):
    def test_uid_label_separator_does_not_discard_seventh_person(self):
        from app.profile_layout import uid_from_text
        for label in ('UID.666666666', 'UID·666666666', 'UID：666666666', 'UID 666666666'):
            with self.subTest(label=label):
                self.assertEqual(uid_from_text(label,.96621),'666666666')
        for label in ('UID..666666666','UID.66666666','UID.6666666660',
                      'UID.66666666S','UID.666666666 777777777','UID.666.666666'):
            with self.subTest(label=label):self.assertIsNone(uid_from_text(label,.99))
        self.assertIsNone(uid_from_text('UID.666666666',.84))

    def test_native_hover_and_one_pixel_row_shift_pass_but_other_people_fail(self):
        from app import fast_layout as L
        with np.load(private_fixture('uid-hover-native.npz'),allow_pickle=False) as images:
            for i in (0,4):
                with self.subTest(row=i):
                    self.assertGreaterEqual(L.visual_similarity(images[f'after_name_{i}'],images[f'before_name_{i}']),.91)
                    self.assertGreaterEqual(L.visual_similarity(images[f'after_avatar_{i}'],images[f'before_avatar_{i}']),.90)
            self.assertLess(L.visual_similarity(images['after_name_0'],images['before_name_1']),.88)
            self.assertLess(L.visual_similarity(images['after_avatar_0'],images['before_avatar_1']),.80)

    def test_pending_name_parser_still_requires_valid_level_and_status(self):
        for level,status in [('Lv.60','在线'),('Lv.58','今日内')]:
            fields=[text_box('',10,14,120,38,0),text_box(level,10,71,72,23),text_box(status,120,71,150,23)]
            self.assertIsNone(parser.parse_card(fields,115,1,2))
            parsed=parser.parse_card(fields,115,1,2,allow_pending_name=True)
            self.assertEqual(parsed.nickname,'')
            self.assertTrue(parsed.nickname_pending)
        for level,status in [('Lv.99','在线'),('','在线'),('Lv.60','不确定')]:
            fields=[text_box(level,10,71,72,23),text_box(status,120,71,150,23)]
            self.assertIsNone(parser.parse_card(fields,115,1,2,allow_pending_name=True))

    def test_rejected_visible_name_becomes_image_record_but_blank_region_does_not(self):
        import cv2
        from app import fast_layout as L, nickname_ocr
        band=friend_screen.CardBand(116,231)
        for visible in (True,False):
            frame=np.full((1080,1920,3),205,np.uint8)
            if visible:cv2.putText(frame,'Name!?',(280,161),cv2.FONT_HERSHEY_SIMPLEX,1,(55,60,70),2)
            with patch.object(L,'bands',return_value=[band]), \
                 patch.object(ocr_engine,'recognize_lines',return_value=[('好友/我的好友',.99),('1/200',.99),('bad',.8),('Lv.60',.99),('在线',.99)]), \
                 patch.object(nickname_ocr,'refine',return_value=([('',0.)],[{'method':'rejected'}])):
                page=L.read_page(frame)
            self.assertTrue(page.confirmed)
            if visible:
                self.assertEqual(len(page.entries),1)
                self.assertTrue(page.entries[0].nickname_pending)
                self.assertEqual(page.entries[0].nickname,'')
                np.testing.assert_array_equal(page.observations[0].name_image,L.crop(frame,L.field_rect(band,L.NAME)))
            else:
                self.assertIsNone(page.observations[0].entry)


class PendingNicknameRegressions(unittest.TestCase):
    setUp=WorkflowRegressions.setUp
    items=WorkflowRegressions.items
    ui=Regressions.ui

    def pending_records(self):
        items=self.items([('', '111111111',1),('', '222222222',2)])
        for obs in items:obs.entry.nickname_pending=True
        records=self.flow.records_from_observations(items)
        for r,uid in zip(records,['111111111','222222222']):r['uid']=uid
        return items,records

    def save_pending(self,records):
        self.db.apply_verified_scan(records,T0,1.,self.work.name,identity_complete=True)
        return self.db.get_display_friends()

    def test_pending_names_are_distinct_by_uid_and_images_survive_database_reopen(self):
        _,records=self.pending_records()
        path=Path(self.work.name)/'pending.db'
        with_db=Database(str(path))
        try:with_db.apply_verified_scan(records,T0,1.,self.work.name,identity_complete=True)
        finally:with_db.close()
        with_db=Database(str(path))
        try:
            rows=with_db.get_display_friends()
            self.assertEqual(len({r['id'] for r in rows}),2)
            self.assertEqual([r['uid'] for r in rows],['111111111','222222222'])
            self.assertTrue(all(r['nickname_pending'] and r['nickname']=='' for r in rows))
            for row,record in zip(rows,records):self.assertEqual(with_db.get_nickname_image(row['id']),record['name_image'])
            self.assertTrue(all(r['nickname_pending'] for r in with_db.get_complete_roster()['rows']))
        finally:with_db.close()

    def test_pending_roster_membership_matches_images_not_empty_name_counts_or_positions(self):
        items,records=self.pending_records()
        self.assertTrue(self.flow.roster_matches(items,list(reversed(items))))
        wrong=self.items([('', '111111111',3),('', '222222222',2)])
        for obs in wrong:obs.entry.nickname_pending=True
        self.assertFalse(self.flow.roster_matches(items,wrong))
        self.assertFalse(self.flow.roster_matches(items,[items[0],items[0]]))

    def test_pending_uid_reuse_follows_images_after_reordering(self):
        items,records=self.pending_records();rows=self.save_pending(records)
        mapped=self.flow.reuse_identities(list(reversed(items)),self.db.get_complete_roster(),self.db.get_visual_profiles())
        self.assertEqual([r['uid'] for r in mapped],['222222222','111111111'])
        self.assertTrue(all(r['entry'].nickname_pending for r in mapped))
        items[0].name_image=items[1].name_image.copy()
        self.assertIsNone(self.flow.reuse_identities(items,self.db.get_complete_roster(),self.db.get_visual_profiles()))

    def test_ocr_guess_cannot_clear_manual_review_or_create_fake_rename_event(self):
        items,records=self.pending_records();rows=self.save_pending(records)
        for obs in items:
            obs.entry.nickname='OCR猜测'
            obs.entry.nickname_pending=False
        mapped=self.flow.reuse_identities(items,self.db.get_complete_roster(),self.db.get_visual_profiles())
        self.assertTrue(all(r['entry'].nickname_pending for r in mapped))
        self.db.apply_verified_scan(records,T1,1.,self.work.name,identity_complete=True)
        self.assertTrue(all(r['nickname_pending'] and r['nickname']=='' for r in self.db.get_display_friends()))
        self.assertEqual([e for e in self.db.get_recent_events() if e['event_type']=='renamed'],[])
        comparison=self.flow.compare_rosters(self.db.get_complete_roster(),[{'uid':'111111111','nickname':'guess','nickname_pending':0},{'uid':'222222222','nickname':'guess','nickname_pending':0}])
        self.assertEqual(comparison['renamed'],[])

    def test_manual_review_updates_baseline_without_changing_uid_or_identity(self):
        items,records=self.pending_records();rows=self.save_pending(records)
        fid=rows[0]['id']
        self.db.update_friend_manual(fid,'111111111','准确昵称',60,None)
        saved=self.db.get_friend(fid)
        self.assertEqual((saved['nickname'],saved['nickname_pending'],saved['uid']),('准确昵称',0,'111111111'))
        baseline=self.db.get_complete_roster()['rows']
        self.assertEqual((baseline[0]['id'],baseline[0]['nickname'],baseline[0]['nickname_pending']),(fid,'准确昵称',0))
        self.assertEqual(self.db.get_nickname_image(fid),records[0]['name_image'])
        self.assertEqual(self.db._conn.execute('SELECT COUNT(*) FROM nickname_aliases WHERE alias=?',('',)).fetchone()[0],0)
        items[0].entry.nickname='准确昵称';items[0].entry.nickname_pending=False
        mapped=self.flow.reuse_identities(items,self.db.get_complete_roster(),self.db.get_visual_profiles())
        self.assertEqual([r['uid'] for r in mapped],['111111111','222222222'])
        self.assertFalse(mapped[0]['entry'].nickname_pending)

    def test_pending_screenshot_cell_and_manual_dialog_clear_pending_and_save_review_reference(self):
        from PySide6.QtWidgets import QLabel,QDialog
        from app import nickname_ocr
        _,records=self.pending_records();rows=self.save_pending(records)
        win=self.ui()
        cell=win.friends_table.item(0,1)
        self.assertFalse(cell.icon().isNull())
        self.assertEqual(cell.text(),'')
        self.assertIn('待人工核查',cell.toolTip())
        friend=self.db.get_friend(rows[0]['id']);friend['nickname_image']=records[0]['name_image']
        dialog=main_window.EditFriendDialog(friend)
        preview=dialog.findChild(QLabel,'nicknameReviewImage')
        self.assertFalse(preview.pixmap().isNull())
        dialog._nickname.setText('人工确认的昵称')
        with patch.object(main_window,'EditFriendDialog',return_value=dialog), \
             patch.object(dialog,'exec',return_value=QDialog.Accepted), \
             patch.object(nickname_ocr,'add_correction') as corrected:
            win._edit_friend(0)
        self.assertEqual(win.friends_table.item(0,1).text(),'人工确认的昵称')
        self.assertTrue(win.friends_table.item(0,1).icon().isNull())
        self.assertFalse(self.db.get_friend(rows[0]['id'])['nickname_pending'])
        corrected.assert_called_once()
        self.assertEqual(corrected.call_args.args[1],'人工确认的昵称')
        dialog.close()


class PendingUidTraversalRegressions(unittest.TestCase):
    setUp=UidTraversalRegressions.setUp
    simulation=FastScanRegressions.simulation

    def test_149_friends_with_three_pending_names_finish_uid_collection_and_next_scan_reuses_ids(self):
        from app import scan_workflow as flow
        state=self.simulation(count=149)
        reverse=scroller.drag_client.side_effect
        def reset_or_reverse(rect,start,end,**kwargs):
            if start[0]!=self.layout.DRAG_X:
                state['offset']=0.
                return True
            return reverse(rect,start,end,**kwargs)
        scroller.drag_client.side_effect=reset_or_reverse
        read=self.layout.read_page.side_effect
        pending_indices={0,6,102}
        def with_pending_names(image,**kwargs):
            page=read(image,**kwargs)
            for obs in page.observations:
                index=round((state['offset']+obs.band.top-116)/self.layout.ROW_PITCH)
                if index in pending_indices:
                    obs.entry.nickname='';obs.entry.nickname_pending=True
                    obs.name_audit={'method':'rejected','original':['ambiguous',.7]}
            return page
        self.layout.read_page.side_effect=with_pending_names
        def read_uid(scan,rect,frame,band,serial):
            actual=round((state['offset']+band.top-116)/self.layout.ROW_PITCH)+1
            self.assertEqual(actual,serial)
            return str(100000000+actual)
        with patch.object(self.layout,'read_header',return_value=(True,149)), \
             patch.object(flow.UidPass,'read_uid',autospec=True,side_effect=read_uid) as profiles:
            result=flow.ScanWorkflow(self.scan).run()
            self.assertTrue(result.complete,result.message)
            self.assertEqual((result.total,profiles.call_count,result.restarts),(149,149,0))
            self.assertEqual(len({r['uid'] for r in result.friends}),149)
            self.assertEqual(sum(r['nickname_pending'] for r in result.friends),3)
            self.assertTrue(all(self.db.get_nickname_image(r['id']) for r in result.friends if r['nickname_pending']))
            pending_evidence=list((Path(self.work.name)/'nickname-review').rglob('*.json'))
            self.assertTrue(pending_evidence)
            self.assertTrue(all(json.loads(p.read_text(encoding='utf-8'))['status']=='pending_review' for p in pending_evidence))
            ids=[r['id'] for r in result.friends]
            again=flow.ScanWorkflow(self.scan).run()
            self.assertTrue(again.complete,again.message)
            self.assertEqual((profiles.call_count,again.uid_seconds,again.restarts),(149,0,0))
            self.assertEqual([r['id'] for r in again.friends],ids)
            self.assertEqual(sum(r['nickname_pending'] for r in again.friends),3)


class SingleCharacterReturnRegressions(unittest.TestCase):
    def pair(self,index):
        from app import fast_layout as L
        fixture = 'uid-colored-hover.npz' if index == 2 else 'uid-single-character-shading.npz'
        with np.load(private_fixture(fixture),allow_pickle=False) as data:
            name,avatar=[data[f'before_{field}_{index}'].copy() for field in ('name','avatar')]
            after_name,after_avatar=[data[f'after_{field}_{index}'].copy() for field in ('name','avatar')]
        band=friend_screen.CardBand(158,273)
        obs=L.CardObservation(band,entry(),name,avatar)
        frame=np.full((1080,1920,3),180,np.uint8)
        x1,y1,x2,y2=L.field_rect(band,L.NAME);frame[y1:y2,x1:x2]=after_name
        frame[band.top+10:band.top+105,158:232]=after_avatar
        return frame,band,obs

    def test_actual_single_and_colored_names_pass_with_whole_ink_and_stronger_avatar_evidence(self):
        from app.scan_workflow import UidPass
        for index in range(3):
            frame,band,obs=self.pair(index)
            passed,rows=UidPass.visible_scores(frame,[band],[obs])
            self.assertTrue(passed,rows)
            self.assertLess(rows[0]['name_similarity'],.88)
            self.assertGreaterEqual(rows[0]['name_ink_similarity'],.95)
            self.assertGreaterEqual(rows[0]['avatar_similarity'],.90)

    def test_binary_colored_name_repro_fails_but_grayscale_edges_pass_stricter_gate(self):
        import cv2
        from app import fast_layout as L,nickname_ocr as N
        from app.scan_workflow import UidPass
        frame,band,obs=self.pair(2)
        current=L.crop(frame,L.field_rect(band,L.NAME))
        binary=L.visual_similarity(*[cv2.cvtColor(N.ink_mask(im),cv2.COLOR_GRAY2RGB) for im in (current,obs.name_image)])
        self.assertLess(binary,.88)
        passed,rows=UidPass.visible_scores(frame,[band],[obs])
        self.assertTrue(passed,rows)
        self.assertGreaterEqual(rows[0]['name_ink_similarity'],.95)

    def test_one_changed_glyph_in_colored_name_is_rejected_with_same_avatar(self):
        import cv2
        from app import fast_layout as L,nickname_ocr as N
        from app.scan_workflow import UidPass
        frame,band,obs=self.pair(2)
        other,other_band,_=self.pair(1)
        name=L.crop(frame,L.field_rect(band,L.NAME))
        character=L.crop(other,L.field_rect(other_band,L.NAME))
        x1,y1,x2,y2=N.tight_rect(N.ink_mask(name))
        cx,cy,cr,cb=N.tight_rect(N.ink_mask(character))
        # Replace only the first glyph, preserving the second glyph, total
        # name width and the original person's entire avatar.
        width=(x2-x1)//2-1
        name[y1:y2,x1:x1+width]=cv2.resize(character[cy:cb,cx:cr],(width,y2-y1),interpolation=cv2.INTER_AREA)
        left,top,right,bottom=L.field_rect(band,L.NAME)
        frame[top:bottom,left:right]=name
        passed,rows=UidPass.visible_scores(frame,[band],[obs])
        self.assertFalse(passed,rows)

    def test_same_avatar_different_single_character_is_not_accepted(self):
        from app import fast_layout as L
        from app.scan_workflow import UidPass
        frame,band,obs=self.pair(0)
        other,_,_=self.pair(1)
        x1,y1,x2,y2=L.field_rect(band,L.NAME)
        frame[y1:y2,x1:x2]=other[y1:y2,x1:x2]
        passed,rows=UidPass.visible_scores(frame,[band],[obs])
        self.assertFalse(passed,rows)

    def test_unrelated_avatar_cannot_use_a_name_foreground_fallback(self):
        from app.scan_workflow import UidPass
        frame,band,obs=self.pair(0)
        frame[band.top+10:band.top+105,158:232]=np.random.default_rng(70).integers(0,255,(95,74,3),dtype=np.uint8)
        passed,rows=UidPass.visible_scores(frame,[band],[obs])
        self.assertFalse(passed)
        self.assertIsNone(rows[0]['name_ink_similarity'])

    def test_partial_or_empty_name_cannot_match_the_full_foreground(self):
        from app import fast_layout as L,nickname_ocr as N
        frame,band,obs=self.pair(0)
        crop=L.crop(frame,L.field_rect(band,L.NAME)).copy()
        x1,y1,x2,y2=N.tight_rect(N.ink_mask(crop))
        crop[:,(x1+x2)//2:x2]=crop[:,x2+3:x2+4]
        self.assertEqual(L.nickname_ink_similarity(crop,obs.name_image),0.)
        self.assertEqual(L.nickname_ink_similarity(np.zeros_like(crop),obs.name_image),0.)


class UidMenuRecoveryRegressions(unittest.TestCase):
    setUp=UidTraversalRegressions.setUp
    simulation=FastScanRegressions.simulation

    def menu_case(self):
        from app import scan_workflow as flow
        self.simulation(count=2)
        self.stack.enter_context(patch.object(CONFIG,'menu_wait_seconds',0))
        rect=(0,0,1920,1080);frame=capture.capture_region(*rect)
        scan=flow.UidPass(self.scan,2)
        expected=flow.profile_layout.ProfileRead(True,'123456789',[])
        self.stack.enter_context(patch.object(self.layout,'read_header',return_value=(True,2)))
        shots=self.stack.enter_context(patch.object(scan,'shot',return_value=(rect,frame)))
        self.stack.enter_context(patch.object(ocr_engine,'recognize',return_value=[]))
        self.stack.enter_context(patch.object(flow.profile_layout,'read',return_value=expected))
        close=self.stack.enter_context(patch.object(scan,'restore_list'))
        click=self.stack.enter_context(patch.object(scan,'click'))
        return scan,rect,frame,shots,close,click

    def test_delayed_menu_is_polled_without_clicking_avatar_twice(self):
        scan,rect,frame,shots,close,click=self.menu_case()
        with patch.object(scan,'profile_targets',side_effect=[[],[text_box('查看资料')]]):
            uid=scan.read_uid(rect,frame,friend_screen.CardBand(116,231),1)
        self.assertEqual(uid,'123456789')
        self.assertEqual(click.call_count,2)
        self.assertEqual(shots.call_count,6)
        close.assert_called_once()

    def test_missed_click_is_retried_once_after_list_identity_check(self):
        scan,rect,frame,shots,close,click=self.menu_case()
        with patch.object(scan,'profile_targets',side_effect=[[],[],[],[text_box('查看资料')]]):
            uid=scan.read_uid(rect,frame,friend_screen.CardBand(116,231),1)
        self.assertEqual(uid,'123456789')
        self.assertEqual(click.call_count,3)
        self.assertEqual(shots.call_count,10)
        close.assert_called_once()
        audit=json.loads((scan.evidence/'uid-001-menu-read.json').read_text(encoding='utf-8'))
        self.assertTrue(audit[2]['list_verified'])
        self.assertEqual(audit[3]['click_attempt'],1)

    def test_menu_never_opens_stops_after_two_clicks_without_esc_closing_list(self):
        from app.fast_scan import ScanIncomplete
        scan,rect,frame,shots,close,click=self.menu_case()
        with patch.object(scan,'profile_targets',return_value=[]):
            with self.assertRaises(ScanIncomplete):scan.read_uid(rect,frame,friend_screen.CardBand(116,231),1)
        self.assertEqual(click.call_count,2)
        self.assertEqual(shots.call_count,12)
        close.assert_not_called()
        evidence=json.loads(next(scan.evidence.glob('uid-failed-*-menu-001.json')).read_text(encoding='utf-8'))
        self.assertEqual(len(evidence['checks']),6)
        self.assertTrue(evidence['checks'][-1]['list_verified'])

    def test_count_change_during_menu_wait_restarts_without_another_click_or_escape(self):
        from app.fast_scan import RosterChanged
        scan,rect,frame,shots,close,click=self.menu_case()
        with patch.object(scan,'profile_targets',return_value=[]), \
             patch.object(self.layout,'read_header',return_value=(True,3)):
            with self.assertRaises(RosterChanged) as error:
                scan.read_uid(rect,frame,friend_screen.CardBand(116,231),1)
        self.assertEqual(error.exception.reason,'count_changed')
        self.assertEqual(click.call_count,1)
        close.assert_not_called()

    def test_changed_list_or_ambiguous_menu_never_gets_a_blind_avatar_retry(self):
        from app.fast_scan import ScanIncomplete
        scan,rect,frame,shots,close,click=self.menu_case()
        for candidates in ([],[text_box('查看资料'),text_box('个人资料')]):
            click.reset_mock();close.reset_mock()
            with patch.object(scan,'profile_targets',return_value=candidates), \
                 patch.object(scan,'visible_scores',return_value=(False,[])):
                with self.assertRaises(ScanIncomplete):scan.read_uid(rect,frame,friend_screen.CardBand(116,231),1)
            self.assertEqual(click.call_count,1)
            close.assert_called_once()


class UidBottomSettleRegressions(unittest.TestCase):
    setUp = UidTraversalRegressions.setUp
    simulation = FastScanRegressions.simulation

    def fixture_case(self, shifts=None):
        from app import fast_layout as L
        metadata=json.loads((private_fixture('uid-bottom-settle.json')).read_text(encoding='utf-8'))
        frame=np.full((1080,1920,3),180,np.uint8)
        observations=[];bands=[]
        with np.load(private_fixture('uid-bottom-settle.npz'),allow_pickle=False) as data:
            for i, (old,new,fields) in enumerate(zip(metadata['before_bands'],metadata['after_bands'],metadata['entries'])):
                old_band=friend_screen.CardBand(*old)
                shift=(new[0]-old[0]) if shifts is None else shifts[i]
                band=friend_screen.CardBand(old[0]+shift,new[1]+shift-(new[0]-old[0]))
                obs=L.CardObservation(old_band,parser.FriendEntry(**fields),
                    data[f'before_name_{i}'].copy(),data[f'before_avatar_{i}'].copy())
                x1,y1,x2,y2=L.field_rect(band,L.NAME)
                frame[y1:y2,x1:x2]=data[f'after_name_{i}']
                frame[band.top+10:band.top+105,158:232]=data[f'after_avatar_{i}']
                observations.append(obs);bands.append(band)
        return frame,bands,observations

    def controlled_scan(self, frames):
        from app.scan_workflow import UidPass
        scan=UidPass(self.scan,149)
        rect=(0,0,1920,1080)
        by_frame={id(frame):bands for frame,bands,_ in frames}
        self.stack.enter_context(patch.object(self.layout,'read_header',return_value=(True,149)))
        self.stack.enter_context(patch.object(self.layout,'bands',side_effect=lambda image, interface_mode='normal':by_frame[id(image)]))
        shots=self.stack.enter_context(patch.object(scan,'shot',side_effect=[(rect,f[0]) for f in frames]))
        return scan,rect,shots

    def test_actual_bottom_six_names_and_avatars_survive_fourteen_pixel_settle(self):
        from app.scan_workflow import UidPass
        frame,bands,obs=self.fixture_case()
        matched,rows=UidPass.visible_scores(frame,bands,obs)
        self.assertTrue(matched,rows)
        self.assertEqual([r['y_shift'] for r in rows],[14]*6)
        self.assertTrue(all(r['translated'] and r['aligned_top_delta']==0 for r in rows))
        self.assertGreater(min(r['name_similarity'] for r in rows),.98)
        self.assertGreater(min(r['avatar_similarity'] for r in rows),.98)

    def test_translation_cannot_accept_independent_drift_whole_row_jump_or_two_rows(self):
        from app.scan_workflow import UidPass
        for shifts,count in [([14,0,0,0,0,0],6),([-125]*6,6),([14]*6,2)]:
            with self.subTest(shifts=shifts,count=count):
                frame,bands,obs=self.fixture_case(shifts)
                matched,rows=UidPass.visible_scores(frame,bands[:count],obs[:count])
                self.assertFalse(matched,rows)
                self.assertTrue(all(r['name_matched'] and r['avatar_similarity']>.98 for r in rows))

    def test_translation_still_rejects_changed_avatar_changed_name_and_swapped_people(self):
        from app.scan_workflow import UidPass
        for field in ('avatar','name','both'):
            with self.subTest(field=field):
                frame,bands,obs=self.fixture_case()
                if field == 'avatar':
                    # Several real tail rows use the same protagonist portrait;
                    # a different person can legitimately share that image.
                    obs[1].avatar_image=np.random.default_rng(2091).integers(0,255,obs[1].avatar_image.shape,dtype=np.uint8)
                elif field == 'both':
                    obs[1].avatar_image,obs[4].avatar_image=obs[4].avatar_image,obs[1].avatar_image
                if field in ('name','both'):
                    obs[1].name_image,obs[4].name_image=obs[4].name_image,obs[1].name_image
                matched,rows=UidPass.visible_scores(frame,bands,obs)
                self.assertFalse(matched,rows)
                self.assertTrue(all(r['geometry_matched'] for r in rows))

    def test_shifted_page_needs_two_consecutive_stable_frames(self):
        cases=[self.fixture_case([shift]*6) for shift in (14,18,18)]
        scan,rect,shots=self.controlled_scan(cases)
        actual_rect,frame,bands=scan.await_visible(cases[0][2],'test_settle')
        self.assertEqual(shots.call_count,3)
        self.assertEqual(actual_rect,rect)
        self.assertIs(frame,cases[-1][0])
        self.assertEqual([b.top for b in bands],[o.band.top+18 for o in cases[0][2]])
        events=[json.loads(line) for line in (scan.evidence/'uid-checks.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['ready'] for r in events],[False,False,True])
        self.assertTrue(all(r['matched'] for r in events))

    def test_continuous_settling_stops_without_claiming_roster_change_or_clicking(self):
        from app.scan_workflow import RosterChanged,ScanIncomplete
        cases=[self.fixture_case([shift]*6) for shift in (14,18,22,26,30)]
        scan,rect,shots=self.controlled_scan(cases)
        page=friend_screen.FriendPage(confirmed=True,expected_total=149,observations=cases[0][2])
        with patch.object(scan,'read_uid') as read:
            with self.assertRaises(ScanIncomplete) as error:
                scan.after_page(rect,cases[0][0],page,list(range(143,149)))
        self.assertNotIsInstance(error.exception,RosterChanged)
        self.assertIn('仍在移动',str(error.exception))
        self.assertEqual(shots.call_count,5)
        read.assert_not_called()
        self.assertEqual(self.db.get_all_friends(),[])

    def test_actual_last_page_reads_only_five_missing_uids_at_fresh_centers(self):
        frame,bands,obs=self.fixture_case()
        scan,rect,shots=self.controlled_scan([(frame,bands,obs)]*12)
        page=friend_screen.FriendPage(confirmed=True,expected_total=149,observations=obs)
        # Test-only fake UIDs; never substitute these values for real profiles.
        scan.uids={i:str(100000001+i) for i in range(144)}
        saved=scan.uids.copy();scan.offset=17769.4
        def read_uid(actual_rect,before,band,serial):
            self.assertEqual(actual_rect,rect)
            self.assertIs(before,frame)
            self.assertEqual(band,bands[serial-144])
            return str(100000000+serial)
        with patch.object(scan,'read_uid',side_effect=read_uid) as read:
            _,fresh=scan.after_page(rect,frame,page,list(range(143,149)))
        self.assertIs(fresh,frame)
        self.assertEqual([call.args[-1] for call in read.call_args_list],list(range(145,150)))
        self.assertEqual(len(scan.uids),149)
        self.assertEqual(len(set(scan.uids.values())),149)
        self.assertEqual({i:scan.uids[i] for i in saved},saved)
        self.assertAlmostEqual(scan.offset,17755.4)
        shift=json.loads((scan.evidence/'uid-layout.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(shift['y_shift'],14)
        self.assertEqual(shots.call_count,12)
        self.assertEqual(self.db.get_all_friends(),[])

    def test_settle_after_profile_needs_only_one_escape(self):
        frame,bands,obs=self.fixture_case()
        scan,rect,shots=self.controlled_scan([(frame,bands,obs)]*2)
        with patch.object(scan,'visible_reference',return_value=obs), \
             patch.object(game_input,'press_esc',return_value=True) as esc:
            scan.restore_list(frame)
        esc.assert_called_once()
        self.assertEqual(shots.call_count,2)

    def test_count_change_during_settle_still_restarts_immediately(self):
        from app.scan_workflow import RosterChanged
        case=self.fixture_case();scan,rect,shots=self.controlled_scan([case])
        self.layout.read_header.return_value=(True,150)
        with self.assertRaises(RosterChanged) as error:
            scan.await_visible(case[2],'test_count_change')
        self.assertEqual(error.exception.reason,'count_changed')
        self.assertEqual(shots.call_count,1)

    def test_avatar_retry_after_settle_uses_stable_new_position(self):
        from app import scan_workflow as flow
        frame,bands,obs=self.fixture_case()
        # Four menu observations each have a post-OCR freshness capture,
        # alongside two settling frames and two independent profile frames.
        scan,rect,shots=self.controlled_scan([(frame,bands,obs)]*12)
        target=text_box('查看资料')
        expected=flow.profile_layout.ProfileRead(True,'123456789',[])
        with patch.object(CONFIG,'menu_wait_seconds',0), \
             patch.object(scan,'visible_reference',return_value=obs), \
             patch.object(scan,'profile_targets',side_effect=[[],[],[],[target]]), \
             patch.object(ocr_engine,'recognize',return_value=[]), \
             patch.object(flow.profile_layout,'read',return_value=expected), \
             patch.object(scan,'click') as click,patch.object(scan,'restore_list') as close:
            result=scan.read_uid(rect,frame,obs[1].band,145)
        self.assertEqual(result,'123456789')
        self.assertEqual(click.call_count,3)
        self.assertEqual(click.call_args_list[1].args[1][1],bands[1].center)
        self.assertEqual(shots.call_count,12)
        close.assert_called_once()

    def test_full_149_uid_traversal_stays_aligned_after_midpage_settle(self):
        from app import scan_workflow as flow
        state=self.simulation(count=149)
        read=self.layout.read_page.side_effect
        state['shifted']=False
        def settle_once(image,**kwargs):
            page=read(image,**kwargs)
            if not state['shifted'] and state['offset']>700:
                state['offset']-=14
                state['shifted']=True
            return page
        self.layout.read_page.side_effect=settle_once
        def read_uid(rect,frame,band,serial):
            actual=round((state['offset']+band.top-116)/self.layout.ROW_PITCH)+1
            self.assertEqual(actual,serial)
            return str(100000000+actual)
        scan=flow.UidPass(self.scan,149)
        with patch.object(self.layout,'read_header',return_value=(True,149)), \
             patch.object(scan,'read_uid',side_effect=read_uid) as profiles:
            result=scan.run(commit=False)
        self.assertTrue(result.complete,result.message)
        self.assertTrue(state['shifted'])
        self.assertEqual(profiles.call_count,149)
        self.assertEqual(len(set(scan.uids.values())),149)
        self.assertEqual([c.args[-1] for c in profiles.call_args_list],list(range(1,150)))
        shifts=[json.loads(line) for line in (scan.evidence/'uid-layout.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(s['y_shift']==14 for s in shifts))
        self.assertEqual(self.db.get_all_friends(),[])


if __name__ == '__main__':
    unittest.main()

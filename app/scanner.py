"""扫描入口与线程协调。

默认快速监测按昵称对应，同名、无法匹配或疑似改名时单独核验UID；全量UID由手动入口触发。
行号仅用于单轮覆盖检查，本地档案保存统计，UID用于确认改名后的历史连续性。
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Set

from app import capture, friend_screen, input, ocr_engine, parser, scroller, window
from app.config import CONFIG, parse_scan_frequency
from app.database import Database, now_str
from app.logger import logger, ocr_dump_logger
from app.scan_retention import retained_scan_round


@dataclass
class ScanResult:
    scan_time: str
    total: int
    online: int
    offline: int
    friends: List[dict] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)
    message: str = ""
    complete: bool = False
    mode: str = "scan"
    quick_seconds: float = 0.
    uid_seconds: float = 0.
    workflow_seconds: float = 0.
    uid_complete: bool = False
    restarts: int = 0
    comparison: dict = field(default_factory=dict)


class Scanner:
    def __init__(
        self,
        db: Database,
        on_result: Optional[Callable[[ScanResult], None]] = None,
        on_message: Optional[Callable[[str], None]] = None,
    ):
        self.db = db
        self.on_result = on_result
        self.on_message = on_message
        self._stop = threading.Event()
        self._uid_stop = threading.Event()
        self._worker_state = threading.local()
        self._lifecycle_lock = threading.Lock()
        self._seen_statuses = {}
        self._missing_counts = {}
        self._pause = threading.Event()       # set = 已暂停
        self._esc_held = False                # ESC 键去抖（避免长按连续触发）
        self._thread: Optional[threading.Thread] = None
        self._uid_thread: Optional[threading.Thread] = None
        self._hwnd: Optional[int] = None
        self._manual_rect = None               # 手动框选区域 (left, top, right, bottom)
        self._rect = None
        self._navigation_error = ""
        self.interface_mode = None            # Reconfirmed from the HUD before each round.
        self._seen_ids: Set[int] = set()       # 本次扫描看到的好友 id
        self._seen_ids_prev: Set[int] = set()  # 上次扫描看到的好友 id
        self._online = 0
        self._offline = 0

    @property
    def account_uid(self):
        return getattr(self.db, 'account_uid', None)

    @property
    def evidence_root(self):
        base = Path(CONFIG.log_dir)
        return base/'accounts'/self.account_uid if self.account_uid else base

    def _verify_account_frame(self, image):
        from app.account_identity import AccountIdentityError, verify_account_frame
        if not self.account_uid:
            # Ephemeral databases support offline replay; persisted rosters must have an owner.
            if getattr(self.db, '_path', None) == ':memory:':
                return
            raise AccountIdentityError('请先识别并选择账号；未归属的旧名单仅供查看')
        try:
            verify_account_frame(image, self.account_uid)
        except AccountIdentityError:
            self._stop.set()
            self._uid_stop.set()
            raise

    def _confirm_account(self, rect):
        from app.account_identity import AccountIdentityError, read_current_account
        if not self.account_uid:
            self._verify_account_frame(None)
            return
        try:
            read_current_account(rect, expected_uid=self.account_uid, hwnd=self._hwnd,
                                 should_continue=self._check_pause)
        except AccountIdentityError:
            self._stop.set()
            self._uid_stop.set()
            raise

    # ------------------------------------------------------------------
    def set_window(self, hwnd: int) -> None:
        if hwnd != self._hwnd or self._manual_rect is not None:
            self._reset_history()
        self._hwnd = hwnd
        self._manual_rect = None

    def set_manual_rect(self, rect) -> None:
        """设置手动框选的扫描区域 (left, top, right, bottom)。"""
        if tuple(rect) != self._manual_rect:
            self._reset_history()
        self._manual_rect = tuple(rect)
        self._hwnd = None

    def clear_manual_rect(self) -> None:
        self._manual_rect = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_uid_scanning(self) -> bool:
        return self._uid_thread is not None and self._uid_thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    def pause(self) -> None:
        if not self._pause.is_set():
            self._pause.set()
            self._say("已暂停扫描（点「继续」或按 ESC 恢复）")

    def resume(self) -> None:
        if self._pause.is_set():
            self._pause.clear()
            self._say("已恢复扫描")

    def toggle_pause(self) -> None:
        if self._pause.is_set():
            self.resume()
        else:
            self.pause()

    def _reset_history(self) -> None:
        self._seen_ids_prev.clear()
        self._missing_counts.clear()

    def _active_stop(self):
        return getattr(self._worker_state, 'stop_event', self._stop)

    def _wait(self, seconds: float) -> bool:
        return not self._active_stop().wait(max(0, seconds))

    def _check_pause(self) -> bool:
        """轮询暂停/停止状态；ESC 键切换暂停。返回 True 表示应继续，False 表示已停止。

        暂停期间鼠标不被程序占用，用户可自由移动；按 ESC 或点「继续」恢复。
        """
        import win32api
        while True:
            if self._active_stop().is_set():
                return False
            pressed = False
            try:
                pressed = bool(win32api.GetAsyncKeyState(0x1B) & 0x8000)  # VK_ESCAPE
            except Exception:
                pass
            if pressed:
                if not self._esc_held:
                    self._esc_held = True
                    self.toggle_pause()
            else:
                self._esc_held = False
            if not self._pause.is_set():
                return True
            time.sleep(0.05)

    def _should_stop(self) -> bool:
        """供滚动函数回调：返回 True 表示立即中断（释放鼠标）。

        轮询 ESC 键切换暂停，但**不阻塞**——拖动过程中一旦暂停/停止，
        立即返回 True 让滚动函数释放鼠标左键，避免鼠标被一直按住。
        """
        import win32api
        if self._active_stop().is_set():
            return True
        pressed = False
        try:
            pressed = bool(win32api.GetAsyncKeyState(0x1B) & 0x8000)  # VK_ESCAPE
        except Exception:
            pass
        if pressed:
            if not self._esc_held:
                self._esc_held = True
                self.toggle_pause()
        else:
            self._esc_held = False
        return self._pause.is_set()

    def _get_rect(self):
        if self._manual_rect is not None:
            return self._manual_rect
        if self._hwnd is None:
            raise RuntimeError("尚未选择目标窗口")
        rect = window.get_window_rect(self._hwnd)
        if rect is None:
            raise RuntimeError("无法获取窗口位置，请确认游戏窗口已打开")
        return rect

    def _say(self, msg: str) -> None:
        logger.info(msg)
        if self.on_message:
            self.on_message(msg)

    def _navigation_failed(self, message: str) -> bool:
        self._navigation_error = message
        self._say(message)
        return False

    def _unsupported_view(self, message):
        # A resolution change needs user action, not a tight continuous retry.
        self._active_stop().set()
        return self._navigation_failed(message)

    def _startup_countdown(self) -> bool:
        """只在用户启动任务时倒计时，工作线程等待，主界面仍可停止/关闭。"""
        remaining = max(0.0, min(30.0, float(CONFIG.startup_delay_seconds)))
        displayed = None
        while remaining > 0:
            if not self._check_pause():
                return False
            seconds = math.ceil(remaining)
            if seconds != displayed:
                self._say('准备扫描：%d 秒后开始，请切回游戏窗口（可点停止取消）' % seconds)
                displayed = seconds
            before = time.monotonic()
            if not self._wait(min(.1, remaining)):
                return False
            remaining -= max(0.0, time.monotonic() - before)
        return self._check_pause()

    def _wait_for_game_foreground(self, *, activate: bool = False) -> bool:
        """激活失败后保留当前任务，等待用户切回，而不是让用户重新点开始。

        等待期间不截图、不发送键鼠事件；暂停时间不消耗焦点等待时限。
        手动框选没有窗口句柄，仍由严格的截图标题验证把关。
        """
        if self._hwnd is None:
            return not self._active_stop().is_set()
        if not self._check_pause():
            return self._navigation_failed('任务已停止')
        if window.is_foreground(self._hwnd):
            return True
        if activate and CONFIG.bring_to_front:
            # 返回 False 只表示这次主动激活未成功，继续等待手动切回。
            window.bring_to_front(self._hwnd)
        remaining = max(0.0, min(300.0, float(CONFIG.focus_wait_seconds)))
        announced = False
        while self._check_pause():
            if window.get_window_rect(self._hwnd) is None:
                return self._navigation_failed('目标游戏窗口已失效，请重新选择窗口')
            if window.is_foreground(self._hwnd):
                # 等切换动画/Alt+Tab 稳定后再截图，避免刚切回就过早发送 O。
                before = time.monotonic()
                if not self._wait(min(.3, remaining)):
                    return self._navigation_failed('任务已停止')
                remaining -= max(0.0, time.monotonic() - before)
                if window.is_foreground(self._hwnd):
                    if announced:
                        self._say('已检测到游戏进入前台，继续当前任务')
                    return True
            if remaining <= 0:
                return self._navigation_failed('等待切回游戏超时，当前操作未继续；再次开始后请在准备期间切回游戏')
            if not announced:
                self._say('等待游戏进入前台（最多 %d 秒）：请点击游戏或按 Alt+Tab 切回，无需再次点击开始；可点停止取消' % math.ceil(remaining))
                announced = True
            before = time.monotonic()
            if not self._wait(min(.1, remaining)):
                return self._navigation_failed('任务已停止')
            remaining -= max(0.0, time.monotonic() - before)
        return self._navigation_failed('任务已停止')

    def _navigation_key(self, vk: int) -> bool:
        if not self._check_pause():
            return self._navigation_failed('导航已停止')
        if self._hwnd is None or not window.is_foreground(self._hwnd):
            return self._navigation_failed('目标游戏未处于前台，未发送按键；请切回游戏后重试')
        try:
            if vk == 0x1B:
                input.press_esc(hwnd=self._hwnd)
            else:
                input.press_key(vk, hwnd=self._hwnd)
        except (input.InputError, OSError) as error:
            return self._navigation_failed(str(error))
        self._say('Windows 已接收 %s 键，等待界面确认' % ('ESC' if vk == 0x1B else chr(vk)))
        return self._wait(CONFIG.navigation_wait_seconds)

    def _read_interface_mode(self, image, rect):
        previous = None
        for attempt in range(4):
            if not self._check_pause():
                return None
            if attempt:
                if not self._wait(.15) or not self._wait_for_game_foreground():
                    return None
                rect = self._get_rect()
                image = capture.capture_window(self._hwnd, *rect)
            mode = friend_screen.gameplay_interface_mode(image)
            if mode is not None and mode == previous:
                return mode
            previous = mode
        return None

    def _ensure_friend_list(self, rect) -> bool:
        self._navigation_error = ""
        self.interface_mode = None
        if self._hwnd is None and self._manual_rect is not None:
            matched = window.find_client_window(rect, CONFIG.window_title_keyword)
            if matched is not None:
                self._hwnd = matched
                self._say('手动区域已对应到游戏窗口，先核对左下角界面提示')
        esc_count = open_count = 0
        max_esc = max(0, min(10, int(CONFIG.navigation_max_esc)))
        max_open = max(1, min(4, int(CONFIG.friend_open_attempts)))
        while self._check_pause():
            if not self._wait_for_game_foreground():
                return False
            # 每次重取客户区，避免窗口移动后仍读旧坐标。
            rect = self._get_rect()
            problem = friend_screen.game_window_problem(rect)
            if problem:
                return self._unsupported_view(problem)
            img = capture.capture_window(self._hwnd, *rect)
            if img is None:
                self._say('导航截图失败，已停止自动按键')
                return False
            if friend_screen.normalize(img) is None:
                return self._unsupported_view('截图不是完整的 16:9 游戏画面，已停止扫描；请调整游戏分辨率后重新开始。')
            in_list = friend_screen.is_friend_list(img)
            if in_list and self.interface_mode is not None:
                return True
            if not CONFIG.ensure_friend_list:
                return self._navigation_failed('自动导航已关闭；请启用自动导航，以便先核对左下角 Enter / Alt+N 再打开好友列表')
            if self._hwnd is not None:
                problem = window.input_permission_error(self._hwnd)
                if problem:
                    return self._navigation_failed(problem)
            if self._hwnd is None:
                return self._navigation_failed('框选区域无法唯一对应游戏客户区；请使用“查找窗口”，以便核对左下角 Enter / Alt+N')
            if not in_list and friend_screen.is_gameplay_hud(img):
                if self.interface_mode is None:
                    self.interface_mode = self._read_interface_mode(img, rect)
                    if self.interface_mode is None:
                        return self._navigation_failed('未能连续确认左下角 Enter / Alt+N，无法区分冒险等级与千星等级，未开始扫描')
                    self._say('已确认%s好友界面；两种等级分别保存' %
                              ('千星奇域' if self.interface_mode == 'qianxing' else '普通'))
                if open_count >= max_open:
                    return self._navigation_failed('O 键已发送，但好友界面仍未打开；请确认游戏快捷键为 O，或手动打开后重试')
                open_count += 1
                self._say('已识别左下角 Enter，准备发送 O 键（%d/%d）' % (open_count, max_open))
                if not self._navigation_key(0x4F):
                    return False
            else:
                if esc_count >= max_esc:
                    self._say('达到 ESC 尝试上限，仍未识别主界面或好友界面，已停止导航')
                    return False
                esc_count += 1
                self._say('返回主画面核对左下角提示，按 ESC（%d/%d）' % (esc_count, max_esc))
                if not self._navigation_key(0x1B):
                    return False
        return False

    # ------------------------------------------------------------------
    def start(self) -> bool:
        with self._lifecycle_lock:
            if self.is_running or self.is_uid_scanning:
                return False
            # Validate before creating a worker, even when config.json was edited.
            parse_scan_frequency(CONFIG.scan_frequency_minutes)
            self._stop = threading.Event()
            self._pause.clear()
            self._esc_held = False
            self._thread = threading.Thread(target=self._run, daemon=True, name='scan-loop')
            self._thread.start()
        return True

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            self._uid_stop.set()
            self._pause.clear()
        # OCR may still be inside a noninterruptible model call. Freeze presence
        # now rather than charging that delay (especially for continuous mode).
        self.db.configure_monitoring(None)
        self._say('正在停止扫描……')

    def _run(self) -> None:
        self._worker_state.stop_event = self._stop
        minutes = parse_scan_frequency(CONFIG.scan_frequency_minutes)
        # The visible frequency is authoritative. Old verification launches
        # persisted scan_once_only=True; that flag must not turn a user's 0 or
        # positive frequency back into a single round.
        interval = None if minutes is None else minutes * 60
        self._worker_state.online_only = bool(CONFIG.online_only)
        monitor_stop = threading.Event()
        monitor = None
        try:
            self.db.configure_monitoring(interval)
            self.db.begin_observation_session()
            cadence = ('单次' if interval is None else
                       '连续' if interval == 0 else '每 %g 分钟' % minutes)
            self._say('快速扫描已启动：%s%s' %
                      (cadence, '，只扫描在线好友' if self._worker_state.online_only else ''))
            if interval is not None and interval > 0:
                # Keep expiry independent of OCR/navigation, which can block for
                # longer than an interval. This worker never touches game input.
                monitor = threading.Thread(target=self._monitor_presence,
                                           args=(monitor_stop,), daemon=True,
                                           name='presence-expiry')
                monitor.start()
            if not self._startup_countdown():
                return
            while not self._active_stop().is_set():
                started = time.monotonic()
                try:
                    result = self.scan_once()
                    if not result.complete:
                        self.db.mark_scan_failed()
                    if self.on_result:
                        self.on_result(result)
                except Exception as e:
                    if isinstance(e, capture.CaptureError):
                        # Geometry failures require user action, not a tight
                        # continuous retry loop that keeps activating the game.
                        self._active_stop().set()
                    self.db.mark_scan_failed()
                    self._missing_counts.clear()
                    logger.exception('扫描出错')
                    self._say('扫描出错: %s' % e)
                    if self.on_result:
                        self.on_result(self._build_result(now_str(), False, '扫描出错: %s' % e))
                if interval is None:
                    break
                # Start-to-start cadence; a slow round never queues overlapping
                # jobs. Zero starts the next round immediately, still serially.
                if not self._wait_next_round(started + interval):
                    break
        finally:
            monitor_stop.set()
            if monitor is not None:
                monitor.join(1)
            self.db.configure_monitoring(None)

    def _monitor_presence(self, stop_event) -> None:
        while not stop_event.wait(.2):
            try:
                self.db.expire_presence()
            except Exception:
                logger.exception('在线状态到期处理失败')
                # A broken database must not silently keep a monitoring task on.
                self._stop.set()
                return

    def _wait_next_round(self, deadline: float) -> bool:
        while not self._active_stop().is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            if not self._wait(min(remaining, 60.)):
                return False
        return False

    def _scroll_to_top(self, rect) -> bool:
        while self._check_pause():
            if not self._wait_for_game_foreground():
                return False
            rect = self._get_rect()
            if scroller.scroll_to_top(rect, should_stop=self._should_stop, hwnd=self._hwnd) is not False:
                return True
            if not self.is_paused:
                break
        return False

    # ------------------------------------------------------------------
    @retained_scan_round
    def scan_once(self) -> ScanResult:
        self._seen_ids = set()
        self._seen_statuses = {}
        self._online = self._offline = 0
        now = now_str()
        rect = self._get_rect()
        self._rect = rect
        if not self._check_pause():
            return self._build_result(now, False, '已停止')
        if self._hwnd is not None:
            problem = window.input_permission_error(self._hwnd)
            if problem:
                self._say(problem)
                return self._build_result(now, False, problem)
        if not self._wait_for_game_foreground(activate=True):
            return self._build_result(now, False, self._navigation_error)
        if not self._ensure_friend_list(rect):
            return self._build_result(now, False, self._navigation_error or '未能确认好友列表')
        rect = self._get_rect()
        self._say('开始扫描窗口 (%d,%d)-(%d,%d) …' % rect)
        self._confirm_account(rect)
        if getattr(self._worker_state, 'online_only', CONFIG.online_only):
            from app.quick_scan import QuickScanWorkflow
            return QuickScanWorkflow(self, online_only=True).run()
        if CONFIG.fast_scan or self.interface_mode == 'qianxing':
            from app.quick_scan import QuickScanWorkflow
            return QuickScanWorkflow(self).run()
        if not self._scroll_to_top(rect):
            return self._build_result(now, False, '回到顶部时停止或滚动失败')

        previous = None
        previous_signature = None
        stable = 0
        moved = False
        hit_bottom = False
        reliable = True
        expected_total = None
        note = '达到最大滚动次数，扫描不完整'
        for page in range(CONFIG.max_scrolls):
            if not self._check_pause():
                note = '已停止，扫描不完整'
                break
            if not self._wait_for_game_foreground():
                note = self._navigation_error
                break
            try:
                rect = self._get_rect()
                self._rect = rect
            except RuntimeError:
                note = '目标窗口失效，扫描不完整'
                break
            img = capture.capture_window(self._hwnd, *rect)
            if img is None:
                note = '截图失败，扫描不完整'
                break
            self._verify_account_frame(img)
            page_result = friend_screen.read_page(img, left_offset=rect[0], top_offset=rect[1],
                                                  should_continue=self._check_pause)
            if not page_result.confirmed:
                note = '好友界面校验失败，扫描不完整：' + '；'.join(page_result.issues)
                break
            if not page_result.reliable:
                reliable = False
                self._say('；'.join(page_result.issues))
            count = page_result.expected_total
            if count is not None:
                if expected_total is not None and expected_total != count:
                    reliable = False
                expected_total = count
            entries = page_result.entries
            if not entries and count != 0:
                reliable = False
            signature = tuple((e.nickname, round((e.click_y - rect[1]) / 8)) for e in entries)
            if previous is not None:
                same = signature == previous_signature
                if same and capture.image_similarity(img, previous) >= CONFIG.bottom_similarity:
                    stable += 1
                else:
                    stable = 0
                if signature and previous_signature and not same:
                    moved = True
            if page_result.texts:
                ocr_dump_logger.info('屏%d: %s', page, ' | '.join(page_result.texts))
            for entry in entries:
                if not self._check_pause():
                    break
                if self._process_entry(entry, rect, now_str()) is None:
                    reliable = False
            if self._active_stop().is_set():
                note = '已停止，扫描不完整'
                break
            if stable >= max(1, CONFIG.bottom_confirmations):
                hit_bottom = True
                break
            previous, previous_signature = img, signature
            if not self._wait_for_game_foreground():
                note = self._navigation_error
                break
            rect = self._get_rect()
            if scroller.scroll_down(rect, should_stop=self._should_stop, hwnd=self._hwnd) is False:
                note = '滚动停止或失败，扫描不完整'
                break

        count_matches = expected_total is not None and expected_total == len(self._seen_ids)
        complete = (hit_bottom and reliable and (moved or count_matches)
                    and (expected_total is None or count_matches))
        if complete:
            note = '已完成扫描'
            if count_matches:
                self._detect_removed(now_str())
            else:
                self._missing_counts.clear()
                self._seen_ids_prev.update(self._seen_ids)
                note += '；未识别好友总数，暂不判断消失'
            self.db.add_session(now, len(self._seen_ids), self._online, self._offline, friend_ids=self._seen_ids)
        else:
            self._missing_counts.clear()
            if hit_bottom:
                note = '未能核实完整好友名单，暂不判断消失'
        self._say('%s：共 %d 位，在线 %d，离线 %d' %
                  (note, len(self._seen_ids), self._online, self._offline))
        return self._build_result(now, complete, note)

    def _build_result(self, scan_time: str, hit_bottom: bool, note: str) -> ScanResult:
        if not hit_bottom:
            self._missing_counts.clear()
        return ScanResult(scan_time, len(self._seen_ids), self._online, self._offline,
                          self.db.export_rows(), self.db.get_recent_events(limit=300),
                          note, complete=hit_bottom)

    def _process_entry(self, entry, rect, now: str):
        # 昵称相似度不足以证明身份；保留不同昵称，供 UID 或人工确认。
        if entry.confidence < CONFIG.ocr_confidence or entry.nickname.startswith('未识别好友@'):
            return None
        row = self.db.find_by_nickname(entry.nickname)
        if row is None:
            row, _ = self.db.find_friend_by_alias(entry.nickname)
        if row is not None:
            fid = row['id']
            self.db.update_status(fid, entry.status, row['nickname'], entry.level,
                                  now, entry.location, entry.last_seen)
        else:
            fid = self.db.add_friend(None, entry.nickname, entry.level, None,
                                     entry.status, now, entry.location, entry.last_seen)
            self._say('录入扫描档案（UID 待补录）: %s' % entry.nickname)
        self._seen_ids.add(fid)
        self._seen_statuses[fid] = entry.status
        self._online = sum(status == 'online' for status in self._seen_statuses.values())
        self._offline = sum(status == 'offline' for status in self._seen_statuses.values())
        return fid

    # ------------------------------------------------------------------
    # 手动识别全部 UID（独立线程，仅手动触发）
    # ------------------------------------------------------------------
    def start_uid_scan(self) -> bool:
        with self._lifecycle_lock:
            if self.is_uid_scanning:
                return False
            # 保持旧线程的停止事件为 set，新任务使用自己的事件。
            self._stop.set()
            self.db.configure_monitoring(None)
            self._pause.clear()
            self._uid_stop = threading.Event()
            previous = self._thread
            self._uid_thread = threading.Thread(target=self._run_uid_scan, args=(previous,),
                                                daemon=True, name='uid-scan')
            self._uid_thread.start()
        return True

    def _run_uid_scan(self, previous=None) -> None:
        self._worker_state.stop_event = self._uid_stop
        try:
            if previous is not None:
                while previous.is_alive():
                    if self._active_stop().is_set():
                        return
                    previous.join(0.05)
            if self._active_stop().is_set():
                return
            self.db.configure_monitoring(None)
            self._esc_held = False
            self.db.begin_observation_session()
            if not self._startup_countdown():
                return
            summary = self.scan_uids_once()
            note = '%s：共尝试 %d 位，成功 %d 位' % (
                'UID 识别结束' if summary['complete'] else 'UID 识别已中断',
                summary['total'], summary['success'])
            if summary.get('message'):
                note += '；' + summary['message']
            self._say(note)
            if self.on_result and summary.get('result') is not None:
                self.on_result(summary['result'])
            elif self.on_result:
                counts = self.db.get_status_summary()
                self.on_result(ScanResult(now_str(), counts['total'], counts['online'], counts['offline'],
                                          self.db.export_rows(), self.db.get_recent_events(limit=300),
                                          note, summary['complete'], 'uid'))
        except Exception as e:
            logger.exception('UID 识别出错')
            self._say('UID 识别出错: %s' % e)
        finally:
            self._reset_history()

    @retained_scan_round
    def scan_uids_once(self) -> dict:
        summary = {'total': 0, 'success': 0, 'complete': False}
        if not self._check_pause():
            return summary
        rect = self._get_rect()
        self._rect = rect
        if self._hwnd is not None:
            problem = window.input_permission_error(self._hwnd)
            if problem:
                self._say(problem)
                summary['message'] = problem
                return summary
        if not self._wait_for_game_foreground(activate=True):
            summary['message'] = self._navigation_error
            return summary
        if not self._ensure_friend_list(rect):
            summary['message'] = self._navigation_error or '未能确认好友列表'
            return summary
        self._confirm_account(self._get_rect())
        if CONFIG.fast_scan or self.interface_mode == 'qianxing':
            from app.scan_workflow import ScanWorkflow
            workflow = ScanWorkflow(self, force_uid=True)
            result = workflow.run()
            return {'total': (getattr(workflow.last, 'expected', None) or 0),
                    'success': len(getattr(workflow.last, 'uids', {})),
                    'complete': result.complete, 'message': result.message, 'result': result}
        rect = self._get_rect()
        if not self._scroll_to_top(rect):
            return summary
        previous = None
        attempted = set()
        self._say('开始识别全部好友 UID……')
        for _ in range(CONFIG.max_scrolls):
            if not self._check_pause():
                break
            if not self._wait_for_game_foreground():
                summary['message'] = self._navigation_error
                break
            rect = self._get_rect()
            img = capture.capture_window(self._hwnd, *rect)
            if img is None:
                break
            self._verify_account_frame(img)
            page_result = friend_screen.read_page(img, left_offset=rect[0], top_offset=rect[1],
                                                  should_continue=self._check_pause)
            if not page_result.confirmed:
                self._say('UID 扫描离开好友界面，已停止：' + '；'.join(page_result.issues))
                break
            if previous is not None and capture.image_similarity(img, previous) >= CONFIG.bottom_similarity:
                summary['complete'] = page_result.reliable
                break
            previous = img
            entries = page_result.entries
            for entry in entries:
                if not self._check_pause():
                    break
                if entry.nickname in attempted:
                    continue
                attempted.add(entry.nickname)
                summary['total'] += 1
                uid = self._read_uid_via_menu(entry, rect)
                if uid:
                    self._bind_uid_result(uid, entry.nickname)
                    summary['success'] += 1
                    self._say('识别 UID: %s -> %s' % (entry.nickname, uid))
                if not self._wait(CONFIG.uid_scan_interval_seconds):
                    break
            if not self._check_pause():
                break
            if not self._wait_for_game_foreground():
                summary['message'] = self._navigation_error
                break
            rect = self._get_rect()
            if scroller.scroll_down(rect, should_stop=self._should_stop, hwnd=self._hwnd) is False:
                break
        return summary

    def _bind_uid_result(self, uid: str, nickname: str) -> int:
        return self.db.resolve_uid(uid, nickname, now_str())

    def _read_uid_via_menu(self, entry, rect) -> Optional[str]:
        if not self._check_pause():
            return None
        opened = False
        try:
            input.click_window(self._hwnd, entry.click_x + CONFIG.avatar_click_offset_x, entry.click_y)
            opened = True
            if not self._wait(CONFIG.menu_wait_seconds):
                return None
            img = capture.capture_window(self._hwnd, *rect)
            if img is None:
                return None
            self._verify_account_frame(img)
            results = ocr_engine.recognize(img)
            target = next((r for r in results if any(word and word in r.text
                           for word in CONFIG.view_profile_words)), None)
            if target is None or not self._check_pause():
                return None
            input.click_window(self._hwnd, int(target.center[0]) + rect[0], int(target.center[1]) + rect[1])
            if not self._wait(CONFIG.profile_wait_seconds):
                return None
            img = capture.capture_window(self._hwnd, *rect)
            if img is None:
                return None
            self._verify_account_frame(img)
            return parser.extract_uid_from_results(ocr_engine.recognize(img), img.shape[1], img.shape[0])
        finally:
            if opened:
                # 取消或识别异常也必须关闭资料卡，之后才能交接下一次扫描。
                for _ in range(2):
                    input.press_esc()
                    time.sleep(max(0, CONFIG.profile_close_seconds))

    def _detect_removed(self, now: str) -> None:
        """只由核对过总数的完整扫描调用；连续缺失才登记事件。"""
        confirmed = set()
        for fid in self._seen_ids_prev - self._seen_ids:
            self._missing_counts[fid] = self._missing_counts.get(fid, 0) + 1
            if self._missing_counts[fid] >= max(2, CONFIG.removal_confirmations):
                self.db.mark_removed(fid, now)
                confirmed.add(fid)
        for fid in self._seen_ids | confirmed:
            self._missing_counts.pop(fid, None)
        self._seen_ids_prev = (self._seen_ids_prev | self._seen_ids) - confirmed

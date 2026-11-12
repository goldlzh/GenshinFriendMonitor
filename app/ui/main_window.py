"""主窗口 UI：好友状态统计、好友信息表格、状态变动、导出与日志交互。"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from typing import List, Optional

from PySide6.QtCore import Qt, QObject, QTimer, Signal, QItemSelectionModel, QSize
from PySide6.QtGui import QColor, QFont, QPixmap, QIcon, QKeySequence, QShortcut, QImage
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app import excel_export, mysql_export, friend_screen, window as win32window
from app.version import APP_NAME
from app.config import CONFIG, parse_scan_frequency
from app.accounts import AccountStore
from app.database import Database
from app.logger import logger
from app.scanner import ScanResult, Scanner
from app.status_events import STATUS_CHANGE_LABELS, is_status_change
from app.ui.region_selector import RegionSelector
from app.ui.profile_import_dialog import ProfileImportDialog

_EVENT_TEXT = STATUS_CHANGE_LABELS

_FRIEND_HEADERS = [
    ('uid', 'UID'), ('nickname', '昵称'), ('level', '冒险等级'),
    ('qianxing_level', '千星等级'), ('tags', '标签'), ('signature', '签名'), ('status', '当前状态'),
    ('qianxing_activity', '游玩奇域'), ('qianxing_activity_summary', '奇域游玩时间统计'),
    ('qianxing_activity_ranges', '奇域游玩时段'),
    ('online_duration', '本次在线时长'), ('cumulative_duration', '累计在线时长'),
    ('today_ranges', '今日上线时段'), ('status_updated_at', '最近状态变化'),
    ('last_seen', '最近登录'), ('profile', '主页'),
]
_FRIEND_COL = {key: index for index, (key, _) in enumerate(_FRIEND_HEADERS)}


def _trim_preview(pixmap):
    """Remove only confident outer horizontal whitespace from a display copy."""
    import numpy as np

    image = pixmap.toImage().convertToFormat(QImage.Format_RGBA8888)
    pixels = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(
            image.height(), image.width(), 4)
    if np.any(pixels[:, :, 3] < 250):
        return pixmap
    rgb = pixels[:, :, :3].astype(np.int16)
    # A row-wise background tolerates the card's gentle vertical gradient.
    background = np.median(rgb, axis=1, keepdims=True)
    foreground = np.max(np.abs(rgb-background), axis=2) > 20
    count = np.count_nonzero(foreground)
    if count < 12 or count > foreground.size*.35:
        return pixmap  # Blank, tiny/uncertain marks, or a decorated background.
    xs = np.where(foreground)[1]
    left, right = max(0, int(xs.min())-6), min(image.width(), int(xs.max())+7)
    if right-left >= image.width()*.9:
        return pixmap
    # Keep all vertical pixels and internal spacing, including detached marks.
    return pixmap.copy(left, 0, right-left, image.height())


def nickname_pixmap(data, width=260, height=38, *, trim=False):
    """Render a bounded preview; original bytes and raw-view behavior stay intact."""
    pixmap = QPixmap()
    if not data or not pixmap.loadFromData(data):
        return pixmap
    if trim:
        pixmap = _trim_preview(pixmap)
        width, height = min(width, pixmap.width()), min(height, pixmap.height())
    return pixmap.scaled(QSize(width, height), Qt.KeepAspectRatio, Qt.SmoothTransformation)


class ScannerBridge(QObject):
    """跨线程信号桥：扫描线程 → 主线程 UI。"""
    result = Signal(object)   # ScanResult
    message = Signal(str)     # 状态消息
    scoped_result = Signal(object)
    scoped_message = Signal(object)
    account_identity = Signal(object)


def _fmt_duration(secs) -> str:
    secs = int(secs or 0)
    if secs <= 0:
        return "0秒"
    m, s = divmod(secs, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}小时{m}分{s}秒"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(760, 480)
        self.resize(1040, 700)

        self._initial_db_path = CONFIG.db_path
        self.account_store = AccountStore(legacy_path=CONFIG.db_path)
        self.db = Database()
        self._initialized_accounts = set()
        self._account_reading = False
        self._profile_importing = False
        self._account_uid = getattr(self.db, 'account_uid', None)
        if self._account_uid or self.db._path == ':memory:':
            self.db.reset_runtime_state()
            self._initialized_accounts.add(self._account_uid)
        self.bridge = ScannerBridge()
        self.scanner = self._make_scanner(self.db)
        self.bridge.scoped_result.connect(self._dispatch_result)
        self.bridge.scoped_message.connect(self._dispatch_message)
        self.bridge.account_identity.connect(self._on_account_identity)
        self.bridge.result.connect(self._on_result)
        self.bridge.message.connect(self._on_message)

        self._manual_rect = None          # 手动框选的扫描区域 (left, top, right, bottom)
        self.scan_started_at = None       # 最近一次成功点击“开始”，重试不累计空闲时间
        self._region_selector = None      # 框选覆盖层实例

        self._build_ui()
        self._refresh_account_choices(self._account_uid)

        # 每秒刷新在线好友的实时上线时长
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_durations)
        self._timer.start(1000)

        self._load_friends_table()
        self._load_events_table()
        self._load_dedup_table()
        self._update_stats()
        saved = self.db.get_complete_roster()
        if saved:
            rows = saved['rows']
            confirmed = sum(bool(r.get('uid')) for r in rows)
            pending = sum(bool(r.get('nickname_pending')) for r in rows)
            note = '已加载完整名单：%d 位好友，UID %d/%d' % (len(rows), confirmed, len(rows))
            if pending:
                note += '；%d 个昵称显示截图，待人工核查（双击昵称编辑）' % pending
            self.phase_label.setText(note)
        if not self._account_uid and self.db._path != ':memory:':
            self.phase_label.setText('旧数据未归属账号，可浏览。请先识别当前账号，再选择扫描或导入旧名单。')
        self._sync_task_controls()

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        root.addWidget(self._build_control_panel())
        root.addWidget(self._build_stats_panel())
        self.phase_label = QLabel('等待快速扫描')
        self.phase_label.setWordWrap(True)
        self.phase_label.setStyleSheet('color: #28578a; padding: 4px;')
        root.addWidget(self.phase_label)
        self.uid_notice = QLabel()
        self.uid_notice.setWordWrap(True)
        self.uid_notice.setStyleSheet('color: #9a6700; padding: 4px;')
        root.addWidget(self.uid_notice)

        # 标签页：好友列表 / 状态变动 / 去重记录
        self.tabs = QTabWidget()
        self.friends_table = self._make_table([label for _, label in _FRIEND_HEADERS])
        # 好友表格支持多选 + 右键手动修正
        self.friends_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.friends_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.friends_table.customContextMenuRequested.connect(self._show_friend_menu)
        self.friends_table.cellDoubleClicked.connect(
            lambda row, column: self._edit_friend(row) if column in (_FRIEND_COL['nickname'], _FRIEND_COL['signature']) else None)
        self.friends_table.setIconSize(QSize(260, 38))
        self.friends_table.setWordWrap(False)
        # Keep columns readable on a narrow monitor; additional history scrolls
        # horizontally instead of squeezing both levels and tags into tiny cells.
        self.friends_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.friends_table.horizontalHeader().setMinimumSectionSize(44)
        self.friends_table.horizontalHeader().setTextElideMode(Qt.ElideRight)
        self._auto_friend_column_widths = {}
        widths = dict(uid=100, nickname=280, level=80, qianxing_level=80, tags=140,
                      signature=260, qianxing_activity=190, qianxing_activity_summary=230,
                      qianxing_activity_ranges=260,
                      status=110, online_duration=110, cumulative_duration=110,
                      today_ranges=150, status_updated_at=155, last_seen=115, profile=86)
        for key, width in widths.items():
            self.friends_table.setColumnWidth(_FRIEND_COL[key], width)
        for column, (_, label) in enumerate(_FRIEND_HEADERS):
            self.friends_table.horizontalHeaderItem(column).setToolTip(label)
        self.friends_table.horizontalHeaderItem(_FRIEND_COL['level']).setToolTip('普通好友界面的冒险等级')
        self.friends_table.horizontalHeaderItem(_FRIEND_COL['qianxing_level']).setToolTip('千星奇域界面的千星等级，与冒险等级分别保存')
        self.friends_table.horizontalHeaderItem(_FRIEND_COL['tags']).setToolTip('千星奇域好友标签；完整内容也可在编辑窗口查看')
        self.friends_table.horizontalHeaderItem(_FRIEND_COL['qianxing_activity_summary']).setToolTip('千星好友界面橙色活动的累计统计，包含编辑内容中；只有设定扫描频率才计时。')
        self.friends_table.horizontalHeaderItem(_FRIEND_COL['qianxing_activity_ranges']).setToolTip('今日各奇域的游玩时段；跨午夜按日期划分，到期或停止时结束。')
        self.events_table = self._make_table(["时间", "UID", "昵称", "事件"])
        self.dedup_table = self._make_table(["时间", "被合并昵称", "保留昵称", "相似度", "状态"])
        self.dedup_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.dedup_table.customContextMenuRequested.connect(self._show_dedup_menu)

        # 好友列表工具栏：昵称/UID 检索和独立的主页录入。
        friend_tab = QWidget()
        fl = QVBoxLayout(friend_tab)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(4)
        toolbar = QHBoxLayout()
        toolbar.setSpacing(4)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("输入昵称或 UID（支持部分匹配，空格分隔多个），回车选中")
        self.search_edit.returnPressed.connect(self._select_matching_friends)
        self.search_btn = QPushButton("选中匹配")
        self.search_btn.clicked.connect(self._select_matching_friends)
        self.sel_all_btn = QPushButton("全选")
        self.sel_all_btn.clicked.connect(self._select_all_friends)
        self.del_sel_btn = QPushButton("删除选中")
        self.del_sel_btn.clicked.connect(self._delete_selected_friends)
        self.del_all_btn = QPushButton("一键全部删除")
        self.del_all_btn.clicked.connect(self._delete_all_friends)
        toolbar.addWidget(QLabel("昵称 / UID:"))
        toolbar.addWidget(self.search_edit, stretch=1)
        toolbar.addWidget(self.search_btn)
        fl.addLayout(toolbar)
        actions = QHBoxLayout()
        self.show_history = QCheckBox('显示历史档案')
        self.show_history.setToolTip('查看已消失好友、历史 OCR 草稿及手动录入资料；确认的减少事件见“状态变动记录”')
        self.show_history.toggled.connect(self._load_friends_table)
        self.paste_profile_btn = QPushButton('粘贴主页录入')
        self.paste_profile_btn.setToolTip('复制好友主页截图后点击，或在好友表格按 Ctrl+V；核对资料后保存到当前账号')
        self.paste_profile_btn.clicked.connect(self._paste_friend_profile)
        self.profile_paste_shortcut = QShortcut(QKeySequence.Paste, self.friends_table)
        self.profile_paste_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self.profile_paste_shortcut.activated.connect(self._paste_friend_profile)
        actions.addWidget(self.show_history)
        actions.addWidget(self.paste_profile_btn)
        actions.addStretch()
        actions.addWidget(self.sel_all_btn)
        actions.addWidget(self.del_sel_btn)
        actions.addWidget(self.del_all_btn)
        fl.addLayout(actions)
        fl.addWidget(self.friends_table, stretch=1)

        self.tabs.addTab(friend_tab, "好友列表")
        self.tabs.addTab(self.events_table, "状态变动记录")
        self.tabs.addTab(self.dedup_table, "去重记录")
        root.addWidget(self.tabs, stretch=1)

        # 日志输出
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        self.log_view.setMaximumHeight(90)
        root.addWidget(self.log_view)

    def _build_control_panel(self) -> QWidget:
        box = QGroupBox("控制")
        grid = QGridLayout(box)
        grid.setContentsMargins(6, 6, 6, 6)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)

        # 第一行：定位 + 扫描控制
        grid.addWidget(QLabel("窗口关键字:"), 0, 0)
        self.keyword_edit = self._make_line_edit(CONFIG.window_title_keyword)
        grid.addWidget(self.keyword_edit, 0, 1)

        self.find_btn = QPushButton("查找窗口")
        self.find_btn.clicked.connect(self._find_windows)
        grid.addWidget(self.find_btn, 0, 2)

        self.window_combo = QComboBox()
        self.window_combo.setMinimumWidth(180)
        grid.addWidget(self.window_combo, 0, 3)

        self.select_region_btn = QPushButton("手动框选")
        self.select_region_btn.setToolTip("框选完整的 16:9 游戏画面，包含四个角；此模式请先自行打开好友界面")
        self.select_region_btn.clicked.connect(self._select_region)
        grid.addWidget(self.select_region_btn, 0, 4)

        self.start_btn = QPushButton("快速扫描")
        self.start_btn.setToolTip("按昵称快速扫描；同名、无法匹配或疑似改名时单独核验 UID，再继续扫描")
        self.start_btn.clicked.connect(self._toggle_scan)
        grid.addWidget(self.start_btn, 0, 5)

        self.pause_btn = QPushButton("暂停")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._toggle_pause)
        grid.addWidget(self.pause_btn, 0, 6)

        self.uid_btn = QPushButton("初始化档案")
        self.uid_btn.setToolTip("首次完整 UID 建档；也可补全或重新核验所有好友 UID，耗时较长。不会清空已有档案。")
        self.uid_btn.clicked.connect(self._toggle_uid_scan)
        grid.addWidget(self.uid_btn, 0, 7)

        # 第二行：导出 / 日志 / 状态提示
        self.export_btn = QPushButton("导出 Excel")
        self.export_btn.clicked.connect(self._export_excel)
        grid.addWidget(self.export_btn, 1, 0)

        self.mysql_btn = QPushButton("导出 MySQL")
        self.mysql_btn.clicked.connect(self._export_mysql)
        grid.addWidget(self.mysql_btn, 1, 1)

        self.logdir_btn = QPushButton("打开日志目录")
        self.logdir_btn.clicked.connect(self._open_log_dir)
        grid.addWidget(self.logdir_btn, 1, 2)

        self.faillog_btn = QPushButton("打开识别失败日志")
        self.faillog_btn.clicked.connect(self._open_fail_log)
        grid.addWidget(self.faillog_btn, 1, 3)

        self.region_label = QLabel("扫描区域：未设置（请查找窗口或手动框选）")
        self.region_label.setStyleSheet("color: #555555;")
        self.region_label.setWordWrap(True)
        grid.addWidget(self.region_label, 1, 4, 1, 3)

        grid.addWidget(QLabel('账号:'), 2, 0)
        self.account_combo = QComboBox()
        self.account_combo.setToolTip('每个账号分别保存好友名单；扫描期间不可切换')
        self.account_combo.currentIndexChanged.connect(self._select_account)
        grid.addWidget(self.account_combo, 2, 1, 1, 2)
        self.detect_account_btn = QPushButton('识别当前账号')
        self.detect_account_btn.setToolTip('读取游戏画面右下角 UID，并选择对应账号；不会开始扫描')
        self.detect_account_btn.clicked.connect(self._detect_account)
        grid.addWidget(self.detect_account_btn, 2, 3)
        self.import_legacy_btn = QPushButton('导入旧名单')
        self.import_legacy_btn.setToolTip('将原来的未归属名单复制到当前空账号，保留原数据库')
        self.import_legacy_btn.clicked.connect(self._import_legacy)
        grid.addWidget(self.import_legacy_btn, 2, 4)
        self.account_status_label = QLabel('请识别游戏右下角 UID')
        self.account_status_label.setWordWrap(True)
        grid.addWidget(self.account_status_label, 2, 5, 1, 3)

        grid.addWidget(QLabel('频率（分钟）:'), 3, 0)
        frequency = CONFIG.scan_frequency_minutes
        self.frequency_edit = QLineEdit('' if frequency is None else str(frequency))
        self.frequency_edit.setPlaceholderText('留空为单次，0 为连续')
        self.frequency_edit.setToolTip('支持非负小数；留空只运行一轮，0 连续运行，正数按分钟重复')
        grid.addWidget(self.frequency_edit, 3, 1, 1, 2)
        hint = '留空：扫描一次　0：连续扫描　正数：按分钟重复'
        self.frequency_hint_label = QLabel(hint)
        self.frequency_hint_label.setWordWrap(True)
        grid.addWidget(self.frequency_hint_label, 3, 3, 1, 5)
        self.online_only_checkbox = QCheckBox('只扫描在线好友')
        self.online_only_checkbox.setChecked(bool(CONFIG.online_only))
        self.online_only_checkbox.setToolTip('快速扫描仅采集当前在线的好友')
        grid.addWidget(self.online_only_checkbox, 4, 0, 1, 4)

        self.save_profile_checkbox = QCheckBox('保存好友主页截图')
        self.save_profile_checkbox.setChecked(bool(CONFIG.save_profile_screenshots))
        self.save_profile_checkbox.setToolTip('仅控制好友主页存档；无法识别的昵称和签名截图始终保存。关闭不会删除已有截图。')
        self.save_profile_checkbox.toggled.connect(self._set_profile_saving)
        grid.addWidget(self.save_profile_checkbox, 4, 4, 1, 2)
        self.profile_space_label = QLabel('约 148 KiB/张；150 人各两张约 44 MiB')
        self.profile_space_label.setWordWrap(True)
        self.profile_space_label.setToolTip('按现有书本截图估算，仅含图片；数据库、待校准字段图、日志和备份另计。')
        grid.addWidget(self.profile_space_label, 4, 6, 1, 2)
        self.aspect_label = QLabel('扫描要求：完整的 16:9 游戏画面（不含标题栏），例如 1920×1080 或 1280×720。')
        self.aspect_label.setWordWrap(True)
        self.aspect_label.setStyleSheet('color: #995400; font-weight: bold;')
        grid.addWidget(self.aspect_label, 5, 0, 1, 8)

        grid.setColumnStretch(1, 1)   # 关键字输入框可伸展
        grid.setColumnStretch(3, 1)   # 窗口下拉框可伸展
        return box

    def _set_profile_saving(self, enabled):
        CONFIG.save_profile_screenshots = bool(enabled)
        original_path = CONFIG.db_path
        try:
            CONFIG.db_path = self._initial_db_path
            CONFIG.save()
        except OSError as error:
            self._append_log('保存主页截图选项失败：'+str(error))
        finally:
            CONFIG.db_path = original_path

    def _validate_game_view(self, rect=None):
        if rect is None:
            rect = self._manual_rect
            if rect is None and self.window_combo.currentData() is not None:
                rect = win32window.get_window_rect(self.window_combo.currentData())
        problem = friend_screen.game_window_problem(rect)
        if problem:
            QMessageBox.warning(self, '需要 16:9 游戏窗口', problem)
            return False
        return True

    def _build_stats_panel(self) -> QWidget:
        box = QGroupBox("本次启动扫描统计")
        lay = QHBoxLayout(box)

        self.lbl_total = QLabel("总好友: 0")
        self.lbl_online = QLabel("在线: 0")
        self.lbl_offline = QLabel("离线: 0")
        self.lbl_unknown = QLabel("未知: 0")
        self.lbl_events = QLabel("变动: 0 次")
        for lbl, color in (
            (self.lbl_total, "#333333"),
            (self.lbl_online, "#1a7f37"),
            (self.lbl_offline, "#9e9e9e"),
            (self.lbl_unknown, "#9e9e9e"),
            (self.lbl_events, "#b35900"),
        ):
            lbl.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {color};")
            lbl.setMinimumWidth(120)
            lay.addWidget(lbl)
        lay.addStretch(1)
        return box

    def _make_table(self, headers: List[str]) -> QTableWidget:
        t = QTableWidget()
        t.setColumnCount(len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectRows)
        t.setAlternatingRowColors(True)
        return t

    def _make_line_edit(self, text: str) -> QWidget:
        from PySide6.QtWidgets import QLineEdit
        e = QLineEdit(text)
        return e

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _make_scanner(self, db: Database) -> Scanner:
        # Queued notifications from an old account must never refresh the new one.
        return Scanner(db,
                       on_result=lambda result: self.bridge.scoped_result.emit((db, result)),
                       on_message=lambda message: self.bridge.scoped_message.emit((db, message)))

    def _dispatch_result(self, payload) -> None:
        db, result = payload
        if db is self.db and not getattr(self, '_closing', False):
            self.bridge.result.emit(result)

    def _dispatch_message(self, payload) -> None:
        db, message = payload
        if db is self.db and not getattr(self, '_closing', False):
            self.bridge.message.emit(message)

    def _task_busy(self) -> bool:
        return (self.scanner.is_running or self.scanner.is_uid_scanning
                or self._account_reading or self._profile_importing)

    def _sync_task_controls(self) -> None:
        busy = self._task_busy()
        self.account_combo.setEnabled(not busy)
        self.detect_account_btn.setEnabled(not busy)
        self.import_legacy_btn.setEnabled(not busy and bool(self._account_uid))
        self.window_combo.setEnabled(not busy)
        self.find_btn.setEnabled(not busy)
        self.select_region_btn.setEnabled(not busy)
        self.frequency_edit.setEnabled(not busy)
        self.online_only_checkbox.setEnabled(not busy)
        self.save_profile_checkbox.setEnabled(not busy)
        allowed = bool(self._account_uid) or self.db._path == ':memory:'
        self.start_btn.setEnabled(allowed and not self._account_reading and not self.scanner.is_uid_scanning
                                 and not self._profile_importing)
        self.uid_btn.setEnabled(allowed and not self._account_reading and not self._profile_importing)
        self.paste_profile_btn.setEnabled(bool(self._account_uid) and not busy)
        self.profile_paste_shortcut.setEnabled(bool(self._account_uid) and not busy)

    def _refresh_account_choices(self, selected_uid=None) -> None:
        self.account_combo.blockSignals(True)
        self.account_combo.clear()
        self.account_combo.addItem('旧数据（未归属）', None)
        accounts = self.account_store.list_accounts()
        if selected_uid and selected_uid not in accounts:
            accounts.append(selected_uid)
        for uid in sorted(accounts):
            self.account_combo.addItem(uid, uid)
        self.account_combo.setCurrentIndex(max(0, self.account_combo.findData(selected_uid)))
        self.account_combo.blockSignals(False)
        self.account_status_label.setText('当前名单：'+selected_uid if selected_uid else '旧名单仅浏览，请先识别账号')

    def _activate_database(self, db: Database, uid) -> None:
        if uid and uid not in self._initialized_accounts:
            try:
                db.reset_runtime_state()
            except Exception:
                db.close()
                raise
            self._initialized_accounts.add(uid)
        old_db = self.db
        self.scanner.stop()
        self.db = db
        self._account_uid = uid
        CONFIG.db_path = str(db._path)
        self.scanner = self._make_scanner(db)
        if self._manual_rect is not None:
            self.scanner.set_manual_rect(self._manual_rect)
        elif self.window_combo.currentData() is not None:
            self.scanner.set_window(self.window_combo.currentData())
        self.scan_started_at = None
        if old_db is not db:
            old_db.close()
        self._refresh_account_choices(uid)
        self._load_friends_table()
        self._load_events_table()
        self._load_dedup_table()
        self._update_stats()
        self.phase_label.setText(f'已选择账号 {uid}，等待手动点击「快速扫描」' if uid else '旧数据未归属账号，仅供浏览和导入')
        self._sync_task_controls()

    def _select_account(self, index: int) -> None:
        uid = self.account_combo.itemData(index)
        if uid == self._account_uid:
            return
        if self._task_busy():
            self._refresh_account_choices(self._account_uid)
            return
        try:
            db = (self.account_store.open_account(uid) if uid
                  else Database(str(self.account_store.legacy_path)))
            self._activate_database(db, uid)
        except Exception as error:
            self._refresh_account_choices(self._account_uid)
            QMessageBox.warning(self, '无法切换账号', str(error))

    def _detect_account(self) -> None:
        if self._task_busy():
            return
        hwnd = self.window_combo.currentData()
        rect = self._manual_rect
        if rect is None and hwnd is not None:
            rect = win32window.get_window_rect(hwnd)
        if not rect:
            QMessageBox.warning(self, '识别账号', '请先查找游戏窗口或手动框选完整游戏画面，确保右下角 UID 可见。')
            return
        if not self._validate_game_view(rect):
            return
        self._account_reading = True
        self._sync_task_controls()
        self.account_status_label.setText('正在读取游戏右下角 UID…')
        self._append_log('正在识别当前账号，请保持游戏右下角 UID 可见')

        def read_identity():
            try:
                from app.account_identity import read_current_account
                result = (read_current_account(rect), None)
            except Exception as error:
                result = (None, str(error))
            self.bridge.account_identity.emit(result)

        threading.Thread(target=read_identity, name='account-identity', daemon=True).start()

    def _on_account_identity(self, result) -> None:
        self._account_reading = False
        if getattr(self, '_closing', False):
            return
        uid, error = result
        if error:
            self._sync_task_controls()
            self.account_status_label.setText('账号识别失败，请确保 UID 清晰可见')
            QMessageBox.warning(self, '账号识别失败', error)
            return
        previous = self._account_uid
        try:
            if uid != previous:
                self._activate_database(self.account_store.open_account(uid), uid)
            else:
                self._sync_task_controls()
                self.account_status_label.setText(f'已核对游戏账号：{uid}')
            self._append_log(f'已识别游戏账号 {uid}，请手动点击「快速扫描」')
            if previous and previous != uid:
                QMessageBox.information(self, '已切换账号', f'游戏当前账号为 {uid}，与之前选择的 {previous} 不同。\n已切换到账号 {uid} 的独立好友名单。')
        except Exception as error:
            self._sync_task_controls()
            QMessageBox.warning(self, '无法载入账号', str(error))

    def _import_legacy(self) -> None:
        uid = self._account_uid
        if self._task_busy() or not uid:
            return
        if QMessageBox.question(
                self, '确认旧名单归属',
                f'请确认旧名单属于账号 {uid}。\n仅可导入尚无好友记录的账号；导入会保留原数据库。\n是否将旧名单复制到该账号？',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self.scanner.stop()
        self.db.close()
        try:
            imported = self.account_store.import_legacy(uid)
        except Exception as error:
            self._activate_database(self.account_store.open_account(uid), uid)
            QMessageBox.warning(self, '无法导入旧名单', str(error))
            return
        self._initialized_accounts.discard(uid)
        self._activate_database(imported, uid)
        self._append_log(f'已将旧名单导入账号 {uid}，原数据库保留')

    def _require_account(self) -> bool:
        if self._account_uid or self.db._path == ':memory:':
            return True
        QMessageBox.warning(self, '请先选择账号', '请点击「识别当前账号」读取游戏右下角 UID，再选择该账号点击「快速扫描」。旧数据不会自动归属新账号。')
        return False

    def _find_windows(self) -> None:
        if self.scanner.is_running or self.scanner.is_uid_scanning:
            QMessageBox.warning(self, "提示", "请先停止当前任务，再切换窗口")
            return
        keyword = self.keyword_edit.text().strip()
        if not keyword:
            QMessageBox.warning(self, "提示", "请输入窗口标题关键字")
            return
        CONFIG.window_title_keyword = keyword
        active_path = CONFIG.db_path
        try:
            CONFIG.db_path = str(self.account_store.legacy_path)
            CONFIG.save()
        except OSError as e:
            self._append_log(f"保存窗口关键字失败: {e}")
        finally:
            CONFIG.db_path = active_path
        wins = win32window.find_windows(keyword)
        self.window_combo.clear()
        if not wins:
            self._append_log(f"未找到标题包含「{keyword}」的窗口")
            QMessageBox.information(self, "结果", f"未找到标题包含「{keyword}」的窗口，请确认游戏已打开。")
            return
        self._manual_rect = None
        self.scanner.clear_manual_rect()
        for hwnd, title, rect in wins:
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            self.window_combo.addItem(f"{title}  尺寸 {w}x{h}  (hwnd={hwnd})", hwnd)
        self._append_log(f"找到 {len(wins)} 个匹配窗口")
        self.region_label.setText(
            f"已找到 {len(wins)} 个窗口，请在下拉框中选择目标（或手动框选）"
        )

    def _select_region(self) -> None:
        """弹出全屏覆盖层，框选完整的 16:9 游戏画面。"""
        if self.scanner.is_running or self.scanner.is_uid_scanning:
            QMessageBox.warning(self, "提示", "请先停止当前任务，再重新框选区域")
            return
        if self._region_selector is not None:
            return
        self.hide()  # 隐藏主窗口，避免遮挡目标
        self._region_selector = RegionSelector()
        self._region_selector.selected.connect(self._on_region_selected)
        self._region_selector.canceled.connect(self._on_region_canceled)
        self._region_selector.show()
        self._region_selector.raise_()
        self._region_selector.activateWindow()

    def _on_region_selected(self, left: int, top: int, right: int, bottom: int) -> None:
        self._manual_rect = (left, top, right, bottom)
        self.scanner.set_manual_rect(self._manual_rect)
        self.region_label.setText(
            f"扫描区域（手动框选）：({left},{top})-({right},{bottom}) "
            f"尺寸 {right - left}x{bottom - top}"
        )
        self._append_log(
            f"已框选扫描区域 ({left},{top})-({right},{bottom})，"
            f"尺寸 {right - left}x{bottom - top}"
        )
        self._cleanup_selector()
        self.show()

    def _on_region_canceled(self) -> None:
        self._append_log("已取消手动框选")
        self._cleanup_selector()
        self.show()

    def _cleanup_selector(self) -> None:
        if self._region_selector is not None:
            self._region_selector.deleteLater()
            self._region_selector = None

    def _yield_to_game(self) -> None:
        """仍持有用户按钮焦点时交接给游戏，默认保留监控窗口。"""
        # 最小化之后再等工作线程的倒计时结束，可能已经失去 Windows
        # 允许切换前台窗口的条件。正常激活请求应在按钮处理期间立即发出。
        # 工作线程仍会独立核验前台状态，激活失败不会发送游戏输入。
        hwnd = self.scanner._hwnd
        if hwnd is not None and CONFIG.bring_to_front:
            if win32window.bring_to_front(hwnd):
                self._append_log('已将焦点交给游戏，准备倒计时后开始扫描')
            else:
                self._append_log('尚未切回游戏，请在准备期间点击游戏；当前任务会继续等待')
        if CONFIG.minimize_on_start:
            self.showMinimized()

    def _toggle_scan(self) -> None:
        if self.scanner.is_running:
            self.scanner.stop()
            self.start_btn.setText('正在停止…' if self.scanner.is_running else '快速扫描')
            self.pause_btn.setText("暂停")
            self.pause_btn.setEnabled(False)
            self._sync_task_controls()
            return
        if self._account_reading or not self._require_account():
            return
        try:
            frequency = parse_scan_frequency(self.frequency_edit.text())
        except ValueError as error:
            QMessageBox.warning(self, '扫描频率无效', str(error))
            self.frequency_edit.setFocus()
            return
        hwnd = self.window_combo.currentData()
        if hwnd is None and self._manual_rect is None:
            QMessageBox.warning(
                self, "提示", "请先查找窗口，或点击「手动框选窗口」框选扫描区域"
            )
            return
        if self._manual_rect is not None:
            self.scanner.set_manual_rect(self._manual_rect)
        elif hwnd is not None:
            # 最近手动选中的区域优先；重新查找窗口会清除此选择。
            self.scanner.set_window(hwnd)
            self.scanner.clear_manual_rect()
            self._manual_rect = None
            self.region_label.setText(f"扫描区域：窗口 {self.window_combo.currentText()}")
        if not self._validate_game_view():
            return
        CONFIG.scan_frequency_minutes = frequency
        CONFIG.scan_once_only = False
        CONFIG.online_only = self.online_only_checkbox.isChecked()
        requested_at = time.perf_counter()
        if not self.scanner.start():
            return
        self.scan_started_at = requested_at
        self.start_btn.setText("停止扫描")
        self.pause_btn.setEnabled(True)
        self._sync_task_controls()
        self._yield_to_game()

    def _toggle_pause(self) -> None:
        """暂停/继续扫描。扫描中按 ESC 键也可切换暂停。"""
        if not (self.scanner.is_running or self.scanner.is_uid_scanning):
            self.pause_btn.setEnabled(False)
            self.pause_btn.setText("暂停")
            return
        self.scanner.toggle_pause()
        self.pause_btn.setText("继续" if self.scanner.is_paused else "暂停")
        if not self.scanner.is_paused:
            self._yield_to_game()

    def _toggle_uid_scan(self) -> None:
        """初始化或补全档案：点好友头像 → 查看资料 → 读全部好友 UID。"""
        if self.scanner.is_uid_scanning:
            self.scanner.stop()
            self.uid_btn.setEnabled(False)
            return
        if self._account_reading or not self._require_account():
            return
        hwnd = self.window_combo.currentData()
        if hwnd is None and self._manual_rect is None:
            QMessageBox.warning(
                self, "提示", "请先查找窗口，或点击「手动框选窗口」框选扫描区域"
            )
            return
        if not self.scanner.is_running:
            if self._manual_rect is not None:
                self.scanner.set_manual_rect(self._manual_rect)
            elif hwnd is not None:
                self.scanner.set_window(hwnd)
                self.scanner.clear_manual_rect()
                self._manual_rect = None
                self.region_label.setText(f"扫描区域：窗口 {self.window_combo.currentText()}")
        if not self._validate_game_view():
            return
        ret = QMessageBox.question(
            self,
            "确认",
            "用于首次完整建档，也可补全或重新核验已有档案，不会清空已有档案。\n"
            "将滚动好友列表，逐个点击好友头像打开资料卡识别 UID。\n"
            "开始后请在准备期间切回游戏，正式识别时勿操作鼠标/键盘。是否开始？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if ret != QMessageBox.Yes:
            return
        if not self.scanner.start_uid_scan():
            return
        self.uid_btn.setText("停止 UID 识别")
        self.uid_btn.setEnabled(True)
        self.start_btn.setEnabled(False)
        self.select_region_btn.setEnabled(False)
        self.find_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self._sync_task_controls()
        self._yield_to_game()
        # 结束后恢复按钮
        QTimer.singleShot(500, self._check_uid_scan_done)

    def _check_uid_scan_done(self) -> None:
        if getattr(self, "_closing", False):
            return
        if self.scanner.is_uid_scanning:
            QTimer.singleShot(500, self._check_uid_scan_done)
            return
        self.uid_btn.setEnabled(True)
        self.uid_btn.setText("初始化档案")
        self.start_btn.setEnabled(True)
        self.start_btn.setText("快速扫描")
        self.select_region_btn.setEnabled(True)
        self.find_btn.setEnabled(True)
        self._sync_task_controls()
        self._load_friends_table()
        self._update_stats()

    def _export_excel(self) -> None:
        try:
            friends = self.db.export_rows()
            events = self.db.export_events()
            path = excel_export.export_friends_to_excel(friends, events, account_uid=self.db.account_uid)
            self._append_log(f"已导出: {path}")
            QMessageBox.information(self, "导出成功", f"Excel 已保存到:\n{path}")
        except Exception as e:
            logger.error("导出 Excel 失败: %s", e)
            QMessageBox.critical(self, "导出失败", str(e))

    def _export_mysql(self) -> None:
        try:
            friends = self.db.export_rows()
            result = mysql_export.export_to_mysql(friends, account_uid=self.db.account_uid)
            message = f"已同步 {result.written} 条好友数据到 MySQL。"
            if result.skipped:
                message += f"\n跳过 {result.skipped} 条未填写有效 UID 的记录，请先识别或补录 UID。"
            self._append_log(message)
            QMessageBox.information(self, "导出结果", message)
        except Exception as e:
            logger.error("导出 MySQL 失败: %s", e)
            QMessageBox.critical(
                self, "导出失败",
                f"MySQL 导出失败：{e}\n\n请确认已安装 MySQL 并在 config.json 中配置连接信息。")

    def _open_log_dir(self) -> None:
        os.makedirs(CONFIG.log_dir, exist_ok=True)
        os.startfile(CONFIG.log_dir)

    def _open_fail_log(self) -> None:
        path = os.path.join(CONFIG.log_dir, "ocr_fail.log")
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
        os.startfile(path)

    # ------------------------------------------------------------------
    # 扫描回调（主线程槽）
    # ------------------------------------------------------------------
    def _on_result(self, result: ScanResult) -> None:
        if getattr(self, "_closing", False):
            return
        self._load_friends_table()
        self._load_events_table()
        self._load_dedup_table()
        self._update_stats()
        self.phase_label.setText(result.message)
        label = "扫描完成" if result.complete else "扫描未完成"
        self._append_log(result.message or
            f"[{result.scan_time}] {label}：共 {result.total} 位，在线 {result.online}，离线 {result.offline}")

    def _on_message(self, msg: str) -> None:
        self.phase_label.setText(msg)
        self._append_log(msg)

    def _append_log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{stamp}] {msg}")

    # ------------------------------------------------------------------
    # 数据刷新
    # ------------------------------------------------------------------
    def _load_friends_table(self) -> None:
        friends = (self.db.get_all_friends() if self.show_history.isChecked()
                   else self.db.get_display_friends())
        self.friends_table.setRowCount(len(friends))
        self._friends_cache = friends
        columns = _FRIEND_COL
        for row, f in enumerate(friends):
            self.friends_table.setItem(row, columns['uid'], self._friend_item(f.get("uid") or ""))
            name_item = self._friend_item(f.get("nickname") or "")
            if f.get('nickname_pending'):
                pixmap = nickname_pixmap(self.db.get_nickname_image(f['id']), width=200, trim=True)
                name_item.setText('' if not pixmap.isNull() else '待核查（截图不可用）')
                name_item.setIcon(QIcon(pixmap))
                hint = '昵称待人工核查，预览仅裁去外围留白，原始截图及内部空白、特殊字符仍保留。双击可查看或填写昵称。'
                if f.get('identity_review_reason'):
                    hint = f['identity_review_reason']+'。'+hint
                name_item.setToolTip(hint)
                name_item.setBackground(QColor('#fff2d5'))
                self.friends_table.setRowHeight(row, 46)
            else:
                self.friends_table.setRowHeight(row, self.friends_table.verticalHeader().defaultSectionSize())
            self.friends_table.setItem(row, columns['nickname'], name_item)
            self.friends_table.setItem(row, columns['level'], self._friend_item(str(f.get("level") or "")))
            qianxing_level = f.get('qianxing_level')
            self.friends_table.setItem(row, columns['qianxing_level'],
                                       self._friend_item('' if qianxing_level is None else str(qianxing_level)))
            tag_item = self._friend_item(f.get('tags') or '')
            tag_item.setToolTip(f.get('tags') or '')
            self.friends_table.setItem(row, columns['tags'], tag_item)
            signature_item = self._friend_item(f.get('signature') or '')
            if f.get('signature_pending'):
                captures = self.db.get_field_captures(f['id'], 'signature')
                pixmap = nickname_pixmap(captures[0]['image'] if captures else None, trim=True)
                signature_item.setText('' if not pixmap.isNull() else '签名待校准')
                signature_item.setIcon(QIcon(pixmap))
                signature_item.setBackground(QColor('#fff2d5'))
                signature_item.setToolTip('签名识别不确定，已保留最多两张原图；双击查看、人工校准。')
                self.friends_table.setRowHeight(row, 46)
            else:
                signature_item.setToolTip(f.get('signature') or '')
            self.friends_table.setItem(row, columns['signature'], signature_item)
            self._set_activity_cells(row, f)
            self.friends_table.setItem(row, columns['status'], self._status_item(f))
            self.friends_table.setItem(row, columns['online_duration'], self._friend_item(_fmt_duration(self._online_secs(f))))
            self.friends_table.setItem(row, columns['cumulative_duration'], self._friend_item(_fmt_duration(self._cumulative_secs(f))))
            self.friends_table.setItem(row, columns['today_ranges'], self._friend_item(self._today_ranges(f)))
            self.friends_table.setItem(row, columns['status_updated_at'], self._friend_item(f.get("status_updated_at") or ""))
            self.friends_table.setItem(row, columns['last_seen'], self._friend_item(f.get("last_seen") or ""))
            profile_btn = QPushButton('查看主页')
            profile_btn.setToolTip('查看最近保留的两张好友主页截图；未保存截图时显示待采集')
            profile_btn.clicked.connect(lambda checked=False, fid=f['id']: self._show_profile_captures(fid))
            self.friends_table.setCellWidget(row, columns['profile'], profile_btn)
        self._compact_friend_columns()
        self._update_uid_notice()

    def _compact_friend_columns(self) -> None:
        """Fit useful content within caps, retaining widths resized by the user."""
        table = self.friends_table
        metrics = table.fontMetrics()
        caps = dict(uid=110, nickname=220, level=88, qianxing_level=88, tags=140,
                    signature=280, status=120, qianxing_activity=180,
                    qianxing_activity_summary=140, qianxing_activity_ranges=180,
                    online_duration=120, cumulative_duration=120, today_ranges=180,
                    status_updated_at=160, last_seen=130, profile=100)
        for column, (key, label) in enumerate(_FRIEND_HEADERS):
            previous = self._auto_friend_column_widths.get(column)
            if previous is not None and table.columnWidth(column) != previous:
                continue
            width = max(44, min(88, metrics.horizontalAdvance(label)+18))
            for row in range(table.rowCount()):
                item = table.item(row, column)
                if item is not None:
                    content = metrics.horizontalAdvance(item.text())
                    if not item.icon().isNull():
                        content += item.icon().actualSize(table.iconSize()).width()+4
                    width = max(width, content+16)
                widget = table.cellWidget(row, column)
                if widget is not None:
                    width = max(width, widget.sizeHint().width()+8)
            width = min(caps[key], width)
            table.setColumnWidth(column, width)
            self._auto_friend_column_widths[column] = table.columnWidth(column)

    def _update_uid_notice(self) -> None:
        rows = self.db.get_display_friends()
        missing = sum(re.fullmatch(r'[0-9]{9}', str(row.get('uid') or '')) is None for row in rows)
        self.uid_notice.setText(
            '提示：当前名单有 %d 位好友尚未录入 UID，可能导致身份匹配及统计不准确。'
            '可继续快速扫描，也可点击“初始化档案”完整补全 UID。' % missing if missing else '')
        self.uid_notice.setVisible(bool(missing))

    def _show_profile_captures(self, fid: int) -> None:
        captures = self.db.get_profile_captures(fid)
        friend = self.db.get_friend(fid) or {}
        title = friend.get('nickname') or friend.get('uid') or '昵称图片'
        ProfileCaptureDialog(captures, title, self).exec()

    def _paste_friend_profile(self) -> None:
        if self._task_busy() or getattr(self, '_closing', False) or not self._require_account():
            return
        database = self.db
        dialog = None
        self._profile_importing = True
        self._sync_task_controls()
        try:
            dialog = ProfileImportDialog(database.get_all_friends(), self._account_uid, self,
                                         image=QApplication.clipboard().image())
            if dialog.exec() != QDialog.Accepted:
                return
            if self.db is not database or getattr(self, '_closing', False):
                return
            values = dialog.values()
            try:
                fid = database.import_friend_profile(**values)
            except Exception as error:
                QMessageBox.warning(self, '无法录入主页', str(error))
                return
            if fid not in {friend['id'] for friend in database.get_display_friends()}:
                self.show_history.setChecked(True)
            self._load_friends_table()
            self._load_events_table()
            self._update_stats()
            for row, friend in enumerate(self._friends_cache):
                if friend['id'] == fid:
                    self.friends_table.selectRow(row)
                    self.friends_table.scrollToItem(self.friends_table.item(row, _FRIEND_COL['uid']))
                    break
            self.tabs.setCurrentIndex(0)
            self._append_log('主页资料已录入当前账号。' +
                             ('可在“主页”列查看保存的截图。' if values.get('save_image', True) else ''))
        finally:
            if dialog is not None:
                dialog.deleteLater()
            self._profile_importing = False
            self._sync_task_controls()

    def _refresh_durations(self) -> None:
        """Refresh the database's validity-bounded status and durations."""
        if getattr(self, '_closing', False):
            return
        busy = self._task_busy()
        self._sync_task_controls()
        if not busy:
            self.start_btn.setText('快速扫描')
        if hasattr(self, "pause_btn"):
            if not (self.scanner.is_running or self.scanner.is_uid_scanning):
                self.pause_btn.setEnabled(False)
                self.pause_btn.setText("暂停")
            else:
                self.pause_btn.setEnabled(True)
                self.pause_btn.setText("继续" if self.scanner.is_paused else "暂停")
        if not hasattr(self, "_friends_cache"):
            return
        friends = (self.db.get_all_friends() if self.show_history.isChecked()
                   else self.db.get_display_friends())
        if [f['id'] for f in friends] != [f['id'] for f in self._friends_cache]:
            self._load_friends_table()
        else:
            self._friends_cache = friends
            for row, f in enumerate(friends):
                self.friends_table.setItem(row, _FRIEND_COL['status'], self._status_item(f))
                self.friends_table.setItem(row, _FRIEND_COL['online_duration'], self._friend_item(_fmt_duration(self._online_secs(f))))
                self.friends_table.setItem(row, _FRIEND_COL['cumulative_duration'], self._friend_item(_fmt_duration(self._cumulative_secs(f))))
                self.friends_table.setItem(row, _FRIEND_COL['today_ranges'], self._friend_item(self._today_ranges(f)))
                self.friends_table.setItem(row, _FRIEND_COL['status_updated_at'], self._friend_item(f.get('status_updated_at') or ''))
                self._set_activity_cells(row, f)
        self._update_presence_stats()
        self._update_uid_notice()
        # Expiry and stopping can write events without a new scan result.
        newest_events = self.db.get_recent_events(limit=1, changes_only=True)
        newest = newest_events[0] if newest_events else None
        if newest != getattr(self, '_latest_event', None):
            self._load_events_table()

    def _set_activity_cells(self, row, friend):
        values = {
            'qianxing_activity': friend.get('qianxing_activity') or '',
            'qianxing_activity_summary': friend.get('qianxing_activity_summary') or _fmt_duration(friend.get('qianxing_activity_seconds')),
            'qianxing_activity_ranges': friend.get('qianxing_activity_ranges') or '',
        }
        for key, value in values.items():
            text = '；'.join(value) if isinstance(value, list) else str(value)
            item = self._friend_item(text)
            item.setToolTip(text)
            self.friends_table.setItem(row, _FRIEND_COL[key], item)

    def _status_item(self, friend: dict) -> QTableWidgetItem:
        status = friend.get('current_status')
        text = {'online': '在线', 'offline': '离线'}.get(status, '未知')
        location = friend.get('location')
        if status == 'online' and location:
            text += '·'+location
        item = self._friend_item(text)
        if status == 'online':
            item.setForeground(QColor('#1a7f37'))
        return item

    def _online_secs(self, f: dict) -> int:
        """The database already includes only the valid monitored tail."""
        return max(0, int(f.get('total_online_seconds') or 0))

    def _cumulative_secs(self, f: dict) -> int:
        return max(0, int(f.get('cumulative_online_seconds') or 0))

    def _today_ranges(self, f: dict) -> str:
        """今日上线时间段（多个用逗号分隔）。"""
        fid = f.get("id")
        if fid is None:
            return ""
        ranges = self.db.get_today_online_ranges(fid)
        return ", ".join(ranges) if ranges else ""

    def _load_events_table(self) -> None:
        events = [event for event in self.db.get_recent_events(limit=500, changes_only=True)
                  if is_status_change(event)]
        self._latest_event = events[0] if events else None
        self.events_table.setRowCount(len(events))
        for row, ev in enumerate(events):
            self.events_table.setItem(row, 0, self._item(ev.get("event_time") or ""))
            uid = (ev.get("uid") or "").strip()
            nickname = (ev.get("nickname") or "").strip()
            uid_item = self._item(uid or "未记录 UID")
            nickname_item = self._item(nickname or "昵称未识别")
            history_hint = "后来补录的信息不会自动回填历史事件。可在好友列表中勾选“显示历史档案”查看保留的档案。"
            if not uid:
                uid_item.setToolTip("这条记录生成时未记录 UID，不代表当前仍未录入。" + history_hint)
            if not nickname:
                nickname_item.setToolTip("这条记录生成时没有识别到昵称。" + history_hint)
            self.events_table.setItem(row, 1, uid_item)
            self.events_table.setItem(row, 2, nickname_item)
            text = _EVENT_TEXT.get(ev.get("event_type"), ev.get("event_type") or "")
            item = self._item(text)
            if ev.get("event_type") in ("added", "removed") and not uid:
                item.setToolTip("这条记录的身份信息不完整，识别或匹配变化也可能产生这类记录，无法据此确定真实的好友增减。")
            if ev.get("event_type") == "removed":
                item.setForeground(QColor("#c0392b"))
            self.events_table.setItem(row, 3, item)

    def _load_dedup_table(self) -> None:
        logs = self.db.get_dedup_logs()
        self.dedup_table.setRowCount(len(logs))
        self._dedup_cache = logs
        for row, d in enumerate(logs):
            self.dedup_table.setItem(row, 0, self._item(d.get("event_time") or ""))
            self.dedup_table.setItem(row, 1, self._item(d.get("matched_nickname") or ""))
            self.dedup_table.setItem(row, 2, self._item(d.get("kept_nickname") or ""))
            self.dedup_table.setItem(row, 3, self._item("%.2f" % (d.get("similarity") or 0.0)))
            undone = d.get("undone")
            state_item = self._item("已撤回" if undone else "已合并")
            if undone:
                state_item.setForeground(QColor("#9e9e9e"))
            self.dedup_table.setItem(row, 4, state_item)

    def _show_dedup_menu(self, pos) -> None:
        row = self.dedup_table.rowAt(pos.y())
        if row < 0 or not hasattr(self, "_dedup_cache") or row >= len(self._dedup_cache):
            return
        d = self._dedup_cache[row]
        if d.get("undone"):
            return
        menu = QMenu(self)
        act = menu.addAction(f"撤回去重（恢复「{d.get('matched_nickname')}」为独立好友）")
        chosen = menu.exec(self.dedup_table.viewport().mapToGlobal(pos))
        if chosen is act:
            if self.db.undo_dedup(d["id"]):
                self._append_log(f"已撤回去重，恢复好友「{d.get('matched_nickname')}」")
                self._load_dedup_table()
                self._load_friends_table()
                self._update_stats()
            else:
                QMessageBox.warning(self, "提示", "撤回失败，该记录可能已撤回或不存在")

    def _update_presence_stats(self) -> None:
        summary = self.db.get_status_summary()
        self.lbl_total.setText(f"总好友: {summary['total']}")
        self.lbl_online.setText(f"在线: {summary['online']}")
        self.lbl_offline.setText(f"离线: {summary['offline']}")
        self.lbl_unknown.setText(f"未知: {summary['unknown']}")

    def _update_stats(self) -> None:
        self._update_presence_stats()
        events = self.db.get_recent_events(limit=100000)
        # 只统计有身份的好友增减，不统计档案录入、UID 补全或匹配待确认。
        change = sum(1 for e in events if e.get("event_type") in ("added", "removed") and is_status_change(e))
        self.lbl_events.setText(f"变动: {change} 次")

    def _item(self, text: str) -> QTableWidgetItem:
        return QTableWidgetItem(text)

    def _friend_item(self, text: str) -> QTableWidgetItem:
        item = self._item(text)
        item.setToolTip(item.text())
        return item

    # ------------------------------------------------------------------
    # 手动修正
    # ------------------------------------------------------------------
    def _show_friend_menu(self, pos) -> None:
        row = self.friends_table.rowAt(pos.y())
        if row < 0:
            return
        menu = QMenu(self)
        pending = self._friends_cache[row].get('nickname_pending')
        act_edit = menu.addAction('核查昵称/签名截图…' if pending or self._friends_cache[row].get('signature_pending') else '编辑好友（昵称/UID/等级/标签/签名）')
        act_delete = menu.addAction("删除该好友记录")
        act = menu.exec(self.friends_table.viewport().mapToGlobal(pos))
        if act is act_edit:
            self._edit_friend(row)
        elif act is act_delete:
            self._delete_friend(row)

    def _current_friend_id(self, row: int):
        if not hasattr(self, "_friends_cache") or row < 0 or row >= len(self._friends_cache):
            return None
        return self._friends_cache[row].get("id")

    def _edit_friend(self, row: int) -> None:
        fid = self._current_friend_id(row)
        if fid is None:
            return
        friend = self.db.get_friend(fid)
        if friend is None:
            return
        friend['nickname_image'] = self.db.get_nickname_image(fid)
        friend['nickname_captures'] = self.db.get_field_captures(fid, 'nickname')
        friend['signature_captures'] = self.db.get_field_captures(fid, 'signature')
        dlg = EditFriendDialog(friend, self)
        if dlg.exec() == QDialog.Accepted:
            try:
                self.db.update_friend_manual(
                    fid, dlg.uid() or None, dlg.nickname(), dlg.level(), dlg.signature(),
                    qianxing_level=dlg.qianxing_level(), tags=dlg.tags(),
                    signature_confirmed=dlg.signature_confirmed())
            except ValueError as e:
                QMessageBox.warning(self, "无法保存", str(e))
                return
            self._load_friends_table()
            self._update_stats()
            if friend.get('nickname_image') and dlg.nickname():
                try:
                    from app.fast_scan import _decode
                    from app.nickname_ocr import add_correction
                    add_correction(_decode(friend['nickname_image']), dlg.nickname(),
                                   'UID '+str(dlg.uid() or fid), '用户在好友编辑窗口人工确认')
                except Exception:
                    logger.exception('昵称已保存，但本机图像校对样本保存失败')
            self._append_log(f"已手动修正好友 #{fid} 信息")

    def _delete_friend(self, row: int) -> None:
        fid = self._current_friend_id(row)
        if fid is None:
            return
        name = self._friends_cache[row].get("nickname") or ""
        if QMessageBox.question(
            self, "确认删除", f"确定删除好友「{name}」的记录吗？\n此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) != QMessageBox.Yes:
            return
        self.db.delete_friend(fid)
        self._load_friends_table()
        self._update_stats()
        self._append_log(f"已删除好友「{name}」的记录")

    # ------------------------------------------------------------------
    # 批量选择 / 检索选中 / 批量删除
    # ------------------------------------------------------------------
    def _selected_friend_ids(self) -> List[int]:
        """返回当前选中行对应的好友 id 列表。"""
        rows = {idx.row() for idx in self.friends_table.selectionModel().selectedRows()}
        ids = []
        for row in sorted(rows):
            fid = self._current_friend_id(row)
            if fid is not None:
                ids.append(fid)
        return ids

    def _select_all_friends(self) -> None:
        """全选好友列表所有行。"""
        self.friends_table.selectAll()
        self._append_log(f"已全选 {self.friends_table.rowCount()} 位好友")

    def _select_matching_friends(self) -> None:
        """昵称或 UID 的子串匹配；空格分隔多个关键词，命中任一即选中。"""
        text = self.search_edit.text().strip()
        if not text:
            QMessageBox.information(self, "提示", "请输入要检索的昵称或 UID")
            return
        keywords = [k.casefold() for k in text.split() if k]
        self.friends_table.clearSelection()
        matched = 0
        for row in range(self.friends_table.rowCount()):
            items = [self.friends_table.item(row, _FRIEND_COL[key]) for key in ('nickname', 'uid')]
            fields = [item.text().casefold() for item in items if item is not None]
            if any(keyword in value for keyword in keywords for value in fields):
                index = self.friends_table.model().index(row, 0)
                self.friends_table.selectionModel().select(
                    index, QItemSelectionModel.Select | QItemSelectionModel.Rows)
                if matched == 0:
                    self.friends_table.scrollToItem(self.friends_table.item(row, _FRIEND_COL['uid']))
                matched += 1
        self._append_log(f"检索「{text}」命中 {matched} 位好友，已选中")
        if matched == 0:
            QMessageBox.information(self, "检索结果", f"当前列表未找到昵称或 UID 包含「{text}」的好友。\n"
                                    '查找已消失或手动录入的资料时，可勾选“显示历史档案”。')

    def _delete_selected_friends(self) -> None:
        """批量删除选中的好友记录。"""
        ids = self._selected_friend_ids()
        if not ids:
            QMessageBox.information(self, "提示", "请先选中要删除的好友（可用检索或全选）")
            return
        rows = {idx.row() for idx in self.friends_table.selectionModel().selectedRows()}
        names = []
        for row in sorted(rows):
            item = self.friends_table.item(row, 1)
            if item is not None:
                names.append(item.text())
        preview = "、".join(names[:10]) + ("…" if len(names) > 10 else "")
        if QMessageBox.question(
            self, "确认批量删除",
            f"确定删除选中的 {len(ids)} 位好友吗？\n{preview}\n此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) != QMessageBox.Yes:
            return
        self.db.delete_friends(ids)
        self._load_friends_table()
        self._update_stats()
        self._append_log(f"已批量删除 {len(ids)} 位好友记录")

    def _delete_all_friends(self) -> None:
        """一键清空全部好友记录。"""
        total = self.friends_table.rowCount()
        if total == 0:
            QMessageBox.information(self, "提示", "好友列表已为空")
            return
        if QMessageBox.question(
            self, "确认全部删除",
            f"确定删除全部 {total} 位好友的记录吗？\n此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) != QMessageBox.Yes:
            return
        self.db.delete_all_friends()
        self._load_friends_table()
        self._update_stats()
        self._append_log(f"已清空全部 {total} 位好友记录")

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if not getattr(self, '_closing', False):
            self._closing = True
            self.scanner.stop()
            self.centralWidget().setEnabled(False)
        if self.scanner.is_running or self.scanner.is_uid_scanning or self._account_reading or self._profile_importing:
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        self._timer.stop()
        self.db.close()
        CONFIG.db_path = self._initial_db_path
        super().closeEvent(event)


class ProfileImageLabel(QLabel):
    """Scale the entire retained screenshot when the viewer is resized."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._original = QPixmap()
        self._zoom = None
        self.setMinimumSize(400, 240)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet('background: #202327; color: white;')

    def set_image(self, data) -> None:
        self._original = QPixmap()
        if data:
            self._original.loadFromData(bytes(data))
        self.set_zoom(self._zoom)

    def set_zoom(self, factor) -> None:
        self._zoom = factor
        if factor is not None and not self._original.isNull():
            self.resize(max(1, round(self._original.width() * factor)),
                        max(1, round(self._original.height() * factor)))
        self._fit_image()

    def _fit_image(self) -> None:
        if self._original.isNull():
            self.setText('截图不可用')
        else:
            target = (QSize(round(self._original.width() * self._zoom), round(self._original.height() * self._zoom))
                      if self._zoom is not None else self.size())
            self.setPixmap(self._original.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_image()


class ProfileCaptureDialog(QDialog):
    def __init__(self, captures: list, nickname: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'好友主页 — {nickname}')
        self.resize(1000, 650)
        layout = QVBoxLayout(self)
        self.captures = sorted(captures, key=lambda item: (item.get('observed_at') or '', item.get('id') or 0), reverse=True)[:2]
        if not self.captures:
            text = QLabel('待采集：尚无该好友的主页截图。完成好友 UID 识别后可在此查看。')
            text.setWordWrap(True)
            text.setAlignment(Qt.AlignCenter)
            layout.addWidget(text, stretch=1)
        else:
            controls = QHBoxLayout()
            self.selector = QComboBox()
            for index, capture in enumerate(self.captures):
                name = capture.get('nickname') or '昵称图片'
                self.selector.addItem(f'{"最新一张" if index == 0 else "前一张"} · {capture.get("observed_at") or ""} · {name}')
            controls.addWidget(self.selector, stretch=1)
            controls.addWidget(QLabel('缩放:'))
            self.zoom_combo = QComboBox()
            for label, factor in [('适应窗口', None), ('100%', 1.0), ('150%', 1.5), ('200%', 2.0)]:
                self.zoom_combo.addItem(label, factor)
            self.zoom_combo.setToolTip('使用 100% 或更高倍率查看 UID 和昵称细节，可拖动滚动条查看全图')
            controls.addWidget(self.zoom_combo)
            layout.addLayout(controls)
            self.preview = ProfileImageLabel()
            self.preview.setMinimumSize(1, 1)
            self.scroll_area = QScrollArea()
            self.scroll_area.setMinimumSize(400, 240)
            self.scroll_area.setWidget(self.preview)
            self.scroll_area.setWidgetResizable(True)
            layout.addWidget(self.scroll_area, stretch=1)
            self.selector.currentIndexChanged.connect(self._select_capture)
            self.zoom_combo.currentIndexChanged.connect(self._change_zoom)
            self._select_capture(0)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _select_capture(self, index: int) -> None:
        if 0 <= index < len(self.captures):
            self.preview.set_image(self.captures[index].get('image'))

    def _change_zoom(self, index: int) -> None:
        factor = self.zoom_combo.itemData(index)
        self.scroll_area.setWidgetResizable(factor is None)
        self.preview.set_zoom(factor)


class EditFriendDialog(QDialog):
    """好友信息手动修正对话框。"""

    def __init__(self, friend: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑好友信息")
        self.setMinimumWidth(360)

        form = QFormLayout(self)
        self._uid = QLineEdit(friend.get("uid") or "")
        self._uid.setPlaceholderText("9 位数字 UID，可留空")
        self._nickname = QLineEdit(friend.get("nickname") or "")
        if friend.get('nickname_pending'):
            self._nickname.setPlaceholderText('可留空保留图片；填写正确昵称后使用文字显示')
            preview = QLabel()
            preview.setObjectName('nicknameReviewImage')
            preview.setPixmap(nickname_pixmap(friend.get('nickname_image'), 640, 64, trim=True))
            preview.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            form.addRow('待核查截图:', preview)
        for field, label in (('nickname', '昵称'), ('signature', '签名')):
            captures = friend.get(field+'_captures') or []
            if captures:
                chooser = QComboBox()
                picture = QLabel()
                picture.setObjectName(field+'HistoryImage')
                picture.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
                for index, item in enumerate(captures[:2]):
                    chooser.addItem(('最新' if index == 0 else '前一张')+' · '+str(item.get('observed_at') or ''))
                def show_image(index, entries=captures, target=picture):
                    target.setPixmap(nickname_pixmap(entries[index]['image'], 640, 72, trim=True))
                chooser.currentIndexChanged.connect(show_image)
                show_image(0)
                form.addRow(label+'截图:', chooser)
                form.addRow('', picture)
        self._level = QSpinBox()
        self._level.setRange(0, 100)
        self._level.setValue(int(friend.get("level") or 0))
        self._level.setSpecialValueText("未知")
        self._qianxing_level = QSpinBox()
        self._qianxing_level.setRange(-1, 9999)
        qianxing_level = friend.get('qianxing_level')
        self._qianxing_level.setValue(-1 if qianxing_level is None else int(qianxing_level))
        self._qianxing_level.setSpecialValueText('未知')
        self._qianxing_level.setToolTip('独立于冒险等级；设为未知可清空')
        self._initial_tags = friend.get('tags')
        self._tags = QLineEdit(self._initial_tags or '')
        self._tags.setPlaceholderText('千星奇域好友标签，可留空')
        self._signature = QLineEdit(friend.get("signature") or "")
        self._initial_signature = friend.get('signature') or ''
        self._signature_pending = bool(friend.get('signature_pending'))
        self.signature_reviewed = QCheckBox('签名已校准（留空表示确认为空签名）')
        self.signature_reviewed.setVisible(self._signature_pending)

        form.addRow("UID:", self._uid)
        form.addRow("昵称:", self._nickname)
        form.addRow("冒险等级:", self._level)
        form.addRow("千星等级:", self._qianxing_level)
        form.addRow("标签:", self._tags)
        form.addRow("签名:", self._signature)
        if self._signature_pending:
            form.addRow('', self.signature_reviewed)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def uid(self) -> str:
        return self._uid.text().strip()

    def nickname(self) -> str:
        return self._nickname.text().strip()

    def level(self) -> Optional[int]:
        return self._level.value() or None

    def signature(self) -> str:
        return self._signature.text().strip() or None

    def signature_confirmed(self):
        return (not self._signature_pending or self.signature_reviewed.isChecked()
                or self._signature.text().strip() != self._initial_signature.strip())

    def qianxing_level(self) -> Optional[int]:
        value = self._qianxing_level.value()
        return None if value < 0 else value

    def tags(self) -> Optional[str]:
        text = self._tags.text().strip()
        return text if text or self._initial_tags is not None else None

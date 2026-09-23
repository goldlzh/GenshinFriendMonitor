"""应用配置管理。

配置优先级：config.json（用户可改） > 默认值。
首次运行会自动生成 config.json 到程序目录下。
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field, fields, asdict


def _base_dir() -> str:
    """程序所在目录（兼容 PyInstaller 打包后的 _MEIPASS 解包目录）。"""
    if getattr(sys, "frozen", False):
        # 打包后：exe 所在目录
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def parse_scan_frequency(value) -> float | None:
    """Minutes: blank/None is one pass, zero repeats without an interval."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    message = '频率请输入非负有限数值（分钟）；留空为单次，0 为连续扫描。'
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        minutes = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(message) from None
    if minutes < 0 or not math.isfinite(minutes) or not math.isfinite(minutes*60):
        raise ValueError(message)
    return minutes


@dataclass
class Config:
    # ---- 扫描 ----
    window_title_keyword: str = "原神"      # 目标窗口标题关键字（用于定位窗口）
    scan_interval_seconds: int = 60         # 旧配置兼容；界面改用 scan_frequency_minutes
    scan_frequency_minutes: float | None = None  # 空为单次，0连续，正数为分钟间隔
    online_only: bool = False               # 快速扫描仅采集在线好友
    bring_to_front: bool = True             # 每轮尝试激活游戏，失败则等待用户切回
    startup_delay_seconds: float = 3.0     # 点击开始后留出切回游戏的时间，仅每次启动任务时等待
    focus_wait_seconds: float = 30.0       # 自动激活失败后等待用户切回游戏，期间不发送输入
    minimize_on_start: bool = False         # 保留扫描器窗口以便查看进度
    ensure_friend_list: bool = True         # 允许自动返回好友界面；关闭后仍须验证左上角标题
    navigation_max_esc: int = 10             # 每轮返回主界面最多按 ESC 次数（上限 10）
    navigation_wait_seconds: float = 0.8    # 导航按键后等待界面动画
    key_hold_seconds: float = 0.12         # 扫描码按键保持时间，适配游戏输入轮询
    friend_open_attempts: int = 4           # 仅确认左下角 Enter 后尝试按 O（上限 4）
    roster_restart_attempts: int = 3        # 名单在采集中变化时最多重新开始次数
    fast_scan: bool = True                 # 六行拖动、批量字段 OCR、完整性核验
    fast_ocr: bool = True                  # 固定文字槽位直接批量识别
    ocr_threads: int = 2                   # 避免 OCR 与游戏争用全部 CPU 核心
    nickname_font_path: str = ""           # 空值时从选中的本机游戏目录查找字库
    scan_once_only: bool = False           # 仅保留旧配置兼容；调度以频率输入为准
    save_scan_evidence: bool = True         # 保存整页图；存疑昵称小图始终单独保留
    save_profile_screenshots: bool = True   # 仅控制主页存档；待校准昵称/签名图必须保存
    drag_duration_seconds: float = 0.24
    drag_hold_seconds: float = 1.0         # 到达终点后保持按下 1 秒，再松开
    fast_settle_seconds: float = 0.16

    scroll_step: int = 2                    # wheel 模式下每次滚动的滚轮格数（正数，越小越稳、越不易漏）
    scroll_settle_seconds: float = 0.3      # 每次滚动后等待画面稳定的时间（秒）
    max_scrolls: int = 80                   # 单次扫描最大滚动次数（防止死循环）
    bottom_confirmations: int = 2           # 连续稳定画面确认底部
    removal_confirmations: int = 2          # 连续完整扫描缺失后登记消失
    bottom_similarity: float = 0.98         # 判定“到底了”的图像相似度阈值（0~1）
    # 滚动方式：wheel=滚轮（推荐，直接向窗口发 WM_MOUSEWHEEL，不依赖光标移动）；drag=模拟手指拖拽滑动（备用）。
    scroll_mode: str = "wheel"
    wheel_invert: bool = True               # 云原神滚轮方向与标准相反（仅 wheel 模式生效）
    drag_pixels: int = 300                  # 每次拖拽的像素距离（scroll_mode=drag 且未启用重叠时生效）
    drag_anchor_x_ratio: float = 0.25       # 拖拽锚点水平位置（占窗口宽度比例，好友列表通常在左侧）
    drag_anchor_y_ratio: float = 0.5        # 拖拽锚点垂直位置（占窗口高度比例）
    drag_invert: bool = True                # 云原神拖拽方向与标准相反：需向下拖才能向下浏览列表
    scroll_overlap_ratio: float = 0.7       # 每次滚动距离占窗口高度的比例（<1 保证相邻屏重叠，防止漏识别）

    # ---- 旧解析兼容字段（当前单卡片解析不使用底色猜测状态） ----
    # 底色判断：好友卡片背景「偏灰=离线，偏亮/彩色=在线」。
    # 仅当文字状态（在线/离线/时间/场景名）无法判定时才用底色兜底。
    use_bg_color: bool = True               # 是否启用底色判断
    bg_sample_x1: float = 0.28              # 底色采样横向起点（占窗口宽度比例，位于状态文字右侧空白处）
    bg_sample_x2: float = 0.42              # 底色采样横向终点
    bg_saturation_threshold: float = 60.0   # 饱和度低于此值视为「灰」（离线）
    bg_dark_threshold: float = 110.0        # 亮度低于此值视为「暗」（离线）
    # 保留旧配置字段以兼容已有 config.json；新扫描不再依据相似度自动合并身份。
    nickname_similarity_threshold: float = 0.72

    # ---- 资料卡 / 自动及手动识别 UID ----
    profile_wait_seconds: float = 1.2       # 点击打开资料卡后等待动画的时间（秒）
    profile_close_seconds: float = 0.6      # 关闭资料卡后等待列表恢复的时间（秒）
    avatar_click_offset_x: int = 0          # 点击头像的额外水平微调（像素，负数=再往左）。解析器已定位到头像中心，一般保持 0
    nickname_min_length: int = 1            # 昵称最短长度（过滤 OCR 噪声）
    # 手动识别 UID：点击头像后弹出的选项菜单
    menu_wait_seconds: float = 0.8          # 点击头像后等待弹出选项菜单的时间（秒）
    view_profile_words: list = field(        # 选项菜单里「查看资料」按钮的关键词（OCR 命中其一即点击）
        default_factory=lambda: ["查看资料", "资料", "个人资料", "查看名片"]
    )
    uid_scan_interval_seconds: float = 0.5  # 手动识别 UID 时，每个好友之间的间隔（秒）

    # ---- OCR ----
    ocr_confidence: float = 0.6             # OCR 文本置信度阈值（低于则记入失败日志）
    uid_pattern: str = r"\d{9}"             # UID 匹配正则（原神 UID 为 9 位数字）
    ocr_upscale_factor: float = 1.5         # OCR 前放大倍数（提升小字/艺术字昵称识别率，1.0=不放大；越大越慢）
    # 昵称排除词：界面上的固定文字（品牌名等），即使被 OCR 识别到也不当作昵称
    nickname_exclude_words: list = field(
        default_factory=lambda: ["云原神", "云·原神", "UID", "uid"]
    )

    # ---- 存储 ----
    db_path: str = field(default_factory=lambda: os.path.join(BASE_DIR, "data", "friends.db"))
    log_dir: str = field(default_factory=lambda: os.path.join(BASE_DIR, "logs"))
    export_dir: str = ""                    # Excel 导出目录，空字符串 = 桌面

    # ---- MySQL 导出 ----
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "genshin_friends"

    def ensure_dirs(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

    @property
    def export_dir_resolved(self) -> str:
        if self.export_dir:
            return self.export_dir
        # 桌面路径
        return os.path.join(os.path.expanduser("~"), "Desktop")

    def save(self) -> None:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in data.items():
                    if k in {f.name for f in fields(cls)}:
                        setattr(cfg, k, v)
            except Exception:
                pass
        # Earlier verification entry points saved a permanent one-shot flag.
        # The frequency already represents all three modes, including blank.
        cfg.scan_once_only = False
        cfg.ensure_dirs()
        if not os.path.exists(CONFIG_PATH):
            cfg.save()
        return cfg


CONFIG = Config.load()

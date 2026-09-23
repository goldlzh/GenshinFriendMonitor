"""好友字段解析。

运行扫描使用 parse_card()，输入是已检测边界并独立裁好的单人横条左侧。
parse() / _cluster_rows() 保留用于旧离线审查工具，不用于普通或 UID 列表扫描。
资料卡 UID 和右上角好友人数提取也在此模块。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional

from app.config import CONFIG
from app.ocr_engine import OcrResult

# 状态相关关键词
# 在线「活动词」：好友正在进行某项活动（如「千星奇域游玩中」「深境螺旋挑战中」）
_ACTIVITY_WORDS = ("游玩中", "挑战中", "探索中", "进行中", "游戏中", "冒险中", "联机", "可邀请", "正在")
_ONLINE_WORDS = ("在线", "在线中", "游戏中", "联机", "可邀请", "正在")
_OFFLINE_WORDS = ("离线", "离开", "暂离")
# 离线「最近登录时间」表达（注意「X天前在线」里也含“在线”，须优先判定为离线）
_TIME_PATTERNS = (
    r"\d+\s*天前", r"\d+\s*小时前", r"\d+\s*分钟前", r"\d+\s*秒前",
    r"今日内", r"昨天", r"前天",
    r"\d+\s*天", r"\d+\s*小时", r"\d+\s*分钟",
)
# 等级 / 噪声（这些不算昵称）
_LEVEL_PATTERNS = (r"冒险等阶\s*(\d+)", r"等级\s*(\d+)", r"Lv\.?\s*(\d+)", r"Lv(\d+)", r"^\d+$")
_UID_RE = re.compile(r"(?<![0-9])[0-9]{9}(?![0-9])")

# 界面固定文字 / 按钮（噪声，不参与昵称/状态识别）
_NOISE_WORDS = (
    "申请加入", "黑名单管理", "我的好友", "好友", "返回",
    "省流", "添加", "搜索", "切换", "排序", "全部",
)
# 头像中心相对「等级/昵称文本左边缘」的水平偏移（物理像素，负方向向左）。
# 实测：等级/昵称文本左边缘 x≈185，头像彩色区域 x≈108~148（中心≈128），
# 故偏移 55 可命中头像中心；用 35 会点到头像右边缘外的背景。
_AVATAR_LEFT_OFFSET = 55


def _text_height(it: OcrResult) -> float:
    """文本框高度（用于区分大字昵称与小字签名/状态）。"""
    return max(p[1] for p in it.box) - min(p[1] for p in it.box)


def _is_noise(text: str) -> bool:
    """是否为界面固定文字/按钮等噪声。"""
    t = text.strip()
    if not t:
        return True
    for w in _NOISE_WORDS:
        if w in t:
            return True
    return False


@dataclass
class FriendEntry:
    """好友列表中的一个条目（未绑定 UID）。"""
    nickname: str
    status: str            # online / offline
    last_seen: Optional[str]
    level: Optional[int]
    click_x: int           # 头像中心 x（屏幕绝对坐标，用于点击打开资料卡）
    click_y: int           # 头像中心 y（屏幕绝对坐标）
    confidence: float
    source: str = ""
    location: Optional[str] = None  # 好友当前所在场景/位置（如「千星奇域」），仅在线时有
    nickname_pending: bool = False  # 昵称仅保留图像，等待人工核查；不是猜测文字
    signature: Optional[str] = None
    signature_confidence: float = 0.0
    interface_mode: str = 'normal'
    qianxing_level: Optional[int] = None
    tags: Optional[str] = None  # None=未确认，空字符串=千星界面确认无标签
    signature_pending: bool = False
    qianxing_activity: Optional[str] = None
    qianxing_activity_verified: bool = False

    def to_dict(self) -> dict:
        return {
            "nickname": self.nickname,
            "status": self.status,
            "last_seen": self.last_seen,
            "level": self.level,
            "click_x": self.click_x,
            "click_y": self.click_y,
            "confidence": self.confidence,
            "source": self.source,
            "location": self.location,
            "nickname_pending": self.nickname_pending,
            "signature": self.signature,
            "signature_confidence": self.signature_confidence,
            "interface_mode": self.interface_mode,
            "qianxing_level": self.qianxing_level,
            "tags": self.tags,
            "signature_pending": self.signature_pending,
            "qianxing_activity": self.qianxing_activity,
            "qianxing_activity_verified": self.qianxing_activity_verified,
        }


def nickname_requires_image(text: str) -> bool:
    """不可丢弃空白或用占位字符冒充完整昵称；这些昵称保留原图。"""
    return any(char.isspace() or unicodedata.category(char)[0] in 'CZ'
               or char in '\ufffc\ufffd' for char in text)


def qianxing_activity_name(text: str) -> Optional[str]:
    """Complete game-style titles and the explicit editor activity label."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text == '编辑内容中':
        return text
    if not 3 <= len(text) <= 82:
        return None
    if (text[0], text[-1]) not in (('「', '」'), ('『', '』')):
        return None
    title = text[1:-1].strip()
    if not title or any(char in title for char in '\r\n\t') or title.endswith(('...', '…', '⋯')):
        return None
    return title


def _extract_last_seen(text: str) -> Optional[str]:
    for pat in _TIME_PATTERNS:
        m = re.search(pat, text)
        if m:
            return m.group(0)
    return None


def _extract_level(text: str) -> Optional[int]:
    for pat in _LEVEL_PATTERNS:
        m = re.search(pat, text)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                continue
    return None


def _is_status_word(text: str) -> Optional[bool]:
    """True=在线词, False=离线词, None=不是状态词。

    优先级：离线「最近登录时间」（如「3天前在线」「今日内」）→ 在线活动词
    （如「千星奇域游玩中」）→ 纯「在线」→ 离线词。
    """
    t = text.strip()
    # 1. 离线时间表达优先（「X天前在线」含“在线”但表示离线）
    if _extract_last_seen(t):
        return False
    # 2. 在线活动词（场景 + 中）
    for w in _ACTIVITY_WORDS:
        if w in t:
            return True
    # 3. 纯「在线」
    if "在线" in t:
        return True
    # 4. 离线词
    for w in _OFFLINE_WORDS:
        if w in t:
            return False
    return None


def _is_nickname_candidate(text: str) -> bool:
    """判断文本块是否为昵称候选（排除状态/时间/等级/纯数字/固定文字）。"""
    t = text.strip()
    if not t or len(t) < CONFIG.nickname_min_length:
        return False
    # 纯符号（无字母/数字/中文）→ 艺术字乱码，不算昵称
    if not re.search(r"[\w\u4e00-\u9fff]", t):
        return False
    if _is_status_word(t) is not None:
        return False
    if _extract_last_seen(t):
        return False
    if _UID_RE.search(t):
        return False
    # 任何等级文本（Lv.60 / 60 / 等级60）都不算昵称
    if _extract_level(t) is not None:
        return False
    if _is_noise(t):
        return False
    # 排除界面固定文字（品牌名、UID 标签等）
    for word in CONFIG.nickname_exclude_words:
        if word and word in t:
            return False
    return True


def _cluster_rows(items: List[OcrResult]) -> List[List[OcrResult]]:
    """按 y 坐标聚类成「卡片」。

    实测每个好友卡片约 80px 高，卡片内文本（昵称在顶部、等级/状态/签名在
    底部）纵向跨度约 35~40px，相邻卡片之间间距约 45~50px。因此用平均字高
    的 2.2 倍作为阈值，能把同一卡片的文本聚到一起，同时不吞并下一张卡片。
    """
    if not items:
        return []
    heights = [_text_height(it) for it in items]
    avg_h = sum(heights) / len(heights) if heights else 20
    threshold = max(avg_h * 2.2, 30)

    rows: List[List[OcrResult]] = []
    current: List[OcrResult] = [items[0]]
    prev_y = items[0].top
    for it in items[1:]:
        if abs(it.top - prev_y) <= threshold:
            current.append(it)
        else:
            current.sort(key=lambda x: x.left)
            rows.append(current)
            current = [it]
        prev_y = it.top
    if current:
        current.sort(key=lambda x: x.left)
        rows.append(current)
    return rows


def _extract_location(items: List[OcrResult], level_it: Optional[OcrResult],
                      nickname_it: Optional[OcrResult]) -> Optional[str]:
    """识别好友状态文字位置（等级右侧）的「场景/位置词」（如「千星奇域」）。

    云原神中，在线好友的状态栏除了「在线」，还可能显示好友当前所在场景
    （深境螺旋、尘歌壶、千星奇域等）。这些词表示好友在线。返回场景名，
    无则返回 None。
    """
    if level_it is None:
        return None
    right_edge = max(p[0] for p in level_it.box)
    candidates: List[OcrResult] = []
    for it in items:
        if it is level_it or it is nickname_it:
            continue
        t = it.text.strip()
        # 纯状态词（在线/离线/离开等）不算场景
        if t in ("在线", "在线中", "离线", "离开", "暂离"):
            continue
        # 时间表达（离线）→ 不算
        if _extract_last_seen(t):
            continue
        # 等级/纯数字 → 不算
        if _extract_level(t) is not None:
            continue
        # 场景名位于等级右侧一小段（约 0~200px），且文本较短
        if right_edge - 5 <= it.left <= right_edge + 200:
            if len(t) <= 10:
                candidates.append(it)
    if not candidates:
        return None
    # 取最靠近等级的候选
    candidates.sort(key=lambda x: x.left)
    loc = candidates[0].text
    # 去掉活动词后缀（如「千星奇域游玩中」→「千星奇域」）
    for w in _ACTIVITY_WORDS:
        if loc.endswith(w):
            loc = loc[: -len(w)]
            break
    return loc or None


def _judge_by_bg(img, row: List[OcrResult]) -> Optional[str]:
    """根据好友卡片底色判断在线/离线：偏灰/偏暗→offline，偏亮/彩色→online。

    img：整张截图（RGB numpy 数组）。row：该卡片聚到的 OCR 文本。
    返回 online/offline，无法判断返回 None。
    """
    if img is None:
        return None
    try:
        import numpy as np
        h, w = img.shape[:2]
        top = int(max(0, min(it.top for it in row) - 5))
        bottom = int(min(h, max(it.bottom for it in row) + 5))
        x1 = int(w * CONFIG.bg_sample_x1)
        x2 = int(w * CONFIG.bg_sample_x2)
        if bottom <= top or x2 <= x1:
            return None
        region = img[top:bottom, x1:x2].reshape(-1, 3).astype(np.float32)
        mx = region.max(axis=1)
        mn = region.min(axis=1)
        sat = float((mx - mn).mean())       # 饱和度（灰≈0）
        gray = float(region.mean())         # 亮度（暗≈低）
        if sat < CONFIG.bg_saturation_threshold:
            return "offline"
        if gray < CONFIG.bg_dark_threshold:
            return "offline"
        return "online"
    except Exception:
        return None


def parse(items: List[OcrResult], left_offset: int = 0, top_offset: int = 0,
         img=None) -> List[FriendEntry]:
    """解析好友列表截图，返回好友条目列表。

    left_offset / top_offset：截图区域相对屏幕左上角的偏移（用于把
    相对坐标换算成屏幕绝对坐标，供点击头像使用）。

    实测排版（每个好友卡片约 80px 高）：
      [头像 x≈130-175]  昵称(大字 h≈30, x≈180, 卡片上部)
                        Lv.60(x≈184)  在线(x≈243)  签名(小字 x≈590)  [申请加入 x≈1016]

    判定逻辑：
      1. 过滤按钮/标题等噪声。
      2. 一张卡片若含「Lv.xx」或「在线」或时间表达，即为好友条目。
      3. 状态：含「在线」→ online；含时间表达→ offline；否则 offline。
      4. 昵称：卡片内高度最大的「大字」文本；缺失则用占位符。
      5. 点击坐标 = 头像中心（位于等级/昵称文本左侧约 55px）。
    """
    items = sorted((it for it in items if it.confidence >= CONFIG.ocr_confidence),
                   key=lambda it: (it.top, it.left))
    rows = _cluster_rows(items)
    entries: List[FriendEntry] = []

    for row in rows:
        # 1. 过滤噪声
        useful = [it for it in row if not _is_noise(it.text)]
        if not useful:
            continue

        texts = [it.text.strip() for it in useful]

        # 2. 状态 / 等级 / 时间
        status: Optional[str] = None
        last_seen: Optional[str] = None
        level: Optional[int] = None
        level_it: Optional[OcrResult] = None
        for it in useful:
            t = it.text.strip()
            s = _is_status_word(t)
            if s is True:
                status = "online"
            elif s is False:
                status = "offline"
            if not last_seen:
                last_seen = _extract_last_seen(t)
            if level is None:
                lv = _extract_level(t)
                if lv is not None:
                    level = lv
                    level_it = it

        # 3. 是否好友卡片：有等级 或 有状态 或 有时间
        if level is None and status is None and last_seen is None:
            continue

        # 4. 昵称 = 卡片内高度最大的「大字」候选（需明显高于等级/状态/签名小字）
        heights = [_text_height(it) for it in useful]
        max_h = max(heights)
        min_h = min(heights)
        nickname_it: Optional[OcrResult] = None
        # 仅当存在明显的「大字 vs 小字」层级差时才挑选昵称，
        # 否则（如昵称整行漏检、只剩小字）直接走占位符，避免把签名误当昵称。
        if max_h >= min_h * 1.4:
            big_threshold = max_h * 0.85
            for it in useful:
                if not _is_nickname_candidate(it.text):
                    continue
                if _text_height(it) < big_threshold:
                    continue
                if nickname_it is None or it.left < nickname_it.left:
                    nickname_it = it

        # 4.5 场景/位置词（在线好友状态栏可能显示「千星奇域」「深境螺旋」等场景名）
        location = _extract_location(useful, level_it, nickname_it)

        # 4.6 状态兜底：文字状态缺失时，依次用场景名、时间、底色判断
        if status is None:
            if location:
                status = "online"
            elif last_seen:
                status = "offline"
            elif CONFIG.use_bg_color:
                status = _judge_by_bg(img, useful)
        if status is None:
            status = "offline"

        # 5. 头像点击坐标（左边缘基准 + 向左偏移）
        anchor = level_it or nickname_it or useful[0]
        avatar_x = int(left_offset + anchor.left - _AVATAR_LEFT_OFFSET)
        avatar_y = int(top_offset + anchor.center[1])

        if nickname_it is not None:
            nickname = nickname_it.text
            confidence = nickname_it.confidence
        else:
            # 昵称无法识别（特殊字体/符号）：用位置占位，后续靠 UID + 手动修正
            nickname = f"未识别好友@{avatar_x}_{avatar_y}"
            confidence = 0.5

        entries.append(FriendEntry(
            nickname=nickname,
            status=status,
            last_seen=last_seen,
            level=level,
            click_x=avatar_x,
            click_y=avatar_y,
            confidence=confidence,
            source=" ".join(texts),
            location=location,
        ))

    # 去重：相邻条目昵称相同的只保留一个（占位昵称含坐标，天然唯一）
    deduped: List[FriendEntry] = []
    seen = set()
    for e in entries:
        if e.nickname in seen:
            continue
        seen.add(e.nickname)
        deduped.append(e)
    return deduped


def extract_uid_from_text(text: str) -> Optional[str]:
    """从资料卡 OCR 文本中提取 9 位 UID。"""
    m = _UID_RE.search(text)
    return m.group(0) if m else None


def extract_uid_from_results(
    results: List[OcrResult], img_width: int, img_height: int
) -> Optional[str]:
    """从资料卡 OCR 结果中提取 UID，排除右下角玩家自己的 UID。

    玩家自己的 UID 固定显示在屏幕右下角（游戏 HUD），资料卡 UID 在
    画面中部/上部。因此跳过落在右下角区域的 9 位数字文本，避免把
    自己的 UID 误当成好友 UID。
    """
    for r in results:
        m = _UID_RE.search(r.text)
        if not m:
            continue
        cx = (r.box[0][0] + r.box[2][0]) / 2
        cy = (r.box[0][1] + r.box[2][1]) / 2
        # 右下角区域（右 1/3 且 下 1/4）→ 玩家自己的 UID
        if cx > img_width * 0.66 and cy > img_height * 0.75:
            continue
        return m.group(0)
    return None


def extract_friend_count(items: List[OcrResult]) -> Optional[int]:
    """只从好友标题读取人数，用于核对名单完整性；不猜测缺失的标题。"""
    ordered = sorted(items, key=lambda it: (it.top, it.left))
    for item in ordered:
        if item.confidence < CONFIG.ocr_confidence or not re.match(r'^(?:我的)?好友', item.text.strip()):
            continue
        text = item.text
        # OCR 框顶部相差不足 1px 也可能把右侧人数排到标题之前。
        # 按几何上的右邻框匹配，不能依赖 OCR 的输出先后顺序。
        right = max(p[0] for p in item.box)
        neighbors = [candidate for candidate in ordered if candidate is not item
                     and candidate.confidence >= CONFIG.ocr_confidence
                     and right - 5 <= candidate.left <= right + _text_height(item) * 4
                     and abs(candidate.center[1] - item.center[1]) <= max(_text_height(item), _text_height(candidate)) * .7]
        for candidate in sorted(neighbors, key=lambda it: it.left)[:2]:
            text += ' ' + candidate.text
        match = re.search(r'(?:我的)?好友\s*[（(]?\s*([0-9]+)\s*[/／]\s*([0-9]+)', text)
        if match and int(match[1]) <= int(match[2]):
            return int(match[1])
    return None


def parse_card(items: List[OcrResult], card_height: int, click_x: int,
               click_y: int, *, allow_pending_name: bool = False,
               interface_mode: str = 'normal',
               qianxing_level_absent: bool = False,
               qianxing_activity_verified: bool = False,
               qianxing_activity_color: Optional[str] = None) -> Optional[FriendEntry]:
    """解析已按视觉边界裁好的单人横条左侧（不含签名/按钮）。

    此入口不进行跨行聚类，不按字体大小猜昵称。所有字段必须位于本卡
    固定的昵称/等级/状态槽位；缺失、跨槽或相互矛盾时返回 None。
    原 parse() 仅保留给旧离线审查脚本，扫描器不再调用。
    """
    if card_height <= 0 or interface_mode not in ('normal', 'qianxing'):
        return None
    trusted = [it for it in items if it.confidence >= CONFIG.ocr_confidence]
    # OCR 框可以稍有边缘留白，但其中心必须在各自字段的高度范围内。
    names = [it for it in trusted if .12 <= it.center[1] / card_height <= .52
             and it.top >= -.02 * card_height and it.bottom <= .59 * card_height]
    details = [it for it in trusted if .56 <= it.center[1] / card_height <= .90
               and it.top >= .49 * card_height and it.bottom <= .99 * card_height]
    if (not names and not allow_pending_name) or not details:
        return None
    names.sort(key=lambda it: it.left)
    pending_name = False
    # 一个昵称被拆成多块时，只拼接同一行相邻块；多行昵称候选直接拒绝。
    for a, b in zip(names, names[1:]):
        overlap = min(a.bottom, b.bottom) - max(a.top, b.top)
        if overlap < min(_text_height(a), _text_height(b)) * .6:
            return None
        if b.left - max(p[0] for p in a.box) > max(_text_height(a), _text_height(b)):
            if not allow_pending_name:
                return None
            pending_name = True
    nickname = ''.join(getattr(it, 'raw_text', it.text) for it in names)
    pending_name = pending_name or nickname_requires_image(nickname)
    if pending_name:
        nickname = ''
    if (not nickname and not (allow_pending_name or pending_name)) or (nickname and not CONFIG.nickname_min_length <= len(nickname) <= 30):
        return None
    # 固定昵称槽内允许纯符号（例如 <^_^>、%）和纯数字；不能套用整屏
    # OCR 的噪声过滤规则。空值、低置信度和不完整等级/状态仍会拒绝。
    details.sort(key=lambda it: it.left)
    levels = []
    state_parts = []
    used = list(names)
    for item in details:
        text = item.text.strip()
        if interface_mode == 'qianxing':
            if item.left < 115:
                # This slot is supplied by the independently located digits
                # crop. Never salvage a number from Lv.6+, 200), or ornaments.
                if re.fullmatch(r'[0-9]{1,3}', text):
                    levels.append(int(text))
                elif text == '--' and qianxing_level_absent:
                    levels.append(None)
                else:
                    return None
            else:
                state_parts.append(text)
            used.append(item)
            continue
        digits = '{1,2}'
        match = re.search(r'(?:[Ll][Vv][.。]?|冒险等阶|等级)\s*([0-9]'+digits+r')(?![0-9])', text)
        if match:
            # 等级应位于裁片最左侧；签名/其他文本的等级不能替代它。
            if item.left > 115:
                return None
            levels.append(int(match[1]))
            remainder = (text[:match.start()] + text[match.end():]).strip(' ·•:：')
            if remainder:
                state_parts.append(remainder)
            used.append(item)
        elif item.center[0] <= 105 and re.fullmatch(r'[0-9]'+digits, text):
            levels.append(int(text))
            used.append(item)
        elif item.left >= 85:
            state_parts.append(text)
            used.append(item)
    # 999 is a bounded OCR sanity check, not a claimed game level limit.
    level_minimum = 0 if interface_mode == 'qianxing' else 1
    level_limit = 999 if interface_mode == 'qianxing' else 60
    if len(levels) != 1 or not state_parts:
        return None
    if levels[0] is not None and not level_minimum <= levels[0] <= level_limit:
        return None
    statuses = set()
    last_seen = None
    location = None
    verified_activity = None
    for text in state_parts:
        if interface_mode == 'qianxing':
            if text == '空闲中':
                statuses.add('online')
                continue
            activity = qianxing_activity_name(text)
            if qianxing_activity_verified and activity is not None:
                statuses.add('online')
                location = activity
                if qianxing_activity_color == 'orange':
                    verified_activity = activity
                continue
            team = re.fullmatch(r'(\d{1,3})\s*[/／]\s*(\d{1,3})\s*组队中', text)
            if team:
                if not 1 <= int(team[1]) <= int(team[2]) <= 999:
                    return None
                statuses.add('online')
                continue
        value = _is_status_word(text)
        if value is True and any(word in text for word in _OFFLINE_WORDS):
            return None
        if value is not None:
            statuses.add('online' if value else 'offline')
            last_seen = last_seen or _extract_last_seen(text)
            if value:
                for word in _ACTIVITY_WORDS:
                    if text.endswith(word) and text != word:
                        location = text[:-len(word)] or None
                        break
        elif text in ('深境螺旋', '尘歌壶', '千星奇域', '幻想真境剧诗'):
            statuses.add('online')
            location = text
        else:
            # 未知文本不能据底色或缺省值强行判成在线/离线。
            return None
    if len(statuses) != 1:
        return None
    status = statuses.pop()
    return FriendEntry(nickname, status, last_seen,
                       levels[0] if interface_mode == 'normal' else None, click_x, click_y,
                       min(it.confidence for it in used),
                       source=' | '.join(it.text for it in trusted),
                       location=location if status == 'online' else None,
                       nickname_pending=not bool(nickname), interface_mode=interface_mode,
                       qianxing_level=levels[0] if interface_mode == 'qianxing' else None,
                       qianxing_activity=verified_activity if status == 'online' else None,
                       qianxing_activity_verified=bool(verified_activity) and status == 'online')

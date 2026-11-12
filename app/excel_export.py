"""Excel 导出：将好友信息与状态变动导出到桌面 .xlsx。"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import List, Optional

from app.config import CONFIG
from app.logger import logger
from app.status_events import STATUS_CHANGE_LABELS, is_status_change


def _fmt_online_seconds(secs: Optional[int]) -> str:
    if not secs:
        return "0秒"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}小时{m}分"
    if m:
        return f"{m}分{s}秒"
    return f"{s}秒"


def export_friends_to_excel(friends: List[dict], events: List[dict],
                            dest_dir: Optional[str] = None, *, account_uid: Optional[str] = None) -> str:
    """导出好友信息（Sheet1）与状态变动（Sheet2），返回生成的文件路径。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    if account_uid is not None and (
            not isinstance(account_uid, str) or re.fullmatch(r'[0-9]{9}', account_uid) is None):
        raise ValueError('导出账号 UID 必须是 9 位数字')
    dest_dir = dest_dir or CONFIG.export_dir_resolved
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    account_prefix = f'{account_uid}_' if account_uid is not None else ''
    basename = f"原神好友统计_{account_prefix}{stamp}"

    wb = Workbook()

    # ---- Sheet1: 好友信息 ----
    ws = wb.active
    ws.title = "好友信息"
    headers = ["UID", "昵称", "冒险等级", "签名", "当前状态", "位置/场景", "本次在线时长", "累计在线时长", "最近状态变化", "首次扫描到", "最近扫描", "最近登录", "千星等级", "标签"]
    if account_uid is not None:
        headers.insert(0, '账号UID')
    headers.extend(['游玩奇域', '奇域游玩时间统计', '奇域游玩时段'])
    ws.append(headers)
    header_fill = PatternFill("solid", fgColor="4472C4")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill

    for f in friends:
        values = [
            f.get("uid") or "",
            f.get("nickname") or "",
            f.get("level") if f.get("level") is not None else "",
            f.get("signature") or "",
            {'online': '在线', 'offline': '离线'}.get(f.get('current_status'), '未知'),
            f.get("location") or "",
            _fmt_online_seconds(f.get("total_online_seconds")),
            _fmt_online_seconds(f.get("cumulative_online_seconds")),
            f.get("status_updated_at") or "",
            f.get("first_seen_at") or "",
            f.get("last_scan_at") or "",
            f.get("last_seen") or "",
            f.get("qianxing_level") if f.get("qianxing_level") is not None else "",
            f.get("tags") or "",
            f.get('qianxing_activity') or '',
            f.get('qianxing_activity_summary') or _fmt_online_seconds(f.get('qianxing_activity_seconds')),
            f.get('qianxing_activity_ranges') or '',
        ]
        ws.append(([account_uid] if account_uid is not None else []) + values)

    # 自动列宽
    for col_idx in range(1, len(headers) + 1):
        ws.column_dimensions[get_column_letter(col_idx)].width = 18

    # ---- Sheet2: 状态变动 ----
    ws2 = wb.create_sheet("状态变动")
    ev_headers = ["时间", "UID", "昵称", "事件类型"]
    if account_uid is not None:
        ev_headers.insert(0, '账号UID')
    ws2.append(ev_headers)
    for cell in ws2[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    for ev in events:
        etype = STATUS_CHANGE_LABELS.get(ev.get('event_type'))
        if not is_status_change(ev):
            continue
        ws2.append(([account_uid] if account_uid is not None else []) +
                   [ev.get("event_time"), ev.get("uid"), ev.get("nickname"), etype])
    for col_idx in range(1, len(ev_headers) + 1):
        ws2.column_dimensions[get_column_letter(col_idx)].width = 20

    # All exported strings are user data, including text beginning with '=' or '#N/A'.
    # Keep the original value while preventing openpyxl's formula/error inference.
    for sheet in wb:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = 's'

    # Exclusive creation also protects simultaneous exports and existing files.
    suffix = 0
    while True:
        tail = f"_{suffix}" if suffix else ""
        path = os.path.join(dest_dir, f"{basename}{tail}.xlsx")
        try:
            stream = open(path, 'xb')
        except FileExistsError:
            suffix += 1
            continue
        break
    try:
        with stream:
            wb.save(stream)
    except Exception:
        # Only remove the new incomplete file reserved by this export.
        os.unlink(path)
        raise
    logger.info("已导出 Excel: %s", path)
    return path


def export_mysql_schema_sql() -> str:
    """返回用于 MySQL 导入的建表 SQL（预留）。"""
    return """
CREATE TABLE IF NOT EXISTS friends (
    uid VARCHAR(32) PRIMARY KEY,
    nickname VARCHAR(128),
    level INT,
    qianxing_level INT,
    tags TEXT,
    signature VARCHAR(512),
    current_status VARCHAR(16),
    first_seen_at VARCHAR(32),
    last_scan_at VARCHAR(32),
    first_online_at VARCHAR(32),
    total_online_seconds INT DEFAULT 0,
    status_updated_at VARCHAR(32)
) DEFAULT CHARSET=utf8mb4;
"""

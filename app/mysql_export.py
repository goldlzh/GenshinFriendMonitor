"""MySQL 导出：将 SQLite 数据同步到配置的 MySQL。

使用前提：本机已安装 MySQL，且 config.json 中已配置连接信息。
"""
from __future__ import annotations

from typing import List
from dataclasses import dataclass
import re

from app.config import CONFIG
from app.logger import logger


@dataclass(frozen=True)
class ExportResult:
    written: int
    skipped: int


def export_to_mysql(friends: List[dict], *, account_uid: str | None = None) -> ExportResult:
    """Export known friend UIDs, keeping bound accounts in a separate table."""
    import pymysql

    if account_uid is not None and (
            not isinstance(account_uid, str) or re.fullmatch(r'[0-9]{9}', account_uid) is None):
        raise ValueError('导出账号 UID 必须是 9 位数字')
    ready = [f for f in friends if re.fullmatch(r'[0-9]{9}', str(f.get('uid') or ''))]
    skipped = len(friends) - len(ready)
    if not ready:
        return ExportResult(0, skipped)

    conn = pymysql.connect(
        host=CONFIG.mysql_host,
        port=CONFIG.mysql_port,
        user=CONFIG.mysql_user,
        password=CONFIG.mysql_password,
        database=CONFIG.mysql_database,
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        with conn.cursor() as cur:
            table = 'account_friends' if account_uid is not None else 'friends'
            account_column = 'account_uid CHAR(9) NOT NULL,' if account_uid is not None else ''
            uid_column = 'uid VARCHAR(32) NOT NULL,' if account_uid is not None else 'uid VARCHAR(32) PRIMARY KEY,'
            primary_key = ', PRIMARY KEY (account_uid, uid)' if account_uid is not None else ''
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {table} (
                    {account_column}
                    {uid_column}
                    nickname VARCHAR(128),
                    level INT,
                    qianxing_level INT,
                    tags TEXT,
                    qianxing_activity VARCHAR(128),
                    qianxing_activity_seconds DOUBLE DEFAULT 0,
                    qianxing_activity_summary TEXT,
                    qianxing_activity_ranges TEXT,
                    signature VARCHAR(512),
                    current_status VARCHAR(16),
                    total_online_seconds INT DEFAULT 0,
                    first_seen_at VARCHAR(32),
                    last_scan_at VARCHAR(32){primary_key}
                ) DEFAULT CHARSET=utf8mb4"""
            )
            # CREATE IF NOT EXISTS does not add fields to an earlier export.
            # Both table names are fixed above; values remain parameterized.
            cur.execute(
                'SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s',
                (CONFIG.mysql_database, table))
            columns = {row[0] for row in cur.fetchall()}
            for column, declaration in (('qianxing_level', 'INT'), ('tags', 'TEXT'),
                    ('qianxing_activity', 'VARCHAR(128)'), ('qianxing_activity_seconds', 'DOUBLE DEFAULT 0'),
                    ('qianxing_activity_summary', 'TEXT'), ('qianxing_activity_ranges', 'TEXT')):
                if column not in columns:
                    cur.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
            count = 0
            for f in ready:
                values = (f.get("uid"), f.get("nickname"), f.get("level"),
                          f.get("signature"), f.get("current_status"),
                          f.get("total_online_seconds"), f.get("first_seen_at"),
                          f.get("last_scan_at"), f.get("qianxing_level"), f.get("tags"),
                          f.get('qianxing_activity'), f.get('qianxing_activity_seconds') or 0,
                          f.get('qianxing_activity_summary'), f.get('qianxing_activity_ranges'))
                if account_uid is not None:
                    values = (account_uid, *values)
                account_field = 'account_uid,' if account_uid is not None else ''
                placeholders = ','.join(['%s'] * len(values))
                cur.execute(
                    f"""INSERT INTO {table}
                       ({account_field}uid, nickname, level, signature, current_status,
                        total_online_seconds, first_seen_at, last_scan_at, qianxing_level, tags,
                        qianxing_activity, qianxing_activity_seconds, qianxing_activity_summary, qianxing_activity_ranges)
                       VALUES ({placeholders})
                       ON DUPLICATE KEY UPDATE
                         nickname=VALUES(nickname), level=COALESCE(VALUES(level), level),
                         qianxing_level=COALESCE(VALUES(qianxing_level), qianxing_level),
                         tags=COALESCE(VALUES(tags), tags),
                         qianxing_activity=VALUES(qianxing_activity),
                         qianxing_activity_seconds=VALUES(qianxing_activity_seconds),
                         qianxing_activity_summary=VALUES(qianxing_activity_summary),
                         qianxing_activity_ranges=VALUES(qianxing_activity_ranges),
                         signature=VALUES(signature), current_status=VALUES(current_status),
                         total_online_seconds=VALUES(total_online_seconds),
                         first_seen_at=VALUES(first_seen_at), last_scan_at=VALUES(last_scan_at)""",
                    values,
                )
                count += 1
        conn.commit()
        logger.info("已导出 %d 条到 MySQL", count)
        return ExportResult(count, skipped)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

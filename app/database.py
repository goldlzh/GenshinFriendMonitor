"""SQLite 存储层：好友主表、扫描会话、状态事件。

线程安全：所有数据库操作通过一个锁 + 连接复用。

好友匹配策略：
  - 自动扫描使用精确昵称与已确认的精确别名匹配。
  - 新昵称独立登记，手动识别 UID 后再关联或合并档案。
  - id 为本地主键，uid 用于确认身份。
"""
from __future__ import annotations

import difflib
from contextlib import nullcontext
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from app.config import CONFIG
from app.logger import logger
from app.roster_comparison import compare_rosters, valid_uid_roster
from app.status_events import STATUS_CHANGE_LABELS, has_event_identity
from app.membership_events import (classify_additions, migrate_membership_events, resolve_archive_membership,
                                   pending_removals, save_pending_removals, defer_unresolved_removals)
from app.evidence_store import EvidenceStoreMixin, ensure_evidence_schema, evidence_schema_needs_migration

_UNSET = object()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS friends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    uid                 TEXT,               -- 可空，首次资料卡识别后填充
    nickname            TEXT NOT NULL,      -- 列表识别锚点（可能随对方改名而变化）
    nickname_pending    INTEGER NOT NULL DEFAULT 0, -- 昵称以截图待人工核查
    identity_review_key TEXT,               -- 本地待核查组，不是游戏身份
    identity_review_reason TEXT,
    level               INTEGER,
    qianxing_level       INTEGER,
    tags                TEXT,
    signature           TEXT,
    signature_pending INTEGER NOT NULL DEFAULT 0,
    location            TEXT,               -- 在线好友当前所在场景/位置（如「千星奇域」），离线为空
    last_seen           TEXT,               -- 游戏显示的最近登录时间
    current_status      TEXT,               -- online / offline / unknown / NULL
    last_observed_status TEXT,              -- 最后一次真实观察，不由停止/超时改写
    qianxing_activity TEXT,
    qianxing_activity_started_at TEXT,
    qianxing_activity_last_at TEXT,
    first_seen_at       TEXT,               -- 首次扫描到（库内持久）
    last_scan_at        TEXT,               -- 最近一次扫描到
    first_online_at     TEXT,               -- 本次运行首次进入在线
    total_online_seconds INTEGER DEFAULT 0, -- 本次运行累计在线秒数（下线即清零，用于实时显示）
    cumulative_online_seconds INTEGER DEFAULT 0, -- 历史累计在线秒数（跨上下线、跨重启保留）
    status_updated_at   TEXT                -- 最近一次状态变化时间
);
CREATE INDEX IF NOT EXISTS idx_friends_uid ON friends(uid);
CREATE INDEX IF NOT EXISTS idx_friends_nickname ON friends(nickname);

CREATE TABLE IF NOT EXISTS scan_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_time     TEXT,
    total_friends INTEGER,
    online_count  INTEGER,
    offline_count INTEGER
);

CREATE TABLE IF NOT EXISTS status_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    uid        TEXT,
    nickname   TEXT,
    event_type TEXT,     -- observed_online / online / offline / scan_default_offline / added / removed / renamed
    event_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_status_events_uid ON status_events(uid);

-- 每次「上线→下线」的完整时段，用于统计本日上线时间段
CREATE TABLE IF NOT EXISTS online_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    friend_id        INTEGER NOT NULL,
    uid              TEXT,
    nickname         TEXT,
    session_date     TEXT,   -- 上线所属日期 YYYY-MM-DD（按下线时刻所在日期）
    start_time       TEXT,   -- 上线时间（ISO）
    end_time         TEXT,   -- 下线时间（ISO）
    duration_seconds INTEGER
);
CREATE INDEX IF NOT EXISTS idx_online_sessions_friend ON online_sessions(friend_id);
CREATE INDEX IF NOT EXISTS idx_online_sessions_date ON online_sessions(session_date);

-- Explicit interruptions must stay separate even in a minute-resolution display.
-- NULL friend_id applies to the whole monitoring run (stop/failure/restart).
CREATE TABLE IF NOT EXISTS presence_range_breaks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    friend_id INTEGER,
    event_time TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_presence_range_breaks
    ON presence_range_breaks(friend_id, event_time);

CREATE TABLE IF NOT EXISTS qianxing_activity_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    friend_id INTEGER NOT NULL,
    activity TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    duration_seconds REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qianxing_activity_friend
    ON qianxing_activity_sessions(friend_id, activity, start_time);

-- 昵称去重记录（滚动重叠时相似昵称被合并的动作，可撤回）
CREATE TABLE IF NOT EXISTS dedup_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    matched_nickname TEXT,      -- 被合并掉的昵称
    friend_id        INTEGER NOT NULL,  -- 保留的好友 id
    kept_nickname    TEXT,      -- 保留的昵称
    similarity       REAL,      -- 匹配时的相似度
    event_time       TEXT,
    undone           INTEGER DEFAULT 0  -- 是否已撤回（1=已撤回）
);

-- 昵称别名：手动修正昵称后，把旧（被识别错）的昵称记为别名。
-- 别名不在 UI 显示，仅用于把后续识别到的相似昵称归到该好友。
CREATE TABLE IF NOT EXISTS nickname_aliases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    friend_id   INTEGER NOT NULL,
    alias       TEXT NOT NULL,     -- 旧的/被识别错的昵称
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_nickname_aliases_friend ON nickname_aliases(friend_id);
CREATE INDEX IF NOT EXISTS idx_nickname_aliases_alias ON nickname_aliases(alias);

CREATE TABLE IF NOT EXISTS visual_profiles (
    friend_id INTEGER PRIMARY KEY,
    name_image BLOB NOT NULL,
    avatar_image BLOB NOT NULL,
    observed_at TEXT NOT NULL,
    interface_mode TEXT NOT NULL DEFAULT 'normal'
);
CREATE TABLE IF NOT EXISTS profile_captures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    friend_id INTEGER NOT NULL,
    friend_uid TEXT NOT NULL,
    image BLOB NOT NULL,
    observed_at TEXT NOT NULL,
    nickname TEXT
);
CREATE INDEX IF NOT EXISTS idx_profile_captures_friend
    ON profile_captures(friend_id, observed_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS verified_roster (
    position INTEGER PRIMARY KEY,
    friend_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS online_scan_roster (
    friend_id INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS roster_baseline (
    id INTEGER PRIMARY KEY CHECK(id=1),
    scan_time TEXT NOT NULL,
    rows_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roster_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_time TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verified_scan (
    id INTEGER PRIMARY KEY CHECK(id=1),
    scan_time TEXT NOT NULL,
    total INTEGER NOT NULL,
    elapsed_seconds REAL NOT NULL,
    evidence_dir TEXT NOT NULL
);
"""


class Database(EvidenceStoreMixin):
    def __init__(self, db_path: Optional[str] = None):
        self._path = db_path or CONFIG.db_path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.create_function('has_event_identity', 2,
                             lambda uid, nickname: has_event_identity(dict(uid=uid, nickname=nickname)),
                             deterministic=True)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self) -> None:
        with self._lock:
            self._conn = self._connect()
            try:
                tables = {r[0] for r in self._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                cols = {r[1] for r in self._conn.execute("PRAGMA table_info(friends)")}
                needs_migration = ('friends' in tables and
                                   not {'id', 'location', 'last_seen', 'cumulative_online_seconds', 'nickname_pending',
                                        'qianxing_level', 'tags', 'identity_review_key',
                                        'identity_review_reason', 'last_observed_status',
                                        'qianxing_activity', 'qianxing_activity_started_at',
                                        'qianxing_activity_last_at', 'signature_pending'} <= cols)
                needs_migration |= 'friends' not in tables and 'friends_old' in tables
                needs_migration |= 'friends' in tables and 'roster_baseline' not in tables
                needs_migration |= 'friends' in tables and not {'account_metadata', 'profile_captures'} <= tables
                needs_migration |= 'friends' in tables and 'online_scan_roster' not in tables
                needs_migration |= 'friends' in tables and 'qianxing_activity_sessions' not in tables
                needs_migration |= 'friends' in tables and 'presence_range_breaks' not in tables
                event_cols = {r[1] for r in self._conn.execute('PRAGMA table_info(status_events)')}
                needs_migration |= bool(event_cols) and 'friend_id' not in event_cols
                visual_cols = {r[1] for r in self._conn.execute('PRAGMA table_info(visual_profiles)')}
                needs_migration |= bool(visual_cols) and 'interface_mode' not in visual_cols
                needs_migration |= evidence_schema_needs_migration(self._conn)
                if 'uid' in cols:
                    needs_migration |= self._conn.execute("""SELECT 1 FROM friends
                        WHERE uid IS NOT NULL AND uid<>'' GROUP BY uid HAVING COUNT(*)>1 LIMIT 1""").fetchone() is not None
                if needs_migration and self._path != ':memory:':
                    backup_path = Path(str(self._path) + '.backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
                    backup = sqlite3.connect(str(backup_path))
                    try:
                        self._conn.backup(backup)
                    finally:
                        backup.close()
                    logger.info('升级前数据库备份: %s', backup_path)
                self._conn.execute('BEGIN IMMEDIATE')
                self._migrate()
                self._recover_monitoring_locked()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                self._conn.close()
                self._conn = None
                raise

    def _execute_schema(self) -> None:
        # executescript 会隐式提交；逐条执行以使迁移和建表属于同一个事务。
        for statement in _SCHEMA.split(';'):
            if statement.strip():
                self._conn.execute(statement)

    def _copy_legacy(self, table: str) -> None:
        old_cols = {r[1] for r in self._conn.execute(f'PRAGMA table_info("{table}")')}
        cols = [r[1] for r in self._conn.execute('PRAGMA table_info(friends)')
                if r[1] in old_cols and r[1] != 'id']
        names = ', '.join('"' + c + '"' for c in cols)
        self._conn.execute(f'INSERT INTO friends ({names}) SELECT {names} FROM "{table}"')
        old_count = self._conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        new_count = self._conn.execute('SELECT COUNT(*) FROM friends').fetchone()[0]
        if old_count != new_count:
            raise RuntimeError('旧档案数量校验失败，迁移已回滚')

    def _migrate(self) -> None:
        tables = {r[0] for r in self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in self._conn.execute('PRAGMA table_info(friends)')}
        legacy = None
        if cols and 'id' not in cols:
            legacy = 'friends_old'
            suffix = 1
            while legacy in tables:
                legacy = f'friends_legacy_backup_{suffix}'
                suffix += 1
            self._conn.execute(f'ALTER TABLE friends RENAME TO "{legacy}"')
            cols = set()
        elif not cols and 'friends_old' in tables:
            # 兼容旧版本已改表名、尚未建新表就中断的数据库。
            legacy = 'friends_old'

        if cols:
            for name, declaration in (
                ('cumulative_online_seconds', 'INTEGER DEFAULT 0'),
                ('location', 'TEXT'), ('last_seen', 'TEXT'),
                ('nickname_pending', 'INTEGER NOT NULL DEFAULT 0'),
                ('qianxing_level', 'INTEGER'), ('tags', 'TEXT'),
                ('identity_review_key', 'TEXT'), ('identity_review_reason', 'TEXT'),
                ('last_observed_status', 'TEXT'), ('qianxing_activity', 'TEXT'),
                ('qianxing_activity_started_at', 'TEXT'), ('qianxing_activity_last_at', 'TEXT'),
                ('signature_pending', 'INTEGER NOT NULL DEFAULT 0'),
            ):
                if name not in cols:
                    self._conn.execute(f'ALTER TABLE friends ADD COLUMN {name} {declaration}')
        self._execute_schema()
        if 'presence_range_breaks' not in tables:
            legacy_event_id = self._conn.execute('SELECT COALESCE(MAX(id),0) FROM status_events').fetchone()[0]
            self._set_metadata_locked('presence_legacy_event_id', str(legacy_event_id))
        visual_cols = {r[1] for r in self._conn.execute('PRAGMA table_info(visual_profiles)')}
        if 'interface_mode' not in visual_cols:
            self._conn.execute("ALTER TABLE visual_profiles ADD COLUMN interface_mode TEXT NOT NULL DEFAULT 'normal'")
        if legacy:
            self._copy_legacy(legacy)
        # 旧表重命名会保留旧索引名，因此用新名字确保索引建立在当前表上。
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_current_friends_uid ON friends(uid)')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_current_friends_nickname ON friends(nickname)')
        self._conn.execute("""DELETE FROM nickname_aliases WHERE id NOT IN
            (SELECT MIN(id) FROM nickname_aliases GROUP BY friend_id, alias)""")
        self._conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_alias_pair ON nickname_aliases(friend_id, alias)')
        ensure_evidence_schema(self._conn)
        migrate_membership_events(self._conn)
        for group in self._conn.execute("""SELECT uid FROM friends WHERE uid IS NOT NULL AND uid<>''
                GROUP BY uid HAVING COUNT(*)>1""").fetchall():
            ids = [r[0] for r in self._conn.execute('SELECT id FROM friends WHERE uid=? ORDER BY id', (group[0],))]
            for other_id in ids[1:]:
                self._merge_friend_locked(ids[0], other_id)
        self._conn.execute('PRAGMA user_version=9')

    @property
    def monitoring_interval_seconds(self):
        """None is a snapshot; zero is continuous; positive values expire."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM account_metadata WHERE key='presence_interval_seconds'").fetchone()
            return json.loads(row[0]) if row else None

    def _set_metadata_locked(self, key, value):
        self._conn.execute('INSERT OR REPLACE INTO account_metadata(key,value) VALUES (?,?)',
                           (key, value))

    def _presence_end_locked(self, row, at):
        finish = datetime.fromisoformat(_time_text(at))
        if row['first_online_at'] and row['last_scan_at']:
            last = datetime.fromisoformat(row['last_scan_at'])
            interval = self.monitoring_interval_seconds
            if interval is None:
                finish = min(finish, last)
            elif interval > 0:
                finish = min(finish, last + timedelta(seconds=interval))
            finish = max(last, finish)
        return finish.isoformat()

    def _finish_presence_locked(self, row, at, *, status='offline', event_type='scan_default_offline'):
        """Settle one session without pretending a failure was an observation."""
        if row['current_status'] != 'online':
            return False
        self._finish_activity_locked(row, at)
        finish = self._presence_end_locked(row, at)
        cumulative = row['cumulative_online_seconds'] or 0
        if row['first_online_at'] and row['last_scan_at']:
            cumulative = round(cumulative + _elapsed_seconds(row['last_scan_at'], finish), 6)
            duration = _elapsed_seconds(row['first_online_at'], finish)
            if duration:
                self._conn.execute('''INSERT INTO online_sessions
                    (friend_id,uid,nickname,session_date,start_time,end_time,duration_seconds)
                    VALUES (?,?,?,?,?,?,?)''',
                    (row['id'], row['uid'], row['nickname'], finish[:10],
                     row['first_online_at'], finish, duration))
        self._conn.execute('''UPDATE friends SET current_status=?,location=NULL,
            first_online_at=NULL,total_online_seconds=0,cumulative_online_seconds=?,
            status_updated_at=? WHERE id=?''', (status, cumulative, finish, row['id']))
        if event_type:
            self._conn.execute('INSERT INTO status_events(uid,nickname,event_type,event_time) VALUES (?,?,?,?)',
                               (row['uid'], row['nickname'], event_type, finish))
        return True

    def _recover_monitoring_locked(self):
        # A process restart is not evidence that the game stayed online. Keep
        # confirmed history, but never charge the unattended gap after a crash.
        if self.monitoring_interval_seconds is not None:
            for row in self._conn.execute("SELECT * FROM friends WHERE current_status='online'").fetchall():
                at = row['last_scan_at'] or now_str()
                self._range_break_locked(at, 'restart', row['id'])
                self._finish_presence_locked(row, at)
            self._range_break_locked(now_str(), 'restart')
            self._set_metadata_locked('presence_interval_seconds', 'null')
            self._set_metadata_locked('presence_expires_at', '')

    def _range_break_locked(self, at, reason, fid=None):
        self._conn.execute('INSERT INTO presence_range_breaks(friend_id,event_time,reason) VALUES (?,?,?)',
                           (fid, at, reason))

    def begin_observation_session(self):
        """First observations after starting are baselines, not transitions.

        Keep presence sessions, durations, membership and event history intact.
        Only a later opposite observation within this run proves a transition.
        """
        with self._lock, self._conn:
            self._conn.execute('UPDATE friends SET last_observed_status=NULL')

    def _settle_unobserved_locked(self, row, at):
        """An absent card ends timing but cannot prove a game logout."""
        if self._finish_presence_locked(row, at):
            self._range_break_locked(at, 'unobserved', row['id'])

    def configure_monitoring(self, interval_seconds, *, at=None):
        if interval_seconds is not None:
            if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float)):
                raise ValueError('监测间隔必须是非负秒数或空值')
            if not math.isfinite(interval_seconds) or interval_seconds < 0:
                raise ValueError('监测间隔必须是非负秒数或空值')
            interval_seconds = float(interval_seconds)
        at = _time_text(at)
        with self._lock:
            # Account import closes the old connection before replacing its
            # scanner; repeated stop is safe after close has already settled it.
            if self._conn is None:
                if interval_seconds is None:
                    return
                raise RuntimeError('数据库已关闭，无法启动监测')
            with self._conn:
                if self.monitoring_interval_seconds == interval_seconds:
                    return
                self._range_break_locked(at, 'configuration')
                for row in self._conn.execute("SELECT * FROM friends WHERE current_status='online'").fetchall():
                    self._finish_presence_locked(row, at)
                self._set_metadata_locked('presence_interval_seconds', json.dumps(interval_seconds))
                self._set_metadata_locked('presence_last_success_at', '')
                self._set_metadata_locked('presence_expires_at', '')

    def _record_presence_success_locked(self, at):
        self._set_metadata_locked('presence_last_success_at', at)
        interval = self.monitoring_interval_seconds
        deadline = ((datetime.fromisoformat(at)+timedelta(seconds=interval)).isoformat()
                    if interval is not None and interval > 0 else '')
        self._set_metadata_locked('presence_expires_at', deadline)

    def expire_presence(self, now=None):
        at = _time_text(now)
        with self._lock, self._conn:
            interval = self.monitoring_interval_seconds
            if interval is None or interval == 0:
                return 0
            changed = 0
            for row in self._conn.execute("SELECT * FROM friends WHERE current_status='online'").fetchall():
                if (row['first_online_at'] and row['last_scan_at'] and
                        _parse_time(at) >= _parse_time(row['last_scan_at'])+interval):
                    changed += self._finish_presence_locked(row, at)
            return changed

    def mark_scan_failed(self, at=None):
        at = _time_text(at)
        with self._lock, self._conn:
            self._range_break_locked(at, 'scan_failed')
            changed = sum(self._finish_presence_locked(row, at) for row in
                          self._conn.execute("SELECT * FROM friends WHERE current_status='online'").fetchall())
            self._set_metadata_locked('presence_expires_at', '')
            return changed

    def _presence_row_locked(self, row, at):
        """Pure read projection; callers can safely use it inside a transaction."""
        result = dict(row)
        activity = self._activity_stats_locked(row, at)
        result.update(qianxing_activity=activity['current'],
                      qianxing_activity_seconds=activity['total_seconds'],
                      qianxing_activity_summary=activity['summary'],
                      qianxing_activity_ranges=activity['ranges'])
        interval = self.monitoring_interval_seconds
        active = (interval is not None and row['current_status'] == 'online'
                  and bool(row['first_online_at']) and bool(row['last_scan_at']))
        result['presence_monitoring'] = bool(active)
        result['presence_expires_at'] = None
        if not active:
            result['first_online_at'] = None
            result['total_online_seconds'] = 0
            return result
        finish = self._presence_end_locked(row, at)
        tail = _elapsed_seconds(row['last_scan_at'], finish)
        result['total_online_seconds'] = round((row['total_online_seconds'] or 0)+tail, 6)
        result['cumulative_online_seconds'] = round((row['cumulative_online_seconds'] or 0)+tail, 6)
        if interval > 0:
            deadline = datetime.fromisoformat(row['last_scan_at'])+timedelta(seconds=interval)
            result['presence_expires_at'] = deadline.isoformat()
            if datetime.fromisoformat(at) >= deadline:
                result.update(current_status='offline', location=None, first_online_at=None,
                              total_online_seconds=0, presence_monitoring=False,
                              status_updated_at=result['presence_expires_at'])
        return result

    def _activity_end_locked(self, row, at):
        finish = datetime.fromisoformat(_time_text(at))
        last = row['qianxing_activity_last_at']
        if last:
            last = datetime.fromisoformat(last)
            interval = self.monitoring_interval_seconds
            if interval is None:
                finish = min(finish, last)
            elif interval > 0:
                finish = min(finish, last + timedelta(seconds=interval))
            finish = max(last, finish)
        return finish.isoformat()

    def _store_activity_interval_locked(self, fid, activity, start, finish):
        if activity and start and finish and finish > start:
            self._conn.execute('''INSERT INTO qianxing_activity_sessions
                (friend_id,activity,start_time,end_time,duration_seconds) VALUES (?,?,?,?,?)''',
                (fid, activity, start, finish, _elapsed_seconds(start, finish)))

    def _finish_activity_locked(self, row, at):
        self._store_activity_interval_locked(row['id'], row['qianxing_activity'],
            row['qianxing_activity_started_at'], self._activity_end_locked(row, at))
        self._conn.execute('''UPDATE friends SET qianxing_activity=NULL,
            qianxing_activity_started_at=NULL,qianxing_activity_last_at=NULL WHERE id=?''', (row['id'],))

    def _update_activity_locked(self, fid, activity, at):
        row = self._conn.execute('SELECT * FROM friends WHERE id=?', (fid,)).fetchone()
        activity = activity.strip() if isinstance(activity, str) else None
        activity = activity or None
        interval = self.monitoring_interval_seconds
        stale = (row['qianxing_activity_started_at'] and interval is not None and interval > 0
                 and _parse_time(at) > _parse_time(row['qianxing_activity_last_at'])+interval)
        if row['qianxing_activity'] != activity or stale:
            self._finish_activity_locked(row, at)
            start = at if activity and interval is not None else None
        else:
            start = row['qianxing_activity_started_at']
            if activity and interval is not None and not start:
                start = at
        self._conn.execute('''UPDATE friends SET qianxing_activity=?,
            qianxing_activity_started_at=?,qianxing_activity_last_at=? WHERE id=?''',
            (activity, start if activity else None, at if activity else None, fid))

    def _activity_stats_locked(self, row, at, date=None):
        current_time = datetime.fromisoformat(at)
        day = datetime.fromisoformat(date or current_time.strftime('%Y-%m-%d'))
        day_end = day+timedelta(days=1)
        groups = {}
        for session in self._conn.execute('''SELECT activity,start_time,end_time
                FROM qianxing_activity_sessions WHERE friend_id=?''', (row['id'],)):
            groups.setdefault(session['activity'], []).append((session['start_time'], session['end_time']))
        current = row['qianxing_activity'] if row['current_status'] == 'online' else None
        interval = self.monitoring_interval_seconds
        active = bool(current and row['qianxing_activity_started_at'] and interval is not None)
        if active:
            finish = self._activity_end_locked(row, at)
            groups.setdefault(current, []).append((row['qianxing_activity_started_at'], finish))
            if interval > 0 and _parse_time(at) >= _parse_time(row['qianxing_activity_last_at'])+interval:
                current, active = None, False
        by_activity, all_intervals = {}, []
        for name, intervals in sorted(groups.items()):
            merged = _union_intervals(intervals)
            all_intervals.extend(merged)
            seconds = round(sum((end-start).total_seconds() for start, end in merged), 6)
            ranges = []
            for start, finish in merged:
                start, finish = max(day, start), min(day_end, finish)
                if finish <= start:
                    continue
                ongoing = active and name == current and finish == current_time
                label = '进行中' if ongoing else ('24:00' if finish == day_end else finish.strftime('%H:%M'))
                ranges.append(start.strftime('%H:%M')+'-'+label)
            by_activity[name] = {'seconds': seconds, 'today_ranges': ranges}
        if current:
            by_activity.setdefault(current, {'seconds': 0., 'today_ranges': []})
        total = round(sum((end-start).total_seconds() for start, end in _union_intervals(all_intervals)), 6)
        summary = '；'.join('%s：%s' % (name, _duration_text(value['seconds']))
                           for name, value in by_activity.items())
        ranges = '；'.join('%s：%s' % (name, '、'.join(value['today_ranges']))
                          for name, value in by_activity.items() if value['today_ranges'])
        return {'current': current, 'total_seconds': total, 'by_activity': by_activity,
                'summary': summary, 'ranges': ranges}

    def get_activity_stats(self, fid, *, now=None, date=None):
        """Verified named activity durations; projection never mutates stored history."""
        with self._lock:
            row = self._conn.execute('SELECT * FROM friends WHERE id=?', (fid,)).fetchone()
            return self._activity_stats_locked(row, _time_text(now), date) if row else None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def account_uid(self) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM account_metadata WHERE key='account_uid'").fetchone()
            return row[0] if row else None

    def set_account_uid(self, uid: str) -> None:
        """Bind the database once to the game account that owns its roster."""
        if not isinstance(uid, str) or re.fullmatch(r'[0-9]{9}', uid) is None:
            raise ValueError('账号 UID 必须是 9 位数字')
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO account_metadata(key,value) VALUES ('account_uid',?)", (uid,))
            if self.account_uid != uid:
                raise ValueError('数据库所属账号 UID 不匹配，不能混用好友名单')

    def find_by_uid(self, uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM friends WHERE uid = ? LIMIT 1", (uid,)
            ).fetchone()
            return dict(row) if row else None

    def find_by_nickname(self, nickname: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM friends WHERE nickname = ? ORDER BY id DESC LIMIT 1",
                (nickname,),
            ).fetchone()
            return dict(row) if row else None

    def find_by_nickname_fuzzy(self, nickname: str, threshold: float):
        """模糊匹配昵称：先归一化后精确匹配，再按相似度匹配。

        返回 (row, similarity)；未命中返回 (None, 最高相似度)。
        用于滚动重叠时同一好友被 OCR 识别出略有差异的昵称。
        """
        norm = _normalize_nickname(nickname)
        if not norm:
            return None, 0.0
        best = None
        best_sim = 0.0
        with self._lock:
            rows = self._conn.execute("SELECT * FROM friends").fetchall()
            all_rows = [dict(r) for r in rows]
        for r in all_rows:
            rn = _normalize_nickname(r.get("nickname") or "")
            if not rn:
                continue
            if rn == norm:
                return r, 1.0
            sim = _similarity(norm, rn)
            if sim > best_sim:
                best_sim = sim
                best = r
        if best is not None and best_sim >= threshold:
            return best, best_sim
        return None, best_sim

    def find_friend_by_alias(self, nickname: str, threshold: float = 1.0):
        """仅匹配人工确认过的精确别名；有歧义时不自动归档。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT DISTINCT f.* FROM nickname_aliases a
                   JOIN friends f ON f.id=a.friend_id WHERE a.alias=?""",
                (nickname.strip(),),
            ).fetchall()
            if len(rows) == 1:
                return dict(rows[0]), 1.0
        return None, 0.0

    def add_nickname_alias(self, friend_id: int, alias: str, now: str) -> None:
        """记录昵称别名（手动修正时的旧昵称）。重复的 (friend_id, alias) 自动忽略。"""
        alias = (alias or "").strip()
        if not alias:
            return
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO nickname_aliases (friend_id, alias, created_at) VALUES (?, ?, ?)",
                (friend_id, alias, now),
            )
            self._conn.commit()

    def get_friend(self, fid: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM friends WHERE id = ?", (fid,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_friends(self, *, now=None) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM friends ORDER BY current_status DESC, nickname"
            ).fetchall()
            return [self._presence_row_locked(r, _time_text(now)) for r in rows]

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def _record_friend_event_locked(self, fid, kind, now, *, uid=None, nickname=None):
        row = self.get_friend(fid)
        if row is None:
            return
        identity = dict(uid=uid if uid is not None else row['uid'],
                        nickname=nickname if nickname is not None else row['nickname'])
        if kind in STATUS_CHANGE_LABELS and not has_event_identity(identity):
            return
        self._conn.execute('''INSERT INTO status_events(friend_id,uid,nickname,event_type,event_time)
            VALUES (?,?,?,?,?)''', (fid, identity['uid'], identity['nickname'], kind, now))

    def add_friend(self, uid: Optional[str], nickname: str, level: Optional[int],
                   signature: Optional[str], status: str, now: str,
                   location: Optional[str] = None, last_seen: Optional[str] = None,
                   *, commit: bool = True, qianxing_level: Optional[int] = None,
                   tags: Optional[str] = None, qianxing_activity=None) -> int:
        """Create a local archive, not evidence that a game friend was added."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO friends
                   (uid, nickname, level, signature, location, current_status,
                     first_seen_at, last_scan_at, first_online_at,
                     total_online_seconds, status_updated_at, last_seen, qianxing_level, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uid, nickname, level, signature, location, status,
                 now, now, now if status == "online" and self.monitoring_interval_seconds is not None else None,
                 0, now, last_seen, qianxing_level, tags),
            )
            self._conn.execute('UPDATE friends SET last_observed_status=? WHERE id=?',
                               (status if status in ('online', 'offline') else None, cur.lastrowid))
            self._update_activity_locked(cur.lastrowid, qianxing_activity if status == 'online' else None, now)
            self._record_friend_event_locked(cur.lastrowid, 'observed_friend', now)
            if status == "online":
                self._conn.execute(
                    "INSERT INTO status_events (uid, nickname, event_type, event_time) VALUES (?, ?, 'observed_online', ?)",
                    (uid, nickname, now),
                )
            if commit:
                self._conn.commit()
            return cur.lastrowid

    def update_status(self, fid: int, status: str, nickname: Optional[str],
                      level: Optional[int], now: str,
                      location: Optional[str] = None, last_seen: Optional[str] = None,
                      *, commit: bool = True, qianxing_activity=None) -> None:
        """Record an observation; only explicitly enabled monitoring earns time."""
        now = _time_text(now)
        with self._lock:
            row = self._conn.execute('SELECT * FROM friends WHERE id=?', (fid,)).fetchone()
            if row is None:
                return
            interval = self.monitoring_interval_seconds
            if (row['current_status'] == 'online' and row['first_online_at'] and
                    row['last_scan_at'] and interval is not None and interval > 0
                    and _parse_time(now) > _parse_time(row['last_scan_at'])+interval):
                self._finish_presence_locked(row, now)
                row = self._conn.execute('SELECT * FROM friends WHERE id=?', (fid,)).fetchone()
            prev_status = row['current_status']
            observed_before = row['last_observed_status']
            if status != 'online' and (prev_status == 'online' or observed_before == 'online'):
                self._range_break_locked(now, status, fid)
            if status != 'online' and prev_status == 'online':
                self._finish_presence_locked(row, now, status='unknown' if status == 'unknown' else 'offline',
                                             event_type='unknown' if status == 'unknown' else None)
                row = self._conn.execute('SELECT * FROM friends WHERE id=?', (fid,)).fetchone()
            self._update_activity_locked(fid, qianxing_activity if status == 'online' else None, now)
            total = row['total_online_seconds'] or 0
            cumulative = row['cumulative_online_seconds'] or 0
            first = row['first_online_at']
            if status == 'online' and interval is not None:
                if first and row['current_status'] == 'online' and row['last_scan_at']:
                    delta = _elapsed_seconds(row['last_scan_at'], now)
                    # SQLite's integer affinity keeps nonintegral values as
                    # REAL; do not discard each subsecond observation's tail.
                    total = round(total+delta, 6)
                    cumulative = round(cumulative+delta, 6)
                else:
                    first, total = now, 0
            else:
                first, total = None, 0
            self._conn.execute("""UPDATE friends SET
                nickname=COALESCE(?,nickname),level=COALESCE(?,level),location=?,
                current_status=?,last_scan_at=?,first_online_at=?,total_online_seconds=?,
                cumulative_online_seconds=?,status_updated_at=?,last_seen=?,last_observed_status=? WHERE id=?""",
                (nickname, level, location if status == 'online' else None, status,
                 now, first, total, cumulative,
                 now if prev_status != status else row['status_updated_at'],
                 None if status == 'online' else (last_seen or row['last_seen']),
                 status if status in ('online', 'offline') else None, fid))
            event = None
            if status == 'online' and (prev_status != 'online' or observed_before != 'online'):
                event = 'online' if observed_before == 'offline' else 'observed_online'
            elif status == 'offline' and observed_before == 'online':
                event = 'offline'
            if event:
                self._record_friend_event_locked(fid, event, now, nickname=nickname or row['nickname'])
            if commit:
                self._conn.commit()

    def _observed_intervals(self, row):
        intervals = [(r[0], r[1]) for r in self._conn.execute(
            'SELECT start_time, end_time FROM online_sessions WHERE friend_id=?', (row['id'],))]
        if row['current_status'] == 'online' and row['first_online_at'] and row['last_scan_at']:
            intervals.append((row['first_online_at'], row['last_scan_at']))
        return intervals

    def _merge_friend_locked(self, keep_id: int, other_id: int) -> None:
        keep = dict(self._conn.execute('SELECT * FROM friends WHERE id=?', (keep_id,)).fetchone())
        other = dict(self._conn.execute('SELECT * FROM friends WHERE id=?', (other_id,)).fetchone())
        if keep['uid'] and other['uid'] and keep['uid'] != other['uid']:
            raise ValueError('不同 UID 的好友不能合并')
        resolve_archive_membership(self._conn, keep, other)
        pending = pending_removals(self._conn)
        if pending:
            for row in pending:
                if row['id'] == other_id:
                    row.update(id=keep_id, uid=keep['uid'] or other['uid'])
                if row.get('identity_candidate_ids'):
                    row['identity_candidate_ids'] = sorted({keep_id if fid == other_id else fid
                                                           for fid in row['identity_candidate_ids']})
            save_pending_removals(self._conn, list({row['id']: row for row in pending}.values()))
        self._conn.execute('UPDATE status_events SET friend_id=? WHERE friend_id=?', (keep_id, other_id))
        overlap = _overlap_seconds(self._observed_intervals(keep), self._observed_intervals(other))
        a, b = keep['cumulative_online_seconds'] or 0, other['cumulative_online_seconds'] or 0
        cumulative = max(a, b, a + b - overlap)
        by_recency = sorted((keep, other), key=lambda r: (r['last_scan_at'] or r['first_seen_at'] or '', r['id']), reverse=True)
        latest = by_recency[0]
        named = next((r for r in by_recency if r['nickname'] and not r['nickname_pending']), latest)
        signature_row = next((r for r in by_recency if not r['signature_pending'] and r['signature'] is not None), latest)
        qianxing_level = next((r['qianxing_level'] for r in by_recency if r['qianxing_level'] is not None), None)
        tags = next((r['tags'] for r in by_recency if r['tags'] is not None), None)
        total = latest['total_online_seconds'] or 0
        first_online = latest['first_online_at']
        if keep['current_status'] == other['current_status'] == 'online':
            active_overlap = _overlap_seconds(
                [(keep['first_online_at'], keep['last_scan_at'])],
                [(other['first_online_at'], other['last_scan_at'])])
            total = max(total, (keep['total_online_seconds'] or 0) +
                        (other['total_online_seconds'] or 0) - active_overlap)
            first_online = min(filter(None, (keep['first_online_at'], other['first_online_at'])), default=None)
        first_seen = min(filter(None, (keep['first_seen_at'], other['first_seen_at'])), default=None)
        self._conn.execute("""UPDATE friends SET first_seen_at=?, last_scan_at=?, first_online_at=?,
            current_status=?, total_online_seconds=?, cumulative_online_seconds=?,
            status_updated_at=?, level=?, signature=?, location=?, last_seen=?, qianxing_level=?, tags=?,
            last_observed_status=?,signature_pending=?,nickname=?,nickname_pending=?,uid=?,
            identity_review_key=NULL,identity_review_reason=NULL WHERE id=?""",
            (first_seen, latest['last_scan_at'], first_online, latest['current_status'], total,
             cumulative, latest['status_updated_at'], latest['level'] or keep['level'] or other['level'],
             signature_row['signature'], latest['location'], latest['last_seen'],
             qianxing_level, tags, latest['last_observed_status'], signature_row['signature_pending'],
             named['nickname'], named['nickname_pending'], keep['uid'] or other['uid'], keep_id))
        for source in (keep, other):
            if source['nickname'] and source['nickname'] != named['nickname']:
                self._conn.execute('INSERT OR IGNORE INTO nickname_aliases(friend_id,alias,created_at) VALUES (?,?,?)',
                                   (keep_id, source['nickname'], latest['last_scan_at'] or latest['first_seen_at']))
        # Keep both confirmed histories; projection unions overlaps so merging
        # aliases never counts the same named activity twice.
        for source in (keep, other):
            self._store_activity_interval_locked(source['id'], source['qianxing_activity'],
                source['qianxing_activity_started_at'], source['qianxing_activity_last_at'])
        self._conn.execute('UPDATE qianxing_activity_sessions SET friend_id=? WHERE friend_id=?',
                           (keep_id, other_id))
        self._conn.execute('''UPDATE friends SET qianxing_activity=?,
            qianxing_activity_started_at=?,qianxing_activity_last_at=? WHERE id=?''',
            (latest['qianxing_activity'], latest['qianxing_activity_started_at'],
             latest['qianxing_activity_last_at'], keep_id))
        self._conn.execute('UPDATE online_sessions SET friend_id=?, uid=? WHERE friend_id=?',
                           (keep_id, keep['uid'] or other['uid'], other_id))
        self._conn.execute('UPDATE presence_range_breaks SET friend_id=? WHERE friend_id=?',
                           (keep_id, other_id))
        self._conn.execute("""DELETE FROM online_sessions WHERE friend_id=? AND id NOT IN
            (SELECT MIN(id) FROM online_sessions WHERE friend_id=? GROUP BY start_time, end_time)""",
            (keep_id, keep_id))
        self._conn.execute("""INSERT OR IGNORE INTO nickname_aliases(friend_id,alias,created_at)
            SELECT ?,alias,created_at FROM nickname_aliases WHERE friend_id=?""", (keep_id, other_id))
        self._conn.execute('DELETE FROM nickname_aliases WHERE friend_id=?', (other_id,))
        self._conn.execute('UPDATE dedup_log SET friend_id=? WHERE friend_id=?', (keep_id, other_id))
        self._conn.execute('UPDATE verified_roster SET friend_id=? WHERE friend_id=?', (keep_id, other_id))
        snapshot = self._conn.execute('SELECT rows_json FROM roster_baseline WHERE id=1').fetchone()
        if snapshot:
            rows = json.loads(snapshot['rows_json'])
            for row in rows:
                if row['id'] == other_id:
                    row.update(id=keep_id, uid=keep['uid'] or other['uid'],
                               identity_review_key=None, identity_review_reason=None, identity_candidate_ids=[])
                elif row.get('identity_candidate_ids'):
                    row['identity_candidate_ids'] = sorted({keep_id if fid == other_id else fid
                                                           for fid in row['identity_candidate_ids']})
            self._conn.execute('UPDATE roster_baseline SET rows_json=? WHERE id=1',
                               (json.dumps(rows, ensure_ascii=False),))
        self._conn.execute('INSERT OR IGNORE INTO online_scan_roster SELECT ? FROM online_scan_roster WHERE friend_id=?',
                           (keep_id, other_id))
        self._conn.execute('DELETE FROM online_scan_roster WHERE friend_id=?', (other_id,))
        self._conn.execute('''INSERT INTO visual_profiles
            SELECT ?,name_image,avatar_image,observed_at,interface_mode FROM visual_profiles WHERE friend_id=?
            ON CONFLICT(friend_id) DO UPDATE SET name_image=excluded.name_image,avatar_image=excluded.avatar_image,
                observed_at=excluded.observed_at,interface_mode=excluded.interface_mode
            WHERE excluded.observed_at>visual_profiles.observed_at''', (keep_id, other_id))
        self._conn.execute('DELETE FROM visual_profiles WHERE friend_id=?', (other_id,))
        self._merge_evidence_locked(keep_id, other_id, keep['uid'] or other['uid'])
        self._conn.execute('DELETE FROM friends WHERE id=?', (other_id,))

    def resolve_uid(self, uid: str, nickname: str, now: str) -> int:
        """用 UID 原子地绑定/合并身份，保留不同 UID 的同名好友和历史时段。"""
        if re.fullmatch(r'[0-9]{9}', uid or '') is None:
            raise ValueError('UID 必须是 9 位数字')
        nickname = nickname.strip()
        if not nickname:
            raise ValueError('昵称不能为空')
        with self._lock, self._conn:
            uid_rows = self._conn.execute('SELECT * FROM friends WHERE uid=? ORDER BY id', (uid,)).fetchall()
            pending = self._conn.execute(
                "SELECT * FROM friends WHERE nickname=? AND (uid IS NULL OR uid='') ORDER BY id",
                (nickname,)).fetchall()
            if not uid_rows and not pending:
                return self.add_friend(uid, nickname, None, None, None, now)
            keep = uid_rows[0] if uid_rows else pending[0]
            fid = keep['id']
            for other in [*uid_rows[1:], *pending]:
                if other['id'] != fid:
                    self._merge_friend_locked(fid, other['id'])
            old_name = keep['nickname']
            self._conn.execute('UPDATE friends SET uid=?, nickname=? WHERE id=?', (uid, nickname, fid))
            self._conn.execute('UPDATE online_sessions SET uid=? WHERE friend_id=?', (uid, fid))
            if pending or not keep['uid']:
                self._record_friend_event_locked(fid, 'uid_completed', now)
            if old_name != nickname:
                self._conn.execute('INSERT OR IGNORE INTO nickname_aliases(friend_id,alias,created_at) VALUES (?,?,?)',
                                   (fid, old_name, now))
                self._conn.execute("INSERT INTO status_events(uid,nickname,event_type,event_time) VALUES (?,?,'renamed',?)",
                                   (uid, nickname, now))
            return fid

    def bind_uid(self, fid: int, uid: str, now: str) -> int:
        row = self.get_friend(fid)
        if row is None:
            raise ValueError('好友记录不存在')
        return self.resolve_uid(uid, row['nickname'], now)

    def rename_friend(self, fid: int, new_nickname: str, now: str) -> None:
        """改名（对方修改昵称后，通过 UID 兜底识别到）。"""
        with self._lock:
            row = self._conn.execute("SELECT * FROM friends WHERE id = ?", (fid,)).fetchone()
            if row is None:
                return
            old = row["nickname"]
            if old == new_nickname:
                return
            self._conn.execute(
                "UPDATE friends SET nickname=? WHERE id=?",
                (new_nickname, fid),
            )
            self._conn.execute(
                "INSERT INTO status_events (uid, nickname, event_type, event_time) VALUES (?, ?, 'renamed', ?)",
                (row["uid"], new_nickname, now),
            )
            self._conn.commit()
            logger.info("好友改名: %s -> %s", old, new_nickname)

    def mark_removed(self, fid: int, now: str) -> None:
        """完整扫描连续确认消失后，结算在线时段并保留档案。"""
        with self._lock:
            row = self.get_friend(fid)
            if row is None:
                return
            self._settle_unobserved_locked(row, now)
            self._record_friend_event_locked(fid, 'removed', now)
            self._conn.commit()

    # ------------------------------------------------------------------
    # 手动修正（UI）
    # ------------------------------------------------------------------
    def update_friend_manual(self, fid: int, uid: Optional[str], nickname: str,
                             level: Optional[int], signature: Optional[str], *,
                             qianxing_level=_UNSET, tags=_UNSET, signature_confirmed=None,
                             commit=True) -> None:
        nickname = nickname.strip()
        if uid and re.fullmatch(r'[0-9]{9}', uid) is None:
            raise ValueError('UID 必须是 9 位数字')
        with self._lock, (self._conn if commit else nullcontext()):
            current = self._conn.execute('SELECT * FROM friends WHERE id=?', (fid,)).fetchone()
            if current is None:
                raise ValueError('好友记录不存在')
            pending = bool(current['nickname_pending'] and not nickname)
            if not nickname and not pending:
                raise ValueError('昵称不能为空')
            old_names = {current['nickname']}
            if uid:
                existing = self._conn.execute('SELECT * FROM friends WHERE uid=? ORDER BY id LIMIT 1', (uid,)).fetchone()
                if existing and existing['id'] != fid:
                    old_names.add(existing['nickname'])
                    self._merge_friend_locked(existing['id'], fid)
                    fid = existing['id']
            self._conn.execute('''UPDATE friends SET uid=?, nickname=?, level=?, signature=?,
                nickname_pending=?,signature_pending=? WHERE id=?''',
                (uid, nickname, level, signature, int(pending), current['signature_pending'], fid))
            signature_is_confirmed = (signature_confirmed is True or
                                      (signature_confirmed is None and bool(signature)))
            if nickname:
                self.clear_field_pending(fid, 'nickname', corrected_text=nickname, commit=False)
            if signature_is_confirmed:
                self.clear_field_pending(fid, 'signature', corrected_text=signature or '', commit=False)
            if nickname or uid:
                self._conn.execute('UPDATE friends SET identity_review_key=NULL, identity_review_reason=NULL WHERE id=?',
                                   (fid,))
            if qianxing_level is not _UNSET:
                self._conn.execute('UPDATE friends SET qianxing_level=? WHERE id=?', (qianxing_level, fid))
            if tags is not _UNSET:
                self._conn.execute('UPDATE friends SET tags=? WHERE id=?', (tags, fid))
            self._prune_profile_captures_locked(fid, uid)
            self._conn.execute('UPDATE online_sessions SET uid=? WHERE friend_id=?', (uid, fid))
            if uid and not current['uid']:
                self._record_friend_event_locked(fid, 'uid_completed', now_str())
            for old_name in old_names - {nickname}:
                if old_name:
                    self._conn.execute('INSERT OR IGNORE INTO nickname_aliases(friend_id,alias,created_at) VALUES (?,?,?)',
                                       (fid, old_name, now_str()))

            identity_confirmed = bool(current['nickname_pending'] or current['identity_review_key']
                                      or current['uid'] != uid)
            if identity_confirmed or signature_is_confirmed or qianxing_level is not _UNSET or tags is not _UNSET:
                baseline = self._conn.execute('SELECT rows_json FROM roster_baseline WHERE id=1').fetchone()
                if baseline:
                    rows = json.loads(baseline[0])
                    for row in rows:
                        if row['id'] == fid:
                            if identity_confirmed:
                                row.update(uid=uid, nickname=nickname, level=level,
                                           signature=signature, nickname_pending=int(pending))
                                if nickname or uid:
                                    row.update(identity_review_key=None, identity_review_reason=None)
                            if signature_is_confirmed:
                                row.update(signature=signature or '', signature_pending=0)
                            if qianxing_level is not _UNSET:
                                row['qianxing_level'] = qianxing_level
                            if tags is not _UNSET:
                                row['tags'] = tags
                    self._conn.execute('UPDATE roster_baseline SET rows_json=? WHERE id=1',
                                       (json.dumps(rows, ensure_ascii=False),))

    def import_friend_profile(self, uid, nickname, level, signature, image_bytes, *,
                              friend_id=None, signature_confirmed=True, save_image=True):
        """Atomically import reviewed offline details, without asserting presence/membership.

        A matching UID updates its owner. An unbound local row is only merged
        when the user explicitly chooses it; a shared nickname is insufficient.
        """
        if not self.account_uid:
            raise ValueError('请先选择资料所属的账号')
        if not isinstance(uid, str) or re.fullmatch(r'[1-9][0-9]{8}', uid) is None:
            raise ValueError('请填写正确的 9 位好友 UID')
        nickname = nickname.strip()
        if not nickname:
            raise ValueError('请核对并填写好友昵称')
        if level is not None and (type(level) is not int or not 1 <= level <= 60):
            raise ValueError('冒险等级应为 1 至 60，无法确认时请留为未知')
        from io import BytesIO
        from PIL import Image
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                image.verify()
        except Exception as error:
            raise ValueError('主页截图无法读取，请重新粘贴') from error
        stamp = now_str()
        with self._lock, self._conn:
            current = self.get_friend(friend_id) if friend_id is not None else self.find_by_uid(uid)
            if friend_id is not None and current is None:
                raise ValueError('所选好友档案已不存在，请重新选择')
            if current is not None and current['uid'] not in (None, '', uid):
                raise ValueError('所选档案已有不同 UID，不能覆盖为另一位好友')
            owner = self.find_by_uid(uid)
            if current is not None and owner is not None and current['id'] != owner['id']:
                listed = self._conn.execute('SELECT COUNT(*) FROM verified_roster WHERE friend_id IN (?,?)',
                                            (current['id'], owner['id'])).fetchone()[0]
                if listed > 1:
                    raise ValueError('当前完整名单中两条记录都已存在，请先核查是否为同一好友，再重新扫描')
                self._merge_friend_locked(owner['id'], current['id'])
                current = self.get_friend(owner['id'])
            if current is None:
                cursor = self._conn.execute('''INSERT INTO friends
                    (uid,nickname,current_status,first_seen_at) VALUES (?,?,'unknown',?)''',
                    (uid, nickname, stamp))
                current = self.get_friend(cursor.lastrowid)
            self.update_friend_manual(
                current['id'], uid, nickname,
                current['level'] if level is None else level,
                (signature or '') if signature_confirmed else current['signature'],
                signature_confirmed=signature_confirmed, commit=False)
            # An explicitly selected unbound row may have joined the UID owner.
            fid = self.find_by_uid(uid)['id']
            if save_image:
                self.save_profile_capture(fid, image_bytes, stamp, commit=False, force=True)
            self._conn.execute('''INSERT INTO status_events(uid,nickname,event_type,event_time)
                VALUES (?,?,'manual_import',?)''', (uid, nickname, stamp))
            return fid

    def delete_friend(self, fid: int) -> None:
        with self._lock:
            self._delete_evidence_locked([fid])
            self._conn.execute('DELETE FROM qianxing_activity_sessions WHERE friend_id=?', (fid,))
            self._conn.execute('DELETE FROM presence_range_breaks WHERE friend_id=?', (fid,))
            self._conn.execute("DELETE FROM friends WHERE id=?", (fid,))
            self._conn.commit()

    def delete_friends(self, ids) -> None:
        """批量删除好友记录。"""
        ids = list(ids)
        if not ids:
            return
        with self._lock:
            placeholders = ",".join("?" for _ in ids)
            self._delete_evidence_locked(ids)
            self._conn.execute(
                f'DELETE FROM qianxing_activity_sessions WHERE friend_id IN ({placeholders})', tuple(ids))
            self._conn.execute(
                f'DELETE FROM presence_range_breaks WHERE friend_id IN ({placeholders})', tuple(ids))
            self._conn.execute(
                f"DELETE FROM friends WHERE id IN ({placeholders})", tuple(ids)
            )
            self._conn.commit()

    def delete_all_friends(self) -> None:
        """清空全部好友记录。"""
        with self._lock:
            self._conn.execute("DELETE FROM friends")
            self._conn.execute('DELETE FROM visual_profiles')
            self._delete_evidence_locked()
            self._conn.execute('DELETE FROM qianxing_activity_sessions')
            self._conn.execute('DELETE FROM presence_range_breaks')
            self._conn.execute('DELETE FROM verified_roster')
            self._conn.execute('DELETE FROM online_scan_roster')
            self._conn.execute('DELETE FROM verified_scan')
            self._conn.commit()

    # ------------------------------------------------------------------
    # 昵称去重（滚动重叠时的相似昵称合并，可撤回）
    # ------------------------------------------------------------------
    def log_dedup(self, matched_nickname: str, friend_id: int,
                  kept_nickname: str, similarity: float, now: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO dedup_log
                   (matched_nickname, friend_id, kept_nickname, similarity, event_time, undone)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (matched_nickname, friend_id, kept_nickname, similarity, now),
            )
            self._conn.commit()

    def get_dedup_logs(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dedup_log ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    def undo_dedup(self, dedup_id: int) -> bool:
        """撤回历史去重，恢复原昵称且不重复创建已经恢复过的档案。"""
        with self._lock, self._conn:
            row = self._conn.execute('SELECT * FROM dedup_log WHERE id=?', (dedup_id,)).fetchone()
            if row is None or row['undone']:
                return False
            keeper = self._conn.execute('SELECT * FROM friends WHERE id=?', (row['friend_id'],)).fetchone()
            if keeper and keeper['nickname'] == row['matched_nickname'] and row['kept_nickname'] != row['matched_nickname']:
                self._conn.execute('UPDATE friends SET nickname=? WHERE id=?',
                                   (row['kept_nickname'], keeper['id']))
            existing = self._conn.execute('SELECT id FROM friends WHERE nickname=? AND id!=?',
                                         (row['matched_nickname'], row['friend_id'])).fetchone()
            if existing is None and row['matched_nickname'] != row['kept_nickname']:
                self._conn.execute('INSERT INTO friends(nickname, first_seen_at) VALUES (?,?)',
                                   (row['matched_nickname'], now_str()))
            self._conn.execute('UPDATE dedup_log SET undone=1 WHERE id=?', (dedup_id,))
            return True

    # ------------------------------------------------------------------
    # 扫描会话
    # ------------------------------------------------------------------
    def add_session(self, scan_time: str, total: int, online: int, offline: int, *, friend_ids=None) -> int:
        with self._lock, self._conn:
            if friend_ids is not None:
                rows = [self.get_friend(fid) for fid in sorted(set(friend_ids))]
                if any(row is None for row in rows):
                    raise ValueError('扫描到的档案已不存在')
                baseline = self._conn.execute(
                    "SELECT value FROM account_metadata WHERE key='legacy_membership_baseline'").fetchone()
                previous = json.loads(baseline[0]) if baseline else None
                comparison = compare_rosters(previous, rows)
                self._record_roster_additions_locked(comparison, rows, scan_time)
                # Legacy disappearance still requires its existing repeated
                # observations; this snapshot is only for addition detection.
                self._set_metadata_locked('legacy_membership_baseline', json.dumps(
                    dict(scan_time=scan_time, total=len(rows), rows=rows), ensure_ascii=False))
            cur = self._conn.execute(
                "INSERT INTO scan_sessions (scan_time, total_friends, online_count, offline_count) VALUES (?, ?, ?, ?)",
                (scan_time, total, online, offline),
            )
            self._record_presence_success_locked(scan_time)
            self._conn.commit()
            return cur.lastrowid

    # ------------------------------------------------------------------
    # 状态变动 / 统计
    # ------------------------------------------------------------------
    def get_recent_events(self, limit: int = 500, *, changes_only=False) -> List[Dict[str, Any]]:
        with self._lock:
            # Filter before LIMIT so diagnostic traffic cannot crowd real
            # changes out of the visible history.
            where = ('WHERE event_type IN (%s)' % ','.join('?' for _ in STATUS_CHANGE_LABELS)
                     if changes_only else '')
            if changes_only:
                where += ' AND has_event_identity(uid,nickname)'
            params = (*STATUS_CHANGE_LABELS, limit) if changes_only else (limit,)
            rows = self._conn.execute(
                f"SELECT * FROM status_events {where} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
            return [dict(r) for r in rows]

    def get_status_summary(self, *, now=None) -> Dict[str, int]:
        rows = self.get_display_friends(now=now)
        total = len(rows)
        online = sum(r['current_status'] == 'online' for r in rows)
        offline = sum(r['current_status'] == 'offline' for r in rows)
        return {"total": total, "online": online, "offline": offline,
                "unknown": total-online-offline}

    def get_display_friends(self, *, now=None):
        """Latest verified list; historical OCR drafts remain separately accessible."""
        with self._lock:
            if self._conn.execute('SELECT 1 FROM verified_scan WHERE id=1').fetchone():
                rows = self._conn.execute('''SELECT f.* FROM friends f JOIN
                    (SELECT friend_id,position FROM verified_roster UNION ALL
                     SELECT friend_id,1000000+friend_id FROM online_scan_roster
                     WHERE friend_id NOT IN (SELECT friend_id FROM verified_roster)) r
                    ON f.id=r.friend_id ORDER BY r.position''').fetchall()
                return [self._presence_row_locked(r, _time_text(now)) for r in rows]
            return self.get_all_friends(now=now)

    def get_visual_profiles(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute('SELECT v.*,f.nickname,f.level,f.qianxing_level,f.tags,f.uid,f.signature,f.signature_pending,f.nickname_pending,f.identity_review_key,f.identity_review_reason FROM visual_profiles v JOIN friends f ON f.id=v.friend_id')]

    def get_identity_candidate_ids(self):
        """Only the latest roster, new online observations and unresolved owners.

        Historical drafts retain their evidence but cannot compete with today's
        cards. A returning archived friend is recovered by an actual UID read.
        Before any complete scan all existing archives remain candidates.
        """
        with self._lock:
            if not self._conn.execute('SELECT 1 FROM verified_scan WHERE id=1').fetchone():
                return {row[0] for row in self._conn.execute('SELECT id FROM friends')}
            ids = {row[0] for row in self._conn.execute('''SELECT friend_id FROM verified_roster
                UNION SELECT friend_id FROM online_scan_roster''')}
            ids.update(row['id'] for row in pending_removals(self._conn))
            return ids

    def get_nickname_image(self, fid):
        with self._lock:
            row = self._conn.execute('SELECT name_image FROM visual_profiles WHERE friend_id=?', (fid,)).fetchone()
            return bytes(row[0]) if row else None

    def get_complete_roster(self):
        """Only a successfully committed full roster is a comparison baseline."""
        with self._lock:
            meta = self._conn.execute('SELECT * FROM verified_scan WHERE id=1').fetchone()
            if meta is None:
                return None
            live = [dict(r) for r in self._conn.execute('SELECT f.* FROM verified_roster r JOIN friends f ON f.id=r.friend_id ORDER BY r.position')]
            if len(live) != meta['total'] or len({r['id'] for r in live}) != len(live):
                return None
            saved = self._conn.execute('SELECT * FROM roster_baseline WHERE id=1').fetchone()
            if saved and saved['scan_time'] == meta['scan_time']:
                rows = json.loads(saved['rows_json'])
            else:
                rows = live
            if len(rows) != meta['total'] or len({r['id'] for r in rows}) != len(rows):
                return None
            return {'scan_time': meta['scan_time'], 'rows': rows}

    def get_roster_comparisons(self):
        with self._lock:
            return [json.loads(r[0]) for r in self._conn.execute(
                'SELECT details_json FROM roster_comparisons ORDER BY id DESC')]

    @staticmethod
    def _validate_scan_identities(records, *, identity_complete):
        uids = [record.get('uid') for record in records]
        for uid in uids:
            if uid in (None, '') and not identity_complete:
                continue
            if not isinstance(uid, str) or re.fullmatch(r'[0-9]{9}', uid) is None:
                raise ValueError('完整身份名单必须包含所有好友的有效 UID' if identity_complete
                                 else '非空 UID 必须是 9 位数字')
        known = [uid for uid in uids if uid]
        if len(set(known)) != len(known):
            raise ValueError('名单包含重复 UID，未提交')
        ids = [record.get('id') for record in records if record.get('id') is not None]
        if len(set(ids)) != len(ids):
            raise ValueError('名单包含重复本地档案，未提交')

    def _apply_scan_records_locked(self, records, scan_time):
        ids = []
        for record in records:
            entry, fid = record['entry'], record.get('id')
            completed_uid = False
            uid = record.get('uid') or None
            signature = record.get('signature', getattr(entry, 'signature', None))
            signature_pending = bool(getattr(entry, 'signature_pending', False))
            interface_mode = getattr(entry, 'interface_mode', 'normal')
            if interface_mode not in ('normal', 'qianxing'):
                raise ValueError('好友界面模式无效')
            level = entry.level if interface_mode == 'normal' else None
            qianxing_level = getattr(entry, 'qianxing_level', None) if interface_mode == 'qianxing' else None
            tags = getattr(entry, 'tags', None) if interface_mode == 'qianxing' else None
            activity = (getattr(entry, 'qianxing_activity', None)
                        if interface_mode == 'qianxing' and entry.status == 'online'
                        and getattr(entry, 'qianxing_activity_verified', False) is True else None)
            if record.get('profile_image') is not None and not uid:
                raise ValueError('主页截图必须由本次确认的好友 UID 归档')
            if uid:
                if re.fullmatch(r'[0-9]{9}', uid) is None:
                    raise ValueError('UID 必须是 9 位数字')
                # UID is the identity. Never merge all rows sharing a name.
                supplied = self.get_friend(fid) if fid is not None else None
                completed_uid = supplied is not None and not supplied['uid']
                if fid is not None and (supplied is None or supplied['uid'] not in (None, '', uid)):
                    raise ValueError('UID 与已有身份冲突，未改绑')
                owners = self._conn.execute('SELECT id FROM friends WHERE uid=? ORDER BY id', (uid,)).fetchall()
                if owners:
                    fid = owners[0]['id']
                    for other in owners[1:]:
                        self._merge_friend_locked(fid, other['id'])
                    # Only this explicitly identified pending row may join its
                    # confirmed UID owner. A shared nickname is not sufficient.
                    if supplied is not None and supplied['id'] != fid and not supplied['uid']:
                        self._merge_friend_locked(fid, supplied['id'])
                if fid is not None:
                    old = self.get_friend(fid)
                    self._conn.execute('UPDATE friends SET uid=? WHERE id=?', (uid, fid))
                    self._conn.execute('UPDATE online_sessions SET uid=? WHERE friend_id=?', (uid, fid))
                    if supplied is not None and not supplied['uid']:
                        # Confirmation resolves a saved pending identity too;
                        # preserve the snapshot's observed fields and timestamp.
                        snapshot = self._conn.execute('SELECT rows_json FROM roster_baseline WHERE id=1').fetchone()
                        if snapshot:
                            rows = json.loads(snapshot['rows_json'])
                            for saved in rows:
                                if saved['id'] == fid:
                                    saved.update(uid=uid, identity_review_key=None, identity_review_reason=None)
                            self._conn.execute('UPDATE roster_baseline SET rows_json=? WHERE id=1',
                                               (json.dumps(rows, ensure_ascii=False),))
                    if (not old['nickname_pending'] and not entry.nickname_pending
                            and old['nickname'] != entry.nickname):
                        self._conn.execute('INSERT OR IGNORE INTO nickname_aliases(friend_id,alias,created_at) VALUES (?,?,?)',
                                           (fid, old['nickname'], scan_time))
                        self._conn.execute("INSERT INTO status_events(uid,nickname,event_type,event_time) VALUES (?,?,'renamed',?)",
                                           (uid, entry.nickname, scan_time))
            if fid is not None:
                row = self.get_friend(fid)
                if row is None:
                    raise ValueError('提交前好友档案已被修改')
                if signature_pending and record.get('signature_image'):
                    corrected = self.get_field_correction(fid, 'signature', record['signature_image'])
                    if corrected is not None:
                        signature, signature_pending = corrected, False
                pending = bool(entry.nickname_pending or row['nickname_pending'])
                nickname = row['nickname'] if pending or not uid else entry.nickname
                self.update_status(fid, entry.status, nickname, level,
                                   scan_time, entry.location, entry.last_seen, commit=False,
                                   qianxing_activity=activity)
                if signature is not None:
                    self._conn.execute('UPDATE friends SET signature=? WHERE id=?', (signature, fid))
            else:
                pending = entry.nickname_pending
                fid = self.add_friend(uid, '' if pending else entry.nickname, level, signature,
                                      entry.status, scan_time, entry.location,
                                      entry.last_seen, commit=False, qianxing_activity=activity)
            if qianxing_level is not None:
                self._conn.execute('UPDATE friends SET qianxing_level=? WHERE id=?', (qianxing_level, fid))
            if tags is not None:
                self._conn.execute('UPDATE friends SET tags=? WHERE id=?', (tags, fid))
            self._conn.execute('UPDATE friends SET nickname_pending=? WHERE id=?', (int(pending), fid))
            self._conn.execute('UPDATE friends SET signature_pending=? WHERE id=?', (int(signature_pending), fid))
            identity = self.get_friend(fid)
            review_key = record.get('identity_review_key', identity['identity_review_key'])
            review_reason = record.get('identity_review_reason', identity['identity_review_reason'])
            if uid:
                review_key, review_reason = None, None
            if review_key and (identity['uid'] or entry.status != 'unknown'):
                raise ValueError('身份待核查占位记录不能绑定 UID 或推断在线状态')
            if any(value is not None and not isinstance(value, str) for value in (review_key, review_reason)):
                raise ValueError('身份核查标识和说明必须是文字或空值')
            self._conn.execute('UPDATE friends SET identity_review_key=?, identity_review_reason=? WHERE id=?',
                               (review_key or None, review_reason or None, fid))
            if completed_uid:
                self._record_friend_event_locked(fid, 'uid_completed', scan_time)
            if fid in ids:
                raise ValueError('同一完整名单不能重复计入相同档案')
            ids.append(fid)
            self._conn.execute('INSERT OR REPLACE INTO visual_profiles VALUES (?,?,?,?,?)',
                               (fid, record['name_image'], record['avatar_image'], scan_time, interface_mode))
            if pending:
                self.save_field_capture(fid, 'nickname', record['name_image'], scan_time,
                                        entry.nickname, commit=False)
            if signature_pending and record.get('signature_image'):
                self.save_field_capture(fid, 'signature', record['signature_image'], scan_time,
                                        getattr(entry, 'signature', None), commit=False)
            if record.get('profile_image') is not None:
                self.save_profile_capture(fid, record['profile_image'], scan_time,
                    entry.nickname, signature=signature, name_image=record['name_image'],
                    signature_image=record.get('signature_image'), nickname_pending=pending,
                    signature_pending=signature_pending, commit=False)
        return ids

    def _record_roster_additions_locked(self, comparison, rows, scan_time, already_seen=()):
        classify_additions(self._conn, comparison, scan_time)
        for kind in ('added', 'reidentified', 'unconfirmed_added'):
            for item in comparison.get(kind, []):
                row = next((row for row in rows if
                            (item.get('id') is not None and row['id'] == item['id']) or
                            (item.get('uid') and row['uid'] == item['uid'])), None)
                if row is not None and row['id'] not in already_seen:
                    self._record_friend_event_locked(row['id'], kind, scan_time)

    def apply_verified_scan(self, records, scan_time, elapsed, evidence_dir,
                            *, comparison=None, identity_complete=False):
        """Commit a fully checked ordered roster and its fingerprints atomically.

        Each record contains an optional existing id, a parsed entry and two PNG
        blobs. Incomplete scans never enter here. Old drafts are retained.
        If provided, comparison is filled with the committed roster difference;
        a failed transaction leaves both it and the previous baseline unchanged.
        """
        records = list(records)
        scan_time = _time_text(scan_time)
        self._validate_scan_identities(records, identity_complete=identity_complete)
        with self._lock, self._conn:
            previous = self.get_complete_roster()
            last_event = self._conn.execute('SELECT COALESCE(MAX(id),0) FROM status_events').fetchone()[0]
            ids = self._apply_scan_records_locked(records, scan_time)
            rows = [self.get_friend(fid) for fid in ids]
            for record, row in zip(records, rows):
                candidates = record.get('identity_candidate_ids')
                if not row['uid'] and candidates:
                    row['identity_candidate_ids'] = sorted(set(candidates))
            if previous is not None:
                # Normalize confirmed merges, but preserve fields observed in
                # the old scan. A newly enrolled UID is not a past observation.
                previous['total'] = len(previous['rows'])
                remapped_ids = [r[0] for r in self._conn.execute(
                    'SELECT friend_id FROM verified_roster ORDER BY position')]
                normalized = {}
                for old, fid in zip(previous['rows'], remapped_ids):
                    old['id'] = fid
                    if fid not in normalized or old.get('uid'):
                        normalized[fid] = old
                previous['rows'] = list(normalized.values())
                # Keep unresolved old identities until a later scan can prove
                # presence or absence; the latest observed total is unchanged.
                pending = pending_removals(self._conn)
                pending_ids = {old['id'] for old in pending}
                for row in rows:
                    inherited = normalized.get(row['id'], {}).get('identity_candidate_ids') or []
                    if not row['uid'] and inherited:
                        row['identity_candidate_ids'] = sorted(set(inherited) | set(row.get('identity_candidate_ids') or []))
                # An unresolved card and its old candidates are alternatives,
                # not additional members. Once its UID is read, compare with
                # the old known identities, not with the anonymous placeholder.
                previous['rows'] = [old for old in previous['rows'] if old.get('uid') or
                    not (set(old.get('identity_candidate_ids') or []) & pending_ids)]
                represented = {old['id'] for old in previous['rows']}
                for old in pending:
                    if old['id'] not in represented:
                        previous['rows'].append(old)
            committed_comparison = compare_rosters(previous, rows)
            deferred = defer_unresolved_removals(previous, rows, committed_comparison)
            save_pending_removals(self._conn, deferred)
            already_seen = {row[0] for row in self._conn.execute('SELECT friend_id FROM online_scan_roster')}
            self._record_roster_additions_locked(committed_comparison, rows, scan_time, already_seen)
            completed = [dict(row) for row in self._conn.execute('''SELECT friend_id AS id,uid,nickname
                FROM status_events WHERE id>? AND event_type='uid_completed' ''', (last_event,))]
            if completed:
                committed_comparison['uid_completed'] = completed
            for row in self._conn.execute("SELECT * FROM friends WHERE current_status='online'").fetchall():
                if row['id'] not in ids:
                    self._settle_unobserved_locked(row, scan_time)
            self._conn.execute('DELETE FROM verified_roster')
            self._conn.execute('DELETE FROM online_scan_roster')
            self._conn.executemany('INSERT INTO verified_roster VALUES (?,?)', enumerate(ids))
            self._conn.execute('INSERT OR REPLACE INTO verified_scan VALUES (1,?,?,?,?)',
                               (scan_time, len(ids), elapsed, evidence_dir))
            self._conn.execute('INSERT OR REPLACE INTO roster_baseline VALUES (1,?,?)',
                               (scan_time, json.dumps(rows, ensure_ascii=False)))
            if comparison is not None:
                self._conn.execute('INSERT INTO roster_comparisons(scan_time,details_json) VALUES (?,?)',
                                   (scan_time, json.dumps(committed_comparison, ensure_ascii=False)))
            if committed_comparison.get('basis') in ('uid', 'local_id'):
                for missing in committed_comparison.get('removed', []):
                    old = (self.get_friend(missing['id']) if 'id' in missing
                           else self.find_by_uid(missing['uid']))
                    if old is not None and old['id'] not in ids:
                        self._settle_unobserved_locked(old, scan_time)
                        self._record_friend_event_locked(old['id'], 'removed', scan_time)
            online = sum(r['entry'].status == 'online' for r in records)
            offline = sum(r['entry'].status == 'offline' for r in records)
            self._conn.execute('INSERT INTO scan_sessions(scan_time,total_friends,online_count,offline_count) VALUES (?,?,?,?)',
                               (scan_time, len(ids), online, offline))
            self._record_presence_success_locked(scan_time)
        if comparison is not None:
            comparison.clear()
            comparison.update(committed_comparison)
        return ids

    def apply_online_scan(self, records, scan_time, elapsed, evidence_dir, *, identity_complete=True,
                          observed_offline=()):
        """Commit an online subset; quick mode may retain unbound/unknown rows."""
        records = list(records)
        observed_offline = list(observed_offline)
        scan_time = _time_text(scan_time)
        self._validate_scan_identities(records+observed_offline, identity_complete=identity_complete)
        allowed_statuses = ('online',) if identity_complete else ('online', 'unknown')
        if any(record['entry'].status not in allowed_statuses for record in records):
            raise ValueError('在线扫描只能提交已确认在线的好友')
        if any(record.get('id') is None or record['entry'].status != 'offline'
               for record in observed_offline):
            raise ValueError('离线观察必须对应已有档案和明确离线的卡片')
        with self._lock, self._conn:
            previous = self.get_complete_roster()
            observed_ids = self._apply_scan_records_locked(records+observed_offline, scan_time)
            ids = observed_ids[:len(records)]
            if previous is not None:
                visible_before = {row[0] for row in self._conn.execute(
                    'SELECT friend_id FROM verified_roster UNION SELECT friend_id FROM online_scan_roster')}
                rows = [self.get_friend(fid) for fid in observed_ids]
                online_comparison = dict(basis='uid' if valid_uid_roster(previous['rows']) else 'local_id',
                    added=[row for row in rows if row['id'] not in visible_before and has_event_identity(row)],
                    unidentified_removed=[row for row in previous['rows'] if not has_event_identity(row)])
                self._record_roster_additions_locked(online_comparison, rows, scan_time, visible_before)
            self._conn.executemany('INSERT OR IGNORE INTO online_scan_roster VALUES (?)',
                                   ((fid,) for fid in observed_ids))
            visible = {row[0] for row in self._conn.execute(
                'SELECT friend_id FROM verified_roster UNION SELECT friend_id FROM online_scan_roster')}
            if not self._conn.execute('SELECT 1 FROM verified_scan WHERE id=1').fetchone():
                visible.update(row[0] for row in self._conn.execute('SELECT id FROM friends'))
            for fid in visible-set(observed_ids):
                row = self.get_friend(fid)
                if row is not None and row['current_status'] != 'unknown':
                    self._settle_unobserved_locked(row, scan_time)
            online = sum(record['entry'].status == 'online' for record in records)
            offline = sum(self.get_friend(fid)['current_status'] == 'offline' for fid in visible)
            self._conn.execute('INSERT INTO scan_sessions(scan_time,total_friends,online_count,offline_count) VALUES (?,?,?,?)',
                               (scan_time, len(visible), online, offline))
            self._set_metadata_locked('online_scan', json.dumps(
                dict(scan_time=scan_time, total=len(ids), elapsed_seconds=elapsed,
                     evidence_dir=evidence_dir), ensure_ascii=False))
            self._record_presence_success_locked(scan_time)
            return ids

    def get_today_online_ranges(self, fid: int, date: Optional[str] = None, *, now=None) -> List[str]:
        """Merge minute-resolution scan fragments without rewriting accounted time."""
        current = datetime.fromisoformat(_time_text(now))
        day = datetime.fromisoformat(date or current.strftime('%Y-%m-%d'))
        end = day + timedelta(days=1)
        with self._lock:
            rows = self._conn.execute(
                'SELECT start_time,end_time FROM online_sessions WHERE friend_id=? AND start_time<? AND end_time>?',
                (fid, end.isoformat(), day.isoformat())).fetchall()
            intervals = [(r[0], r[1]) for r in rows]
            row = self._conn.execute('SELECT * FROM friends WHERE id=?', (fid,)).fetchone()
            breaks = [datetime.fromisoformat(r[0]) for r in self._conn.execute(
                '''SELECT event_time FROM presence_range_breaks
                   WHERE (friend_id=? OR friend_id IS NULL) AND event_time>=? AND event_time<=?''',
                (fid, day.isoformat(), end.isoformat()))]
            # Older versions have no explicit break rows. Preserve their real
            # offline/unknown observations; default-offline events alone are
            # not proof of a game logout. UID-less names are matched conservatively.
            legacy = self._conn.execute(
                "SELECT value FROM account_metadata WHERE key='presence_legacy_event_id'").fetchone()
            legacy_event_id = int(legacy[0]) if legacy else 0
            if row and legacy_event_id:
                breaks.extend(datetime.fromisoformat(r[0]) for r in self._conn.execute(
                    '''SELECT event_time FROM status_events WHERE
                       (uid=? OR (uid IS NULL AND nickname=?))
                       AND event_type IN ('offline','unknown','removed')
                       AND id<=? AND event_time>=? AND event_time<=?''',
                    (row['uid'], row['nickname'], legacy_event_id, day.isoformat(), end.isoformat())))
            active_start = None
            if row and row['current_status'] == 'online' and row['first_online_at']:
                finish = self._presence_end_locked(row, current)
                projected = self._presence_row_locked(row, current.isoformat())
                if projected['presence_monitoring']:
                    active_start = datetime.fromisoformat(row['first_online_at'])
                if datetime.fromisoformat(row['first_online_at']) < end and current > day:
                    intervals.append((row['first_online_at'], finish))
        clipped = []
        for start_text, end_text in intervals:
            if not start_text or not end_text:
                continue
            start, finish = max(day, datetime.fromisoformat(start_text)), min(end, datetime.fromisoformat(end_text))
            if finish > start:
                clipped.append((start, finish))
        merged = _display_online_intervals(clipped, breaks)
        ranges = []
        for start, finish in merged:
            ongoing = active_start is not None and day.date() == current.date() and finish == current and finish >= active_start
            label = '进行中' if ongoing else ('24:00' if finish == end else finish.strftime('%H:%M'))
            ranges.append(start.strftime('%H:%M') + '-' + label)
        return ranges

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export_rows(self) -> List[Dict[str, Any]]:
        return self.get_display_friends()

    def export_events(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM status_events ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    def reset_runtime_state(self) -> None:
        """Clear runtime observations while preserving sessions and event history."""
        with self._lock, self._conn:
            self._recover_monitoring_locked()
            for row in self._conn.execute("SELECT * FROM friends WHERE current_status='online'").fetchall():
                at = row['last_scan_at'] or now_str()
                self._range_break_locked(at, 'reset', row['id'])
                self._finish_presence_locked(row, at)
            self._conn.execute("""UPDATE friends SET current_status=NULL,location=NULL,
                last_scan_at=NULL,first_online_at=NULL,total_online_seconds=0,status_updated_at=NULL,
                last_observed_status=NULL,qianxing_activity=NULL,qianxing_activity_started_at=NULL,
                qianxing_activity_last_at=NULL""")

    def close(self) -> None:
        with self._lock:
            if self._conn:
                if self.monitoring_interval_seconds is not None:
                    self.configure_monitoring(None)
                self._conn.close()
                self._conn = None


def _normalize_nickname(s: str) -> str:
    """归一化昵称用于相似匹配：去空格/标点、全角转半角、统一小写。"""
    if not s:
        return ""
    s = s.strip()
    # 全角转半角
    s = "".join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s
    )
    # 去空格与常见标点
    s = re.sub(r"[\s\u3000·\-_—~,，。.!！?？:：;；'\"‘’“”()（）\[\]【】<>《》/@#￥%…&*+=|\\^]", "", s)
    return s.lower()


def _similarity(a: str, b: str) -> float:
    """两个归一化昵称的相似度（0~1）。"""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _parse_time(s: str) -> float:
    """解析 ISO 时间字符串为秒级时间戳。"""
    import datetime
    return datetime.datetime.fromisoformat(s).timestamp()


def now_str() -> str:
    import datetime
    return datetime.datetime.now().isoformat()


def _elapsed_seconds(start, finish) -> float:
    return max(0., (datetime.fromisoformat(finish)-datetime.fromisoformat(start)).total_seconds())


def _display_online_intervals(intervals, breaks):
    """Coalesce visible adjoining minutes, except across known interruptions.

    Only labels use this union. Duration arithmetic uses exact intervals, so a
    late scan never earns time in the unobserved gap. Actual offline, stop and
    failure boundaries remain separate even when they occur in the same minute.
    """
    # Merged friend identities may carry overlapping histories. Split before
    # unioning them so a second overlapping history cannot erase a known break.
    breaks = sorted(set(breaks))
    pieces = []
    for start, finish in intervals:
        points = [start, *(point for point in breaks if start < point < finish), finish]
        pieces.extend(zip(points, points[1:]))
    result = []
    for start, finish in sorted(pieces):
        if result:
            first, previous = result[-1]
            touching = start.replace(second=0, microsecond=0) <= previous.replace(second=0, microsecond=0)
            interrupted = start >= previous and any(previous <= point <= start for point in breaks)
            if touching and not interrupted:
                result[-1] = (first, max(previous, finish))
                continue
        result.append((start, finish))
    return result


def _union_intervals(intervals):
    valid = []
    for start, finish in intervals:
        if not start or not finish:
            continue
        start = datetime.fromisoformat(start) if isinstance(start, str) else start
        finish = datetime.fromisoformat(finish) if isinstance(finish, str) else finish
        if finish > start:
            valid.append((start, finish))
    result = []
    for start, finish in sorted(valid):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], finish))
        else:
            result.append((start, finish))
    return result


def _duration_text(seconds):
    hours, remainder = divmod(int(seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    return '%02d:%02d:%02d' % (hours, minutes, seconds)


def _overlap_seconds(first, second) -> int:
    def union(intervals):
        result = []
        valid = sorted((_parse_time(a), _parse_time(b)) for a, b in intervals if a and b and b > a)
        for start, end in valid:
            if result and start <= result[-1][1]:
                result[-1] = (result[-1][0], max(result[-1][1], end))
            else:
                result.append((start, end))
        return result
    return round(sum(max(0, min(b, d) - max(a, c))
                     for a, b in union(first) for c, d in union(second)), 6)


def _time_text(value=None) -> str:
    if value is None:
        return datetime.now().isoformat()
    if isinstance(value, str):
        datetime.fromisoformat(value)
        return value
    return value.isoformat()

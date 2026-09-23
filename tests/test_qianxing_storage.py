"""Two game interfaces share identity while retaining separate level fields."""
from tests import test_regressions as _baseline

import atexit
from contextlib import closing
import logging
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from openpyxl import load_workbook

from app.database import Database, _SCHEMA
from app import excel_export, mysql_export

# A standalone run imports the shared isolated logger without running that
# module's tearDownModule. Close its Windows file handles before temp cleanup.
atexit.register(logging.shutdown)


OWNER, FRIEND = '111111111', '333333333'
T0, T1, T2 = '2026-09-20T10:00:00', '2026-09-20T10:01:00', '2026-09-20T10:02:00'


def record(mode='normal', *, level=60, qianxing_level=None, tags=None, status='online', profile_image=None):
    entry = SimpleNamespace(nickname='名字', nickname_pending=False, signature=None,
                            interface_mode=mode, level=level, qianxing_level=qianxing_level,
                            tags=tags, status=status, location=None, last_seen=None)
    row = dict(entry=entry, uid=FRIEND, name_image=b'name-' + mode.encode(), avatar_image=b'avatar')
    if profile_image is not None:
        row['profile_image'] = profile_image
    return row


class QianxingStorageTests(unittest.TestCase):
    def setUp(self):
        self.database = Database(':memory:')
        self.database.set_account_uid(OWNER)
        self.addCleanup(self.database.close)

    def scan(self, item, now=T0):
        return self.database.apply_verified_scan([item], now, 1, '', identity_complete=True)[0]

    def test_interfaces_keep_one_identity_history_and_two_independent_levels(self):
        self.database.configure_monitoring(0)
        fid = self.scan(record(profile_image=b'normal home'))
        self.assertEqual(fid, self.scan(record('qianxing', level=999, qianxing_level=0,
                                              tags='休闲玩家 · 建造爱好者', profile_image=b'qianxing home'), T1))
        self.assertEqual(fid, self.scan(record(level=61, qianxing_level=888, tags='不应覆盖', status='offline'), T2))
        saved = self.database.get_friend(fid)
        self.assertEqual((61, 0, '休闲玩家 · 建造爱好者'),
                         (saved['level'], saved['qianxing_level'], saved['tags']))
        self.assertEqual(1, len(self.database.get_all_friends()))
        self.assertEqual(1, sum(event['event_type'] == 'added' for event in self.database.export_events()))
        self.assertEqual(1, self.database._conn.execute('SELECT COUNT(*) FROM online_sessions').fetchone()[0])
        self.assertEqual([b'qianxing home'],
                         [item['image'] for item in self.database.get_profile_captures(fid)])
        baseline = self.database.get_complete_roster()['rows'][0]
        self.assertEqual((61, 0), (baseline['level'], baseline['qianxing_level']))
        visual = self.database.get_visual_profiles()[0]
        self.assertEqual(('normal', 0, saved['tags']),
                         (visual['interface_mode'], visual['qianxing_level'], visual['tags']))

    def test_unknown_tags_are_retained_and_confirmed_empty_tags_clear(self):
        exact_tags = ' 青色标签  蓝色标签\n白色标签 '
        fid = self.scan(record('qianxing', level=None, qianxing_level=12, tags=exact_tags))
        self.scan(record('qianxing', level=None), T1)
        self.assertEqual((12, exact_tags), (self.database.get_friend(fid)['qianxing_level'],
                                          self.database.get_friend(fid)['tags']))
        self.scan(record(tags=''), T2)
        self.assertEqual(exact_tags, self.database.get_friend(fid)['tags'])
        self.scan(record('qianxing', level=None, tags=''), T2)
        self.assertEqual('', self.database.get_complete_roster()['rows'][0]['tags'])

    def test_manual_edit_can_keep_clear_or_set_new_fields(self):
        fid = self.scan(record('qianxing', level=None, qianxing_level=7, tags='原标签'))
        self.database.update_friend_manual(fid, FRIEND, '名字', 60, '签名')
        self.assertEqual((7, '原标签'), (self.database.get_friend(fid)['qianxing_level'],
                                      self.database.get_friend(fid)['tags']))
        self.database.update_friend_manual(fid, FRIEND, '名字', 60, '签名', qianxing_level=0, tags='')
        baseline = self.database.get_complete_roster()['rows'][0]
        self.assertEqual((0, ''), (baseline['qianxing_level'], baseline['tags']))
        self.database.update_friend_manual(fid, FRIEND, '名字', 60, '签名', qianxing_level=None, tags=None)
        self.assertEqual((None, None), (self.database.get_friend(fid)['qianxing_level'],
                                      self.database.get_friend(fid)['tags']))

    def test_duplicate_uid_rollback_preserves_levels_tags_and_visual_mode(self):
        fid = self.scan(record('qianxing', level=None, qianxing_level=8, tags='旧标签'))
        with self.assertRaises(ValueError):
            self.database.apply_verified_scan([record(level=60), record('qianxing', qianxing_level=9, tags='错误')],
                                               T1, 1, '')
        saved = self.database.get_friend(fid)
        self.assertEqual((None, 8, '旧标签'), (saved['level'], saved['qianxing_level'], saved['tags']))
        self.assertEqual('qianxing', self.database.get_visual_profiles()[0]['interface_mode'])

    def test_merge_retains_both_levels_and_corresponding_visual_mode(self):
        first = self.database.add_friend(FRIEND, '普通', 60, None, 'online', T0)
        other = self.database.add_friend(FRIEND, '千星', None, None, 'online', T1,
                                         qianxing_level=0, tags='标签')
        self.database._conn.execute('INSERT INTO visual_profiles VALUES (?,?,?,?,?)',
                                    (other, b'qname', b'qavatar', T1, 'qianxing'))
        self.database._conn.commit()
        self.assertEqual(first, self.database.resolve_uid(FRIEND, '合并名字', T2))
        saved = self.database.get_friend(first)
        self.assertEqual((60, 0, '标签'), (saved['level'], saved['qianxing_level'], saved['tags']))
        visual = self.database.get_visual_profiles()[0]
        self.assertEqual((b'qname', 'qianxing'), (visual['name_image'], visual['interface_mode']))

    def test_old_database_migration_keeps_backup_and_marks_old_images_normal(self):
        old_schema = (_SCHEMA.replace('    qianxing_level       INTEGER,\n', '')
                      .replace('    tags                TEXT,\n', '')
                      .replace(",\n    interface_mode TEXT NOT NULL DEFAULT 'normal'", ''))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.db'
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(old_schema)
                connection.execute("INSERT INTO friends(uid,nickname,level) VALUES (?,'旧名字',60)", (FRIEND,))
                connection.execute('INSERT INTO visual_profiles VALUES (?,?,?,?)', (1, b'oldname', b'oldavatar', T0))
                connection.commit()
            database = Database(str(path))
            try:
                self.assertEqual(('旧名字', 60, None, None),
                                 tuple(database.get_friend(1)[key] for key in ('nickname', 'level', 'qianxing_level', 'tags')))
                self.assertEqual('normal', database.get_visual_profiles()[0]['interface_mode'])
                database.update_friend_manual(1, FRIEND, '旧名字', 60, None, qianxing_level=6, tags='持久标签')
            finally:
                database.close()
            backups = list(path.parent.glob(path.name + '.backup-*'))
            self.assertEqual(1, len(backups))
            with closing(sqlite3.connect(backups[0])) as backup:
                self.assertNotIn('qianxing_level', {row[1] for row in backup.execute('PRAGMA table_info(friends)')})
                self.assertEqual('旧名字', backup.execute('SELECT nickname FROM friends').fetchone()[0])
            database = Database(str(path))
            try:
                self.assertEqual((6, '持久标签'), (database.get_friend(1)['qianxing_level'], database.get_friend(1)['tags']))
                self.assertEqual(1, len(list(path.parent.glob(path.name + '.backup-*'))))
            finally:
                database.close()


class QianxingExportTests(unittest.TestCase):
    def test_excel_distinguishes_both_levels_and_preserves_zero_and_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            path = excel_export.export_friends_to_excel(
                [dict(uid=FRIEND, level=60, qianxing_level=0, tags='青蓝白标签')], [], directory, account_uid=OWNER)
            workbook = load_workbook(path, read_only=True)
            try:
                headers, values = list(workbook['好友信息'].values)
                row = dict(zip(headers, values))
                self.assertEqual((OWNER, FRIEND, 60, 0, '青蓝白标签'),
                                 tuple(row[key] for key in ('账号UID', 'UID', '冒险等级', '千星等级', '标签')))
            finally:
                workbook.close()

    def test_mysql_upgrades_old_tables_and_exports_fields_with_account_identity(self):
        for owner, table in ((OWNER, 'account_friends'), (None, 'friends')):
            connection = MagicMock()
            cursor = connection.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = [('uid',), ('level',)]
            with patch('pymysql.connect', return_value=connection):
                mysql_export.export_to_mysql([dict(uid=FRIEND, level=60, qianxing_level=0, tags='',
                    qianxing_activity='编辑内容中', qianxing_activity_seconds=60,
                    qianxing_activity_summary='编辑内容中：00:01:00',
                    qianxing_activity_ranges='编辑内容中：10:00-10:01')], account_uid=owner)
            calls = [call.args for call in cursor.execute.call_args_list]
            self.assertIn((f'ALTER TABLE {table} ADD COLUMN qianxing_level INT',), calls)
            self.assertIn((f'ALTER TABLE {table} ADD COLUMN tags TEXT',), calls)
            sql, values = next(args for args in calls if args[0].lstrip().startswith('INSERT INTO'))
            self.assertEqual((0, ''), values[-6:-4])
            self.assertEqual(('编辑内容中', 60, '编辑内容中：00:01:00', '编辑内容中：10:00-10:01'), values[-4:])
            self.assertIn('level=COALESCE(VALUES(level), level)', sql)
            self.assertIn('qianxing_level=COALESCE(VALUES(qianxing_level), qianxing_level)', sql)
            self.assertIn('tags=COALESCE(VALUES(tags), tags)', sql)
            self.assertEqual((OWNER, FRIEND) if owner else (FRIEND,), values[:2] if owner else values[:1])

    def test_mysql_current_schema_needs_no_repeated_alter(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [(name,) for name in ('qianxing_level', 'tags',
            'qianxing_activity', 'qianxing_activity_seconds', 'qianxing_activity_summary', 'qianxing_activity_ranges')]
        with patch('pymysql.connect', return_value=connection):
            mysql_export.export_to_mysql([dict(uid=FRIEND, qianxing_level=9, tags=None)], account_uid=OWNER)
        self.assertFalse(any(call.args[0].startswith('ALTER TABLE') for call in cursor.execute.call_args_list))


if __name__ == '__main__':
    unittest.main()

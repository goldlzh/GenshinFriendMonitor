"""Account exports use isolated storage and mocked MySQL connections."""
from tests import test_regressions as _baseline

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from openpyxl import load_workbook

from app import excel_export, mysql_export
from app.database import Database


OWNER_A, OWNER_B, FRIEND = '111111111', '222222222', '333333333'
NOW = '2026-09-20T10:00:00'


class AccountExportTests(unittest.TestCase):
    def database(self, owner, nickname):
        database = Database(':memory:')
        self.addCleanup(database.close)
        database.set_account_uid(owner)
        database.add_friend(FRIEND, nickname, 60, owner + '的签名', 'offline', NOW)
        return database

    def test_mysql_same_friend_uid_uses_distinct_account_keys(self):
        first, second = self.database(OWNER_A, '账号A好友'), self.database(OWNER_B, '账号B好友')
        connections = [MagicMock(), MagicMock()]
        written = {}
        with patch('pymysql.connect', side_effect=connections):
            for database, connection in zip((first, second), connections):
                result = mysql_export.export_to_mysql(database.export_rows(), account_uid=database.account_uid)
                self.assertEqual((1, 0), (result.written, result.skipped))
                cursor = connection.cursor.return_value.__enter__.return_value
                create = cursor.execute.call_args_list[0].args[0]
                self.assertIn('CREATE TABLE IF NOT EXISTS account_friends', create)
                self.assertIn('PRIMARY KEY (account_uid, uid)', create)
                statement, values = next(call.args for call in cursor.execute.call_args_list
                                         if call.args[0].lstrip().startswith('INSERT INTO'))
                self.assertIn('INSERT INTO account_friends', statement)
                self.assertNotIn(database.account_uid, statement)
                self.assertEqual(15, len(values))
                self.assertEqual((None, None), values[-6:-4])
                self.assertEqual((database.account_uid, FRIEND), values[:2])
                written[values[:2]] = values[2]
                connection.commit.assert_called_once()
                connection.close.assert_called_once()
        self.assertEqual({(OWNER_A, FRIEND): '账号A好友', (OWNER_B, FRIEND): '账号B好友'}, written)

    def test_mysql_account_failure_rolls_back_and_closes(self):
        database = self.database(OWNER_A, '名字')
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        def fail_write(sql, *args):
            if sql.lstrip().startswith('INSERT INTO'):
                raise RuntimeError('write failed')
        cursor.execute.side_effect = fail_write
        with patch('pymysql.connect', return_value=connection), self.assertRaises(RuntimeError):
            mysql_export.export_to_mysql(database.export_rows(), account_uid=database.account_uid)
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_mysql_account_still_skips_unknown_friend_uids(self):
        with patch('pymysql.connect') as connect:
            result = mysql_export.export_to_mysql([{'uid': None}, {'uid': 'wrong'}], account_uid=OWNER_A)
        self.assertEqual((0, 2), (result.written, result.skipped))
        connect.assert_not_called()

    def test_legacy_mysql_keeps_original_table_and_value_order(self):
        connection = MagicMock()
        with patch('pymysql.connect', return_value=connection):
            mysql_export.export_to_mysql([{'uid': FRIEND, 'nickname': 'legacy'}])
        cursor = connection.cursor.return_value.__enter__.return_value
        create = cursor.execute.call_args_list[0].args[0]
        self.assertIn('CREATE TABLE IF NOT EXISTS friends', create)
        self.assertNotIn('account_uid', create)
        statement, values = next(call.args for call in cursor.execute.call_args_list
                                 if call.args[0].lstrip().startswith('INSERT INTO'))
        self.assertIn('INSERT INTO friends', statement)
        self.assertEqual(14, len(values))
        self.assertEqual((None, None), values[-6:-4])
        self.assertEqual((FRIEND, 'legacy'), values[:2])

    def test_excel_filename_and_both_sheets_identify_the_account(self):
        first, second = self.database(OWNER_A, '账号A好友'), self.database(OWNER_B, '账号B好友')
        paths = []
        with tempfile.TemporaryDirectory() as directory, patch.object(excel_export, 'datetime') as clock:
            clock.now.return_value.strftime.return_value = '20260920_100000'
            for database in (first, second):
                friend = database.find_by_uid(FRIEND)
                database.update_status(friend['id'], 'online', None, None, '2026-09-20T10:01:00')
                path = Path(excel_export.export_friends_to_excel(
                    database.export_rows(), database.export_events(), directory, account_uid=database.account_uid))
                paths.append(path)
                self.assertIn(database.account_uid, path.name)
                workbook = load_workbook(path, read_only=True)
                try:
                    for sheet in ('好友信息', '状态变动'):
                        rows = list(workbook[sheet].values)
                        self.assertEqual('账号UID', rows[0][0])
                        self.assertEqual(database.account_uid, rows[1][0])
                        mapped = dict(zip(rows[0], rows[1]))
                        self.assertEqual(FRIEND, mapped['UID'])
                        self.assertEqual(database.export_rows()[0]['nickname'], mapped['昵称'])
                finally:
                    workbook.close()
            self.assertNotEqual(paths[0], paths[1])
            self.assertTrue(all(path.is_file() for path in paths))

    def test_legacy_excel_keeps_original_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = excel_export.export_friends_to_excel([{'uid': FRIEND, 'nickname': 'legacy'}], [], directory)
            workbook = load_workbook(path, read_only=True)
            try:
                self.assertEqual(('UID', '昵称'), tuple(next(workbook['好友信息'].values))[:2])
                self.assertEqual(('时间', 'UID', '昵称', '事件类型'), tuple(next(workbook['状态变动'].values)))
                self.assertEqual(17, workbook['好友信息'].max_column)
                self.assertEqual(('千星等级', '标签'), tuple(next(workbook['好友信息'].values))[-5:-3])
            finally:
                workbook.close()

    def test_invalid_owner_uid_is_rejected_before_export(self):
        for owner in ('', '../111111111', '123', 111111111):
            with patch('pymysql.connect') as connect, self.assertRaises(ValueError):
                mysql_export.export_to_mysql([{'uid': FRIEND}], account_uid=owner)
            connect.assert_not_called()
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'export'
                with self.assertRaises(ValueError):
                    excel_export.export_friends_to_excel([], [], str(target), account_uid=owner)
                self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()

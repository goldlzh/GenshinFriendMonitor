"""Separate friend databases by the UID of the account shown in the game HUD."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from app.config import CONFIG
from app.database import Database


def _account_uid(uid: str) -> str:
    if not isinstance(uid, str) or re.fullmatch(r'[0-9]{9}', uid) is None:
        raise ValueError('账号 UID 必须是 9 位数字')
    return uid


class AccountStore:
    """A legacy database is kept untouched until explicitly copied to an account."""

    def __init__(self, legacy_path=None):
        self.legacy_path = Path(legacy_path or CONFIG.db_path).resolve()
        self.root = self.legacy_path.parent / 'accounts'

    def path_for(self, uid: str) -> Path:
        return self.root / _account_uid(uid) / 'friends.db'

    def list_accounts(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(folder.name for folder in self.root.iterdir()
                      if folder.is_dir() and re.fullmatch(r'[0-9]{9}', folder.name)
                      and (folder / 'friends.db').is_file())

    def open_account(self, uid: str) -> Database:
        path = self.path_for(uid)
        path.parent.mkdir(parents=True, exist_ok=True)
        database = Database(str(path))
        try:
            database.set_account_uid(uid)
        except Exception:
            database.close()
            raise
        return database

    def _check_empty_destination(self, path: Path, uid: str) -> None:
        if not path.exists() or not path.stat().st_size:
            return
        # A writable connection lets SQLite clean up an idle WAL when it closes;
        # read-only WAL inspection can leave sidecars even without live users.
        connection = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)
        try:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            if 'account_metadata' in tables:
                owner = connection.execute(
                    "SELECT value FROM account_metadata WHERE key='account_uid'").fetchone()
                if owner and owner[0] != uid:
                    raise ValueError('目标数据库所属账号 UID 不匹配')
            for table in tables:
                if table in ('account_metadata', 'sqlite_sequence'):
                    continue
                quoted = '"' + table.replace('"', '""') + '"'
                if connection.execute(f'SELECT 1 FROM {quoted} LIMIT 1').fetchone():
                    raise ValueError('该账号数据库已有好友或历史数据，不能覆盖导入')
        finally:
            connection.close()

    def import_legacy(self, uid: str) -> Database:
        """Explicitly copy a legacy snapshot; the target must be closed and empty.

        SQLite backup includes committed WAL data without opening or migrating
        the source through Database. The copy is checked before publication.
        """
        path = self.path_for(uid)
        if not self.legacy_path.is_file():
            raise FileNotFoundError('未找到可导入的旧好友数据库')
        self._check_empty_destination(path, uid)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        copied = None
        try:
            handle, filename = tempfile.mkstemp(prefix='legacy-import-', suffix='.db', dir=path.parent)
            os.close(handle)
            temporary = Path(filename)
            with closing(sqlite3.connect(self.legacy_path.as_uri() + '?mode=ro', uri=True)) as source, \
                    closing(sqlite3.connect(str(temporary))) as destination:
                source.backup(destination)
            copied = Database(str(temporary))
            copied.set_account_uid(uid)
            copied.close()
            copied = None
            # A second check prevents replacing an account created during copy.
            self._check_empty_destination(path, uid)
            # The UI closes its target connection before this call. Refuse a
            # remaining WAL instead of replacing the main file underneath it.
            if Path(str(path) + '-wal').exists():
                raise ValueError('目标数据库仍有活动日志，请关闭该账号数据库后重试')
            temporary.replace(path)
            return self.open_account(uid)
        finally:
            if copied is not None:
                copied.close()
            if temporary is not None:
                # This is only the exact staging file created by mkstemp above.
                temporary.unlink(missing_ok=True)
                for suffix in ('-wal', '-shm'):
                    Path(str(temporary) + suffix).unlink(missing_ok=True)
                # Old-schema migration may create a backup of the staging
                # file. The original legacy database remains the real backup.
                for backup in temporary.parent.glob(temporary.name + '.backup-*'):
                    if backup.is_file():
                        backup.unlink()

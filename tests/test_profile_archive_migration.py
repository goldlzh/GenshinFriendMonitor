"""Offline archive migration never initializes a live application database."""
from tests import test_regressions as _baseline

from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from PIL import Image

from app.database import Database
from scripts.crop_profile_archive import migrate_archive


def png(color, size=(10, 8)):
    buffer = BytesIO()
    Image.new('RGB', size, color).save(buffer, format='PNG')
    return buffer.getvalue()


class ProfileArchiveMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=_baseline.WORK)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root/'friends.db'
        db = Database(str(self.path))
        db.set_account_uid('111111111')
        fid = db.add_friend('123456789', '已确认昵称', 60, '签名', 'offline', '2026-09-21T10:00:00')
        db.save_profile_capture(fid, png('red'), '2026-09-21T10:00:00')
        db.save_profile_capture(fid, png('blue'), '2026-09-21T11:00:00', signature='变更后的签名')
        db.close()
        # A live persisted monitor is deliberately present. The script must
        # not construct Database and reset it as a side effect of migration.
        with closing(sqlite3.connect(str(self.path))) as connection:
            connection.execute("INSERT INTO account_metadata VALUES ('presence_interval_seconds','60.0')")
            connection.commit()

    def image_rows(self, path=None):
        with closing(sqlite3.connect((path or self.path).as_uri()+'?mode=ro', uri=True)) as connection:
            return connection.execute('SELECT id,friend_id,friend_uid,image,observed_at,nickname FROM profile_captures ORDER BY id').fetchall()

    def run_migration(self, **kwargs):
        return migrate_archive(self.path, backup_dir=self.root/'backups', report_path=self.root/'report.json',
                               transform=kwargs.pop('transform', lambda image, uid: png('green', (4, 3))), **kwargs)

    def test_dry_run_default_keeps_original_images_and_creates_no_backup(self):
        original = self.image_rows()
        report = self.run_migration()
        self.assertEqual(report['status'], 'dry_run')
        self.assertEqual(report['converted'], 2)
        self.assertTrue(report['business_unchanged'])
        self.assertEqual(self.image_rows(), original)
        self.assertFalse((self.root/'backups').exists())

    def test_apply_keeps_backup_metadata_monitor_and_count_while_changing_images(self):
        original = self.image_rows()
        calls = []
        def crop(image, uid):
            calls.append(uid)
            return png('green', (4, 3))
        report = self.run_migration(apply=True, transform=crop)
        self.assertEqual(report['status'], 'applied')
        self.assertEqual(calls, ['123456789', '123456789'])
        self.assertEqual(self.image_rows(Path(report['backup'])), original)
        for before, after in zip(original, self.image_rows()):
            self.assertEqual(before[:3]+before[4:], after[:3]+after[4:])
            self.assertEqual(after[3], png('green', (4, 3)))
        self.assertTrue(report['business_unchanged'])
        self.assertEqual(report['quick_check_after'], 'ok')
        self.assertEqual(report['before']['capture_count'], report['after']['capture_count'])
        self.assertEqual(report['after']['dimensions'], {'4x3': 2})
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM account_metadata WHERE key='presence_interval_seconds'").fetchone()[0], '60.0')

    def test_bad_images_are_preserved_and_reported_while_valid_images_are_migrated(self):
        original = self.image_rows()
        def crop(image, uid):
            if image == original[0][3]:
                raise ValueError('UID validation failed')
            return png('green', (4, 3))
        report = self.run_migration(apply=True, transform=crop, vacuum=False)
        self.assertEqual((report['converted'], len(report['skipped'])), (1, 1))
        self.assertEqual(report['skipped'][0]['capture_id'], original[0][0])
        self.assertEqual(self.image_rows()[0], original[0])
        self.assertTrue(report['business_unchanged'])

    def test_concurrent_business_edit_aborts_all_image_replacements(self):
        original = self.image_rows()
        def crop(image, uid):
            with closing(sqlite3.connect(str(self.path))) as concurrent:
                concurrent.execute("UPDATE friends SET nickname='同期人工更正'")
                concurrent.commit()
            return png('green', (4, 3))
        with self.assertRaisesRegex(RuntimeError, '数据库已变化'):
            self.run_migration(apply=True, transform=crop)
        self.assertEqual(self.image_rows(), original)
        report = json.loads((self.root/'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(Path(report['backup']).is_file())

    def test_already_cropped_bytes_are_preserved_without_reencoding(self):
        original = self.image_rows()
        report = self.run_migration(apply=True, transform=lambda image, uid:image)
        self.assertEqual((report['converted'], report['already_cropped']), (0, 2))
        self.assertEqual(self.image_rows(), original)


if __name__ == '__main__':
    unittest.main()

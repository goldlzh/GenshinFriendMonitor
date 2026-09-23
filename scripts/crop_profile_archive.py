"""UID-checked, book-only migration of archived friend screenshots.

Dry-run is the default. --apply creates an online SQLite backup first and
changes only profile_captures.image after checking for concurrent edits.
The application Database class is deliberately never imported or constructed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import sys
import uuid

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _quote(name):
    return '"'+name.replace('"', '""')+'"'


def _business_fingerprint(connection):
    """Hash every stored field except the one image column we may change."""
    digest = hashlib.sha256()
    tables = connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    for table, schema in tables:
        columns = [r[1] for r in connection.execute('PRAGMA table_info('+_quote(table)+')')
                   if not (table == 'profile_captures' and r[1] == 'image')]
        records = []
        for row in connection.execute('SELECT '+','.join(map(_quote, columns))+' FROM '+_quote(table)):
            values = [dict(blob_sha256=_digest(value)) if isinstance(value, bytes) else value for value in row]
            records.append(json.dumps(values, ensure_ascii=True, separators=(',', ':')))
        digest.update(json.dumps([table, schema, columns, sorted(records)], ensure_ascii=True).encode())
    return digest.hexdigest()


def _captures(connection):
    return [dict(r) for r in connection.execute('SELECT * FROM profile_captures ORDER BY id')]


def _image_fingerprint(captures):
    return _digest(json.dumps([(r['id'], _digest(r['image'])) for r in captures]).encode())


def _dimensions(encoded):
    try:
        with Image.open(BytesIO(encoded)) as image:
            return '%dx%d' % image.size
    except Exception:
        return 'undecodable'


def _summary(connection, captures, path):
    tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    counts = {table: connection.execute('SELECT COUNT(*) FROM '+_quote(table)).fetchone()[0] for table in tables}
    per_friend = Counter(r['friend_id'] for r in captures)
    friends = dict(connection.execute('''SELECT COUNT(*) total,COUNT(DISTINCT uid) unique_uids,
        SUM(nickname_pending) nickname_pending,SUM(level IS NOT NULL) adventure_levels,
        SUM(qianxing_level IS NOT NULL) qianxing_levels,SUM(tags IS NOT NULL) tags_known,
        SUM(length(tags)>0) tags_nonempty FROM friends''').fetchone())
    return dict(table_counts=counts, friends=friends, capture_count=len(captures),
                capture_friends=len(per_friend), max_captures_per_friend=max(per_friend.values(), default=0),
                image_bytes=sum(len(r['image']) for r in captures),
                dimensions=dict(sorted(Counter(_dimensions(r['image']) for r in captures).items())),
                database_bytes=path.stat().st_size,
                wal_bytes=Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0)


def migrate_archive(database, *, apply=False, backup_dir=None, report_path=None,
                    transform=None, vacuum=True, progress=None):
    database = Path(database).resolve(strict=True)
    backup_dir = Path(backup_dir or ROOT/'data'/'backups').resolve()
    report = dict(database=str(database), apply=bool(apply), started_at=datetime.now().isoformat(),
                  status='preparing', backup=None, converted=0, already_cropped=0, skipped=[])
    try:
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as source:
            source.row_factory = sqlite3.Row
            source.execute('PRAGMA query_only=ON')
            source.execute('BEGIN')
            original_business = _business_fingerprint(source)
            captures = _captures(source)
            owners = {r[0]:r[1] for r in source.execute('SELECT id,uid FROM friends')}
            report['before'] = _summary(source, captures, database)
            report['business_sha256_before'] = original_business
            report['quick_check_before'] = source.execute('PRAGMA quick_check').fetchone()[0]
            if report['quick_check_before'] != 'ok':
                raise ValueError('数据库 quick_check 失败，未开始迁移')
            if apply:
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = backup_dir/(database.parent.name+'-profile-crop-'+
                                     datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.db')
                with closing(sqlite3.connect(str(backup))) as destination:
                    source.backup(destination)
                    if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('在线备份完整性校验失败，未开始迁移')
                report['backup'] = str(backup)
                if progress:
                    progress('backup', dict(path=str(backup), bytes=backup.stat().st_size))
            source.rollback()
        if transform is None:
            # Isolate OCR logs from live scanner diagnostics without saving
            # configuration or importing the storage layer.
            from dataclasses import replace
            from app import config
            isolated_logs = ROOT/'logs'/'profile-crop-ocr'
            isolated_logs.mkdir(parents=True, exist_ok=True)
            config.CONFIG = replace(config.CONFIG, log_dir=str(isolated_logs))
            from app.profile_layout import crop_profile_capture
            transform = crop_profile_capture
        replacements = []
        for index, row in enumerate(captures, 1):
            try:
                if owners.get(row['friend_id']) != row['friend_uid']:
                    raise ValueError('归档 UID 与好友档案 UID 不一致，保留原图')
                result = transform(bytes(row['image']), row['friend_uid'])
                if not isinstance(result, (bytes, bytearray, memoryview)) or not result:
                    raise ValueError('截图转换返回空值或非字节值')
                result = bytes(result)
                if result == row['image']:
                    report['already_cropped'] += 1
                else:
                    replacements.append((row['id'], row['image'], result))
            except Exception as error:
                report['skipped'].append(dict(capture_id=row['id'], friend_uid=row['friend_uid'], reason=str(error)))
            if progress and (index % 25 == 0 or index == len(captures)):
                progress('converted', dict(checked=index, total=len(captures),
                                          ready=len(replacements), skipped=len(report['skipped'])))
        report['converted'] = len(replacements)
        converted = {key:new for key, old, new in replacements}
        proposed = [dict(row, image=converted.get(row['id'], row['image'])) for row in captures]
        report['proposed_image_bytes'] = sum(len(row['image']) for row in proposed)
        report['proposed_dimensions'] = dict(sorted(Counter(_dimensions(row['image']) for row in proposed).items()))
        if apply:
            with closing(sqlite3.connect(database.as_uri()+'?mode=rw', uri=True, timeout=5)) as target:
                target.row_factory = sqlite3.Row
                try:
                    target.execute('BEGIN IMMEDIATE')
                    if (_business_fingerprint(target) != original_business or
                            _image_fingerprint(_captures(target)) != _image_fingerprint(captures)):
                        raise RuntimeError('离线转换期间数据库已变化，未替换任何图片，请重新运行')
                    for capture_id, old, new in replacements:
                        result = target.execute('UPDATE profile_captures SET image=? WHERE id=? AND image=?',
                                                (new, capture_id, old))
                        if result.rowcount != 1:
                            raise RuntimeError('原截图已变化，整批迁移已回滚')
                    if _business_fingerprint(target) != original_business:
                        raise RuntimeError('图片以外的内容发生变化，整批迁移已回滚')
                    if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise RuntimeError('更新后 quick_check 失败，整批迁移已回滚')
                    target.commit()
                except Exception:
                    target.rollback()
                    raise
                report['status'] = 'applied'
                report['vacuum'] = 'not_requested'
                if vacuum and replacements:
                    target.execute('PRAGMA busy_timeout=1000')
                    try:
                        checkpoint = tuple(target.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
                        if checkpoint[0]:
                            report['vacuum'] = 'skipped_busy_checkpoint'
                        else:
                            target.execute('VACUUM')
                            checkpoint = tuple(target.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
                            report['vacuum'] = 'complete' if not checkpoint[0] else 'complete_checkpoint_busy'
                    except sqlite3.OperationalError as error:
                        report['vacuum'] = 'skipped: '+str(error)
        else:
            report['status'] = 'dry_run'
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as final:
            final.row_factory = sqlite3.Row
            final.execute('PRAGMA query_only=ON')
            report['after'] = _summary(final, _captures(final), database)
            report['business_sha256_after'] = _business_fingerprint(final)
            report['business_unchanged'] = report['business_sha256_after'] == original_business
            report['quick_check_after'] = final.execute('PRAGMA quick_check').fetchone()[0]
        if not report['business_unchanged'] or report['quick_check_after'] != 'ok':
            raise RuntimeError('迁移后验证不一致，请查看保留的完整备份')
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = str(error)
        raise
    finally:
        report['finished_at'] = datetime.now().isoformat()
        if report_path is not None:
            path = Path(report_path).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--apply', action='store_true', help='write after a verified online backup')
    parser.add_argument('--backup-dir', type=Path, default=ROOT/'data'/'backups')
    parser.add_argument('--report', type=Path, default=ROOT/'logs'/'profile-crop-migration.json')
    parser.add_argument('--no-vacuum', action='store_true')
    args = parser.parse_args()
    report = migrate_archive(args.database, apply=args.apply, backup_dir=args.backup_dir,
                             report_path=args.report, vacuum=not args.no_vacuum,
                             progress=lambda stage, info: print(json.dumps(dict(stage=stage, **info)), flush=True))
    print(json.dumps({key:report[key] for key in ('status', 'converted', 'already_cropped',
                                                'business_unchanged', 'quick_check_after')}, ensure_ascii=True))


if __name__ == '__main__':
    main()

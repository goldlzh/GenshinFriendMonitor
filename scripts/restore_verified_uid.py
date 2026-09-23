"""Restore an audited, complete UID traversal into the scanner's normal database.

Dry-run by default: writes a separate preview database for UI verification.
--apply makes a SQLite backup first; existing complete rosters are never replaced.
No game input or new OCR guesses are used by this recovery tool.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import CONFIG
from app.database import Database
from app import fast_layout as layout, parser, profile_layout
from app.fast_scan import _decode, _png
from app.friend_screen import CardBand


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def check(condition, message):
    if not condition:
        raise ValueError(message)


def source_directory(value):
    path = (ROOT/value).resolve()
    check(path.is_relative_to((ROOT/'logs/scans').resolve()), 'Source must be a saved scan in this workspace')
    return path


def load_observations(directory, expected):
    result = read_json(directory/'result.json')
    check(result['complete'] and result['expected_total'] == expected
          and result['covered_rows'] == expected and result['recognized_rows'] == expected,
          'The source traversal is incomplete')
    entries = {row['position']-1: row['entry'] for row in result['rows']}
    check(set(entries) == set(range(expected)) and all(entries.values()), 'Invalid source row coverage')
    observations = {}
    for page in read_lines(directory/'pages.jsonl'):
        frame = _decode((directory/('page-%03d.png' % page['page'])).read_bytes())
        check(frame is not None and frame.shape == (1080, 1920, 3), 'Missing normalized page image')
        for row in page['rows']:
            index = row['index']
            check(index in entries, 'Page references an out-of-range row')
            # The final result selected this exact observation, including the
            # captured coordinates and fields, when resolving page overlaps.
            if row['entry'] != entries[index]:
                continue
            band = CardBand(row['top'], row['bottom'])
            observations[index] = layout.CardObservation(band, parser.FriendEntry(**row['entry']),
                layout.crop(frame, layout.field_rect(band, layout.NAME)),
                layout.crop(frame, (158, band.top+10, 232, band.top+105)))
    check(set(observations) == set(entries), 'A final row has no matching original image')
    return result, [observations[i] for i in range(expected)]


def prepare(audit_path):
    audit = read_json(audit_path)
    association = read_json(audit_path.parent/'roster-visual-correspondence.json')
    expected = audit['expected_friends']
    check(expected > 0 and audit['captured_unique_uids'] == expected
          and audit['association_records_checked'] == expected
          and audit['independent_uid_crop_rereads_matched'] == 2*expected,
          'Independent audit is incomplete')
    directory = source_directory(audit['source_uid_scan'])
    check(directory.name == association['uid_scan'], 'Audit source mismatch')
    quick_directory = source_directory(Path('logs/scans')/association['quick_scan'])
    result, current = load_observations(directory, expected)
    _, quick = load_observations(quick_directory, expected)
    check(result['phase'] == 'UID 采集', 'Source is not a UID traversal')
    mappings = association['rows']
    check(association['matched_one_to_one'] and len(mappings) == expected
          and {r['uid_position'] for r in mappings} == set(range(1, expected+1))
          and all(len(r['candidates']) == 1 for r in mappings)
          and {r['candidates'][0]['quick_position'] for r in mappings} == set(range(1, expected+1)),
          'The two captured rosters do not have a unique, complete image mapping')
    uids = read_lines(directory/'uids.jsonl')
    check(len(uids) == expected and {r['traversal_position'] for r in uids} == set(range(1, expected+1))
          and len({r['uid'] for r in uids}) == expected
          and all(re.fullmatch(r'[1-9][0-9]{8}', r['uid']) for r in uids),
          'Missing or duplicate UID')
    by_position = {row['traversal_position']: row for row in uids}
    checks = read_lines(directory/'uid-checks.jsonl')
    check(checks[-1]['ready'] and checks[-1]['title_verified'] and checks[-1]['total'] == expected,
          'The final list was not verified')
    records, conflicts = [], []
    for mapping in sorted(mappings, key=lambda r: r['uid_position']):
        position = mapping['uid_position']
        obs, reference = current[position-1], quick[mapping['candidates'][0]['quick_position']-1]
        uid_record = by_position[position]
        check(uid_record['nickname'] == obs.entry.nickname, 'UID record and clicked row disagree')
        read = read_json(directory/('uid-%03d-read.json' % position))
        check(read['matched'] and len(read['samples']) == 2
              and [s['sample'] for s in read['samples']] == [0, 1], 'UID was not read twice')
        for sample in read['samples']:
            check(sample['visible'] and sample['uid'] == uid_record['uid']
                  and profile_layout.uid_from_text(*sample['fields'][0]) == uid_record['uid'],
                  'UID sample disagrees with the captured identity')
            crop = _decode((directory/('uid-%03d-crop-%d.png' % (position, sample['sample']))).read_bytes())
            check(crop is not None and crop.shape[:2] == (40, 180), 'Missing original UID crop')
        name = layout.visual_similarity(obs.name_image, reference.name_image)
        avatar = layout.visual_similarity(obs.avatar_image, reference.avatar_image)
        ink = None
        check(obs.entry.level == reference.entry.level, 'Cross-pass level mismatch')
        if obs.entry.nickname != reference.entry.nickname:
            ink = layout.nickname_ink_similarity(obs.name_image, reference.name_image)
            check(name >= .94 and avatar >= .94 and ink >= .97,
                  'Conflicting nickname images are not sufficiently corroborated')
            conflicts.append({'uid': uid_record['uid'], 'position_in_source': position,
                              'uid_pass_text': obs.entry.nickname, 'quick_text': reference.entry.nickname,
                              'name_similarity': name, 'avatar_similarity': avatar, 'ink_similarity': ink,
                              'status': 'pending_review'})
            recovered_entry = replace(obs.entry, nickname='', nickname_pending=True)
        else:
            if not (name >= .86 and avatar >= .82):
                ink = layout.nickname_ink_similarity(obs.name_image, reference.name_image)
                check(avatar >= .90 and ink >= .95, 'Cross-pass image correspondence failed')
            recovered_entry = obs.entry
        records.append({'id': None, 'uid': uid_record['uid'], 'entry': recovered_entry,
                        'name_image': _png(obs.name_image), 'avatar_image': _png(obs.avatar_image)})
    completed = (datetime.fromisoformat(result['scan_time']) + timedelta(seconds=result['elapsed_seconds'])).isoformat(timespec='seconds')
    manifest = {'source_uid_scan': str(directory), 'source_quick_scan': str(quick_directory),
                'observed_at': completed, 'uid_seconds': result['elapsed_seconds'],
                'total': expected, 'pending_nicknames': sum(r['entry'].nickname_pending for r in records), 'conflicts': conflicts,
                'audit_sha256': hashlib.sha256(audit_path.read_bytes()).hexdigest(),
                'uids_sha256': hashlib.sha256((directory/'uids.jsonl').read_bytes()).hexdigest(),
                'source_result_sha256': hashlib.sha256((directory/'result.json').read_bytes()).hexdigest()}
    return records, manifest


def backup_database(source, destination):
    check(source.is_file() and not destination.exists(), 'Invalid backup paths')
    reader = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)
    writer = sqlite3.connect(str(destination))
    try:
        reader.backup(writer)
        check(writer.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Backup integrity check failed')
    finally:
        writer.close()
        reader.close()


def verify_database(db, records):
    rows = db.get_display_friends()
    baseline = db.get_complete_roster()
    check(len(rows) == len(records) and len({r['uid'] for r in rows}) == len(records), 'Saved roster count mismatch')
    check(baseline and len(baseline['rows']) == len(records), 'Complete baseline not saved')
    for row, record in zip(rows, records):
        check(row['uid'] == record['uid'] and row['nickname'] == record['entry'].nickname
              and bool(row['nickname_pending']) == record['entry'].nickname_pending,
              'Saved nickname/UID pairing mismatch')
        check(db.get_nickname_image(row['id']) == record['name_image'], 'Original nickname image not saved')
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--audit', type=Path, required=True)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    audit_path = args.audit.resolve()
    records, manifest = prepare(audit_path)
    destination = Path(CONFIG.db_path).resolve()
    output = ROOT/'logs/uid-recovery'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    output.mkdir(parents=True)
    copy = (destination.with_name(destination.name+'.before-uid-recovery-'+output.name)
            if args.apply else output/'preview.db')
    backup_database(destination, copy)
    target = destination if args.apply else copy
    db = Database(str(target))
    try:
        check(db.get_complete_roster() is None, 'A complete roster already exists; refusing to replace it')
        original_ids = {r['id'] for r in db.get_all_friends()}
        comparison = {'basis': 'skipped', 'reason': '此前无完整名单；从已核验的实机 UID 采集记录恢复',
                      'added': [], 'removed': [], 'renamed': []}
        db.apply_verified_scan(records, manifest['observed_at'], manifest['uid_seconds'], str(output),
                               identity_complete=True, comparison=comparison)
        rows = verify_database(db, records)
        check(original_ids <= {r['id'] for r in db.get_all_friends()}, 'Historical records were removed')
        manifest.update(applied=args.apply, database=str(target), backup=str(copy) if args.apply else None,
                        preserved_history_rows=len(original_ids), stored_ids=[r['id'] for r in rows],
                        restored_at=datetime.now().isoformat(timespec='seconds'))
    finally:
        db.close()
    for conflict in manifest['conflicts']:
        record = records[conflict['position_in_source']-1]
        (output/('nickname-'+conflict['uid']+'.png')).write_bytes(record['name_image'])
    (output/'recovery.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print('Recovery report:', output/'recovery.json')


if __name__ == '__main__':
    main()

"""Replay calibrated UID crops only; never capture/input or write friend data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import re
import sys
import time

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import friend_screen, profile_layout


def image(path):
    result = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    if result is None:
        raise ValueError('Cannot load ' + str(path))
    return cv2.cvtColor(result, cv2.COLOR_BGR2RGB)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directories', nargs='+', type=Path)
    args = parser.parse_args()
    cases = []
    skipped = []
    for directory in args.directories:
        bindings = {row['traversal_position']: row['uid'] for row in
                    (json.loads(line) for line in (directory/'uids.jsonl').read_text(encoding='utf-8').splitlines())}
        for path in sorted(directory.glob('uid-*-profile*.png')):
            serial = int(re.search(r'uid-([0-9]+)-', path.name)[1])
            if serial not in bindings:
                skipped.append(str(path))
                continue
            cases.append((str(path), image(path), bindings[serial]))
        for path in sorted(directory.glob('page-000.png')):
            cases.append((str(path), image(path), None))
    results = []
    for source, frame, expected in cases:
        started = time.perf_counter()
        value = profile_layout.read(frame)
        results.append({'source': source, 'expected': expected, 'uid': value.uid,
                        'visible': value.visible, 'fields': value.fields,
                        'seconds': round(time.perf_counter()-started, 5),
                        'passed': value.uid == expected})
    report = {'scope': 'Offline screenshot replay only; no game input or database writes',
              'uid_rect': profile_layout.UID, 'cases': results, 'skipped_without_baseline': skipped,
              'all_passed': all(row['passed'] for row in results),
              'warm_median_seconds': statistics.median(row['seconds'] for row in results[1:]) if len(results)>1 else None}
    target = ROOT/'logs/uid-layout/verification.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['all_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

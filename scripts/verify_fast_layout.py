"""Offline layout/throughput report for explicitly supplied screenshots.

Inputs must contain a complete 16:9 game client area. This never controls the
game or updates the friend database. Repeated images are an OCR benchmark,
not a live scan or a count of distinct friends.
"""
from pathlib import Path
import argparse
import json
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PIL import Image
import numpy as np
from app import fast_layout as layout, friend_screen
from app.config import CONFIG


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('images', nargs='+', type=Path)
    cli.add_argument('--pages', type=int, default=1)
    cli.add_argument('--mode', choices=('normal', 'qianxing'), default='normal')
    cli.add_argument('--output', type=Path, default=ROOT/'logs/layout-verification.json')
    args = cli.parse_args()
    CONFIG.fast_ocr = True
    frames = []
    for path in args.images:
        frame = friend_screen.normalize(np.asarray(Image.open(path).convert('RGB')))
        if frame is None:
            cli.error('Input must be a complete 16:9 client screenshot: '+str(path))
        frames.append(frame)
    results = []
    for index in range(max(1, args.pages)):
        started = time.perf_counter()
        page = layout.read_page(frames[index % len(frames)], interface_mode=args.mode)
        results.append({'confirmed': page.confirmed, 'expected_total': page.expected_total,
                        'issues': page.issues, 'seconds': time.perf_counter()-started,
                        'entries': [entry.to_dict() for entry in page.entries]})
    report = {'kind': 'offline_supplied_images', 'live_game_verified': False,
              'median_seconds': statistics.median(row['seconds'] for row in results),
              'pages': results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Local report:', args.output)
    return 0 if all(row['confirmed'] for row in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())

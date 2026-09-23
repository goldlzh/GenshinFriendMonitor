"""Re-read saved real screens into an isolated DB; never controls the game.

Usage: python scripts/replay_scan.py SCAN_DIRECTORY --font LOCAL_GAME_FONT
Replay timings exclude game input/capture and are not a live acceptance result.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from PIL import Image
from app import fast_layout as layout
from app.config import CONFIG
from app.database import Database
from app.fast_scan import FastScan, ScanIncomplete
from app.scanner import Scanner


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('scan_directory', type=Path)
    args.add_argument('--font', type=Path, required=True)
    args.add_argument('--tail-frame', type=Path, help='同一证据目录中尚未录入 pages.jsonl 的末页截图')
    args = args.parse_args()
    source = args.scan_directory.resolve()
    records = [json.loads(line) for line in (source/'pages.jsonl').read_text(encoding='utf-8').splitlines()]
    original = json.loads((source/'result.json').read_text(encoding='utf-8'))
    if args.tail_frame is not None:
        tail = args.tail_frame.resolve()
        if not tail.is_file() or not tail.is_relative_to(source):
            raise ValueError('补充末页必须是同一证据目录内的真实截图')
        records.append({'page': max(r['page'] for r in records)+1, 'file': str(tail)})
    out = ROOT/'logs'/'replay'/datetime.now().strftime('%Y%m%d-%H%M%S')
    out.mkdir(parents=True, exist_ok=True)
    corrections = Path(CONFIG.db_path).parent/'nickname-corrections'
    with tempfile.TemporaryDirectory(prefix='replay-db-', dir=out) as work:
        assert Path(work).resolve().is_relative_to(out.resolve())
        CONFIG.db_path = str(Path(work)/'friends.db')
        CONFIG.log_dir = str(out)
        CONFIG.nickname_font_path = str(args.font.resolve())
        CONFIG.save_scan_evidence = False
        if corrections.is_dir():
            shutil.copytree(corrections, Path(work)/'nickname-corrections')
        db = Database()
        scanner = Scanner(db)
        scanner._say = lambda message: print(message, flush=True)
        scanner._check_pause = lambda: True
        class Replay(FastScan):
            cursor = 0
            def check(self):
                pass
            def shot(self):
                record = records[self.cursor]
                path = Path(record['file']) if 'file' in record else source/('page-%03d.png' % record['page'])
                return (0, 0, 1920, 1080), np.array(Image.open(path).convert('RGB'))
            def drag(self, *a, **kw):
                raise ScanIncomplete('回放不允许游戏输入')
            def advance(self, rect, before):
                self.cursor += 1
                if self.cursor >= len(records):
                    raise ScanIncomplete('回放图像用尽')
                rect, after = self.shot()
                old, new = layout.scrollbar(before), layout.scrollbar(after)
                expected = layout.scrollbar_displacement(old, new, self.expected)
                pixels, score = layout.scroll_displacement(before, after, expected)
                if pixels < 20 or score < .67:
                    raise ScanIncomplete('回放图像衔接无法确认')
                self.offset += pixels
                return rect, after, {'mode': 'offline_replay', 'pixels': pixels, 'correlation': score}
        try:
            started = time.perf_counter()
            run = Replay(scanner)
            run.scan_time = original['scan_time']
            result = run.run()
            summary = {'mode': 'offline_replay', 'source': str(source),
                       'tail_frame': str(args.tail_frame.resolve()) if args.tail_frame else None,
                       'complete': result.complete, 'total': result.total,
                       'seconds': time.perf_counter()-started, 'pages': run.frame_count,
                       'database_unique_ids': len({r['id'] for r in db.get_display_friends()}),
                       'message': result.message, 'result': str(run.evidence/'result.json')}
            (out/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0 if result.complete else 1
        finally:
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())

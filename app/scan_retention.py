"""Keep diagnostic images from the latest two logical scans per account.

Only images below known scan/review directories are removed. JSON/text reports,
databases and the mandatory per-friend image archive are never cleanup targets.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

_NAME = re.compile(r'^\d{8}-\d{6}-[0-9a-f]{6}$')
_IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif', '.tif', '.tiff'}
_CURRENT = ContextVar('diagnostic_scan_round', default=None)
_LOCK = threading.RLock()
_ACTIVE = set()


def _linked(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _root(path):
    path = Path(os.path.abspath(path))
    for parent in (path, *path.parents):
        if parent.exists() and _linked(parent):
            raise ValueError('截图清理拒绝链接或重解析路径：%s' % parent)
    return path


def _safe(path, root):
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(root):
        return False
    try:
        return all(not _linked(p) for p in (path, *path.parents)
                   if p.is_relative_to(root) and p.exists())
    except OSError:
        return False


def _read(path):
    try:
        if path.stat().st_size > 2_000_000:
            return {}
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _pid_running(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # Access denied is not proof that another scanner has stopped.
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _directories(root, child):
    directory = root / child
    if not directory.is_dir() or not _safe(directory, root):
        return []
    return [p for p in directory.iterdir() if _NAME.fullmatch(p.name)
            and p.is_dir() and _safe(p, root)]


def _groups(root):
    directories = {p.name: p for p in _directories(root, 'scans')}
    parents = {name: name for name in directories}

    def find(name):
        while parents[name] != name:
            parents[name] = parents[parents[name]]
            name = parents[name]
        return name

    def merge(names):
        for name in names[1:]:
            parents[find(name)] = find(names[0])

    rounds = {}
    ordering = {}
    for name, directory in directories.items():
        marker = directory / 'scan-round.json'
        meta = _read(marker) if _safe(marker, root) else {}
        created = meta.get('created_ns', 0)
        ordering[name] = (name[:15], created if isinstance(created, int) else 0, name)
        round_id = meta.get('round_id')
        if isinstance(round_id, str) and _NAME.fullmatch(round_id):
            rounds.setdefault(round_id, []).append(name)
        # Older full UID workflows wrote their component passes only to the
        # final pass. Group them even after the project directory was moved.
        report_path = directory / 'workflow.json'
        report = _read(report_path) if _safe(report_path, root) else {}
        names = []
        for value in report.get('passes', []) if isinstance(report.get('passes'), list) else []:
            if not isinstance(value, str) or '..' in value.replace('\\', '/').split('/'):
                continue
            member = value.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
            if member in directories:
                names.append(member)
        if name in names:
            merge(names)
    for names in rounds.values():
        merge(names)
    groups = {}
    for name in directories:
        groups.setdefault(find(name), []).append(name)
    active = set()
    for round_id, names in rounds.items():
        manifest = root / 'scan-rounds' / (round_id + '.json')
        meta = _read(manifest) if _safe(manifest, root) else {}
        if ((str(root), round_id) in _ACTIVE or
                (meta.get('active') and _pid_running(meta.get('pid')))):
            active.update(names)
    return directories, sorted(groups.values(), key=lambda group: max(ordering[n] for n in group), reverse=True), active


def _image_files(directory, root):
    # Explicit traversal never follows a directory junction, even into another
    # allowed scan. Recheck every file immediately before unlinking as well.
    for child in directory.iterdir():
        if not _safe(child, root):
            continue
        if child.is_dir():
            yield from _image_files(child, root)
        elif child.is_file() and child.suffix.lower() in _IMAGES:
            yield child


def _remove_images(directories, root, apply, report):
    for directory in directories:
        for image in _image_files(directory, root):
            try:
                size = image.stat().st_size
                if apply:
                    if not _safe(image, root):
                        continue
                    image.unlink()
                report['image_count'] += 1
                report['bytes'] += size
            except OSError as exc:
                report['errors'].append('%s: %s' % (image, exc))


def prune_scan_images(evidence_root, *, keep=2, apply=False):
    """Prune an account's old scan images; dry run unless explicitly applied."""
    if keep < 1:
        raise ValueError('至少保留一轮扫描')
    root = _root(evidence_root)
    report = dict(root=str(root), apply=apply, image_count=0, bytes=0, errors=[],
                  kept_passes=[], old_passes=[])
    with _LOCK:
        directories, groups, active = _groups(root)
        kept = {name for group in groups[:keep] for name in group} | active
        old = set(directories) - kept
        report.update(kept_passes=sorted(kept), old_passes=sorted(old), rounds=len(groups))
        targets = [directories[name] for name in sorted(old)]
        targets += [p for p in _directories(root, 'nickname-review') if p.name in old]
        _remove_images(targets, root, apply, report)
    return report


def prune_log_images(log_dir, *, keep=2, apply=False):
    """Clean legacy and account roots, including the old shared review folder."""
    root = _root(log_dir)
    roots = [root]
    accounts = root / 'accounts'
    if accounts.is_dir() and _safe(accounts, root):
        roots += [p for p in accounts.iterdir() if re.fullmatch(r'\d{9}', p.name)
                  and p.is_dir() and _safe(p, root)]
    reports = [prune_scan_images(p, keep=keep, apply=apply) for p in roots]
    kept = {name for report in reports for name in report['kept_passes']}
    known = kept | {name for report in reports for name in report['old_passes']}
    # Legacy review directories were shared across accounts. Match by their
    # scan-pass name; orphan reviews form their own historical rounds.
    shared = _directories(root, 'nickname-review')
    orphans = sorted((p for p in shared if p.name not in known), key=lambda p: p.name, reverse=True)
    kept.update(p.name for p in orphans[:keep])
    extra = dict(root=str(root / 'nickname-review'), image_count=0, bytes=0, errors=[])
    # The legacy root report already covers its own associated old reviews.
    already = set(reports[0]['old_passes'])
    _remove_images([p for p in shared if p.name not in kept and p.name not in already], root, apply, extra)
    return dict(apply=apply, image_count=sum(p['image_count'] for p in reports)+extra['image_count'],
                bytes=sum(p['bytes'] for p in reports)+extra['bytes'], roots=reports,
                shared_reviews=extra, errors=[e for p in reports+[extra] for e in p['errors']])


def _write_manifest(root, round_id, active):
    directory = root / 'scan-rounds'
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (round_id + '.json')
    if not _safe(target, root):
        raise ValueError('扫描轮次记录路径不安全')
    target.write_text(json.dumps(dict(schema_version=1, round_id=round_id,
                                    active=active, pid=os.getpid()), indent=2), encoding='utf-8')


def new_pass_directory(evidence_root):
    root = _root(evidence_root)
    name = datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
    current = _CURRENT.get()
    round_id = current[1] if current and current[0] == root else name
    directory = root / 'scans' / name
    if not _safe(directory, root):
        raise ValueError('扫描截图路径不安全')
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'scan-round.json').write_text(json.dumps(
        dict(schema_version=1, round_id=round_id, created_ns=time.time_ns()), indent=2), encoding='utf-8')
    _cleanup(root)
    return directory


def _cleanup(root):
    try:
        return prune_scan_images(root, apply=True)
    except (OSError, ValueError):
        # Permission failures must not crash scanning. Individual failed file
        # deletions are returned in the report and retried on the next round.
        return None


@contextmanager
def scan_round(evidence_root, evidence=None):
    root = _root(evidence_root)
    current = _CURRENT.get()
    if current and current[0] == root:
        yield current[1]
        return
    meta = _read(Path(evidence) / 'scan-round.json') if evidence else {}
    round_id = meta.get('round_id')
    if not isinstance(round_id, str) or not _NAME.fullmatch(round_id):
        round_id = datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
    token = _CURRENT.set((root, round_id))
    key = str(root), round_id
    with _LOCK:
        _ACTIVE.add(key)
        _write_manifest(root, round_id, True)
    try:
        _cleanup(root)
        yield round_id
    finally:
        try:
            with _LOCK:
                _write_manifest(root, round_id, False)
                _ACTIVE.discard(key)
            _cleanup(root)
        finally:
            _CURRENT.reset(token)


def retained_scan_round(function):
    """Scanner/workflow entrypoints share one round across nested UID passes."""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        scanner = getattr(self, 'scanner', self)
        with scan_round(scanner.evidence_root, getattr(self, 'evidence', None)):
            return function(self, *args, **kwargs)
    return wrapped


def main():
    parser = argparse.ArgumentParser(description='按账号保留最近两轮扫描截图，默认只预览')
    parser.add_argument('--log-dir', default='logs')
    parser.add_argument('--apply', action='store_true', help='实际清理旧轮图片')
    args = parser.parse_args()
    print(json.dumps(prune_log_images(args.log_dir, apply=args.apply), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

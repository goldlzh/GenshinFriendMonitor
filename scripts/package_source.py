"""Build a public source ZIP using an explicit allowlist, never a workspace ZIP.

Only source, public documentation and synthetic tests are included. Runtime
settings, databases, logs, screenshots, private fixtures and Git history are
never candidates, even if an old commit tracked them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import zipfile

ROOT_FILES = frozenset((
    '.gitignore', '.gitattributes', 'README.md', 'PRIVACY.md', 'CHANGELOG.md',
    'FEATURE_STATUS.md', 'SCAN_WORKFLOW.md', 'SCAN_OPTIMIZATION.md',
    'LOCAL_SETUP.md', 'CODE_REVIEW.md', 'FIXES.md', 'FRIEND_LAYOUT.md',
    'main.py', 'admin_start.py', 'build.spec', 'build.bat', 'start.bat',
    'start-admin.bat', 'start-verify-admin.bat', 'requirements.txt',
    'requirements-local-lock.txt', 'config.example.json',
    'tests/fixtures/README.md',
    'docs/VALIDATION-2026-11-12.md', 'docs/RELEASE_NOTES.md',
))


def public_files(root: Path):
    root = root.resolve()
    paths = [root/name for name in ROOT_FILES if (root/name).is_file()]
    for folder in ('app', 'scripts', 'tests'):
        paths.extend((root/folder).rglob('*.py'))
    paths.extend((root/'prompts').rglob('*.md'))
    paths.extend((root/'prompts/schemas').glob('*.json'))
    paths.append(root/'app/assets/chat_icon.png')
    result = []
    for path in sorted(set(paths)):
        relative = path.relative_to(root)
        if not path.is_file() or '__pycache__' in relative.parts:
            continue
        # Refuse links/junctions, including a linked ancestor, before reading.
        if any(parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction())
               for parent in (path, *path.parents) if parent != root and root in parent.parents):
            raise ValueError('Public source contains a linked path: '+str(relative))
        if not path.resolve().is_relative_to(root):
            raise ValueError('Public source escapes project root: '+str(relative))
        result.append((relative.as_posix(), path.read_bytes()))
    return result


def package(root: Path, output: Path, *, version='1.0.0', stage: Path | None = None):
    if not version or any(c not in '0123456789.-abcdefghijklmnopqrstuvwxyz' for c in version):
        raise ValueError('Version must be a simple release identifier')
    entries = public_files(root)
    manifest = {'product': 'GenshinFriendMonitor', 'version': version,
                'files': [{'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                          for name, data in entries]}
    entries.append(('SOURCE_MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')))
    if stage is not None:
        stage = stage.resolve()
        if stage.exists():
            raise FileExistsError('Staging destination must be new: '+str(stage))
        stage.mkdir(parents=True)
        for name, data in entries:
            path = stage/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    output.mkdir(parents=True, exist_ok=True)
    archive = output/('GenshinFriendMonitor-source-v'+version+'.zip')
    prefix = archive.stem
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name, data in entries:
            info = zipfile.ZipInfo(prefix+'/'+name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            bundle.writestr(info, data)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n', encoding='ascii')
    return archive, len(entries), digest


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    cli.add_argument('--output', type=Path)
    cli.add_argument('--stage', type=Path)
    cli.add_argument('--version', default='1.0.0')
    args = cli.parse_args()
    archive, count, digest = package(args.root, args.output or args.root/'dist',
                                     version=args.version, stage=args.stage)
    print(json.dumps({'archive': str(archive), 'files': count, 'sha256': digest}, ensure_ascii=False))


if __name__ == '__main__':
    main()

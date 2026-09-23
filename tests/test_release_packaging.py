"""Public packages must not collect personal runtime data or private fixtures."""
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from scripts.package_source import package
from scripts.package_windows import package as package_windows


class SourcePackageTests(unittest.TestCase):
    def test_allowlist_excludes_runtime_and_git_even_when_present(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public = ('main.py', 'README.md', 'app/config.py', 'tests/test_example.py',
                      'config.example.json', 'app/assets/chat_icon.png')
            private = ('config.json', '.git/config', 'logs/scan.log', 'data/friends.db',
                       'tests/fixtures/real-screen.npz', '.env', 'screenshots/friend.png',
                       '.venv/settings.py', 'release-work/credential.py')
            for name in (*public, *private):
                path = root/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'public placeholder' if name in public else b'PRIVATE_SENTINEL')
            archive, count, digest = package(root, root/'dist', stage=root/'stage')
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(set(public) | {'SOURCE_MANIFEST.json'},
                                 {name.split('/', 1)[1] for name in bundle.namelist()})
                self.assertFalse(any(b'PRIVATE_SENTINEL' in bundle.read(name) for name in bundle.namelist()))
            self.assertEqual(len(public)+1, count)
            self.assertEqual(64, len(digest))
            self.assertFalse((root/'stage/config.json').exists())

    def test_existing_stage_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stage = root/'stage'
            stage.mkdir()
            (stage/'important.txt').write_text('keep')
            with self.assertRaises(FileExistsError):
                package(root, root/'dist', stage=stage)
            self.assertEqual('keep', (stage/'important.txt').read_text())

    def test_windows_bundle_has_only_executable_public_docs_and_licenses(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root/'GenshinFriendMonitor.exe'
            binary.write_bytes(b'MZ_SYNTHETIC_TEST_EXECUTABLE')
            for name in ('README.md', 'PRIVACY.md', 'CHANGELOG.md', 'FEATURE_STATUS.md'):
                (root/name).write_text('public instructions', encoding='utf-8')
            for name in ('config.json', 'friends.db', 'account.log', 'private.png'):
                (root/name).write_bytes(b'PRIVATE_SENTINEL')
            with patch('scripts.package_windows.dependency_licenses', return_value={'package/LICENSE': b'license'}):
                result = package_windows(root, binary, root/'dist', version='1.1.0')
            with zipfile.ZipFile(root/'dist'/result['archive']) as bundle:
                names = {name.split('/', 1)[1] for name in bundle.namelist()}
                self.assertEqual({'GenshinFriendMonitor.exe', '使用说明.txt', 'README.md', 'PRIVACY.md',
                                  'CHANGELOG.md', 'FEATURE_STATUS.md', 'THIRD_PARTY_LICENSES/package/LICENSE'}, names)
                self.assertFalse(any(b'PRIVATE_SENTINEL' in bundle.read(name) for name in bundle.namelist()))
            self.assertEqual(64, len(result['sha256']))


if __name__ == '__main__':
    unittest.main()

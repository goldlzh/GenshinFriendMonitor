"""Diagnostic retention never touches user archives or another account."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import scan_retention as retention


class ScanRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def scan(self, number, root=None, round_id=None):
        directory = (root or self.root) / 'scans' / ('20260101-%06d-%06x' % (number, number))
        directory.mkdir(parents=True)
        (directory / 'page.png').write_bytes(b'screenshot')
        (directory / 'result.json').write_text('{"complete":true}')
        if round_id:
            (directory / 'scan-round.json').write_text(json.dumps({'round_id': round_id}))
        return directory

    def test_dry_run_and_apply_keep_two_and_leave_text_and_database(self):
        old, second, latest = [self.scan(i) for i in range(1, 4)]
        archive = self.root / 'friends.db'
        archive.write_bytes(b'mandatory nickname and profile archives')
        report = retention.prune_scan_images(self.root)
        self.assertEqual((report['image_count'], report['bytes']), (1, 10))
        self.assertTrue((old / 'page.png').exists())
        retention.prune_scan_images(self.root, apply=True)
        self.assertFalse((old / 'page.png').exists())
        self.assertTrue((old / 'result.json').exists())
        self.assertTrue((second / 'page.png').exists())
        self.assertTrue((latest / 'page.png').exists())
        self.assertEqual(archive.read_bytes(), b'mandatory nickname and profile archives')
        self.assertEqual(retention.prune_scan_images(self.root, apply=True)['image_count'], 0)

    def test_legacy_uid_passes_count_as_one_round(self):
        scans = [self.scan(i) for i in range(1, 6)]
        # An old workflow contained three internal passes, with its summary in
        # the final pass; the project has since moved to this temporary folder.
        (scans[3] / 'workflow.json').write_text(json.dumps({
            'passes': ['C:/old/project/logs/scans/' + p.name for p in scans[1:4]]}))
        report = retention.prune_scan_images(self.root, apply=True)
        self.assertEqual(report['rounds'], 3)
        self.assertEqual(report['image_count'], 1)
        for p in scans[1:]:
            self.assertTrue((p / 'page.png').exists())

    def test_nested_stages_share_the_top_level_round(self):
        rounds = []
        for _ in range(3):
            with retention.scan_round(self.root):
                group = []
                for _ in range(3):
                    directory = retention.new_pass_directory(self.root)
                    (directory / 'page.png').write_bytes(b'x')
                    with retention.scan_round(self.root, directory):
                        self.assertTrue((directory / 'page.png').exists())
                    group.append(directory)
                rounds.append(group)
        self.assertTrue(all(not (p / 'page.png').exists() for p in rounds[0]))
        self.assertTrue(all((p / 'page.png').exists() for group in rounds[1:] for p in group))
        self.assertEqual(retention.prune_scan_images(self.root)['rounds'], 3)

    def test_same_second_orders_by_creation_not_random_suffix(self):
        values = ['ffffff', 'eeeeee', '000000']
        directories = []
        for index, suffix in enumerate(values):
            name = '20260101-000001-' + suffix
            p = self.root / 'scans' / name
            p.mkdir(parents=True)
            (p / 'page.png').write_bytes(b'x')
            (p / 'scan-round.json').write_text(json.dumps(
                {'round_id': name, 'created_ns': index+1}))
            directories.append(p)
        retention.prune_scan_images(self.root, apply=True)
        self.assertFalse((directories[0] / 'page.png').exists())
        self.assertTrue((directories[-1] / 'page.png').exists())

    def test_account_roots_and_old_shared_reviews(self):
        roots = [self.root, self.root / 'accounts' / '100000001', self.root / 'accounts' / '100000002']
        old = []
        for index, root in enumerate(roots):
            scans = [self.scan(index*10+i, root) for i in range(1, 4)]
            old.append(scans[0])
            review = self.root / 'nickname-review' / scans[0].name
            review.mkdir(parents=True)
            (review / 'nickname.png').write_bytes(b'x')
            (review / 'review.json').write_text('{}')
        report = retention.prune_log_images(self.root, apply=True)
        self.assertEqual(report['image_count'], 6)
        for p in old:
            self.assertFalse((p / 'page.png').exists())
            self.assertFalse((self.root / 'nickname-review' / p.name / 'nickname.png').exists())
            self.assertTrue((self.root / 'nickname-review' / p.name / 'review.json').exists())

    def test_active_old_round_protected_and_stale_process_pruned(self):
        p = self.scan(1, round_id='20260101-000001-000001')
        self.scan(2)
        self.scan(3)
        directory = self.root / 'scan-rounds'
        directory.mkdir()
        manifest = directory / (p.name + '.json')
        manifest.write_text(json.dumps({'active': True, 'pid': 12345}))
        with patch.object(retention, '_pid_running', return_value=True):
            self.assertEqual(retention.prune_scan_images(self.root, apply=True)['image_count'], 0)
        with patch.object(retention, '_pid_running', return_value=False):
            self.assertEqual(retention.prune_scan_images(self.root, apply=True)['image_count'], 1)

    def test_orphan_reviews_keep_their_latest_two(self):
        for i in range(1, 4):
            directory = self.root / 'nickname-review' / ('20260101-%06d-%06x' % (i, i))
            directory.mkdir(parents=True)
            (directory / 'crop.png').write_bytes(b'x')
        self.assertEqual(retention.prune_log_images(self.root, apply=True)['image_count'], 1)

    def test_non_round_folders_and_external_paths_are_never_targets(self):
        p = self.scan(1)
        self.scan(2)
        self.scan(3)
        outside = self.root / 'personal'
        outside.mkdir()
        (outside / 'private.png').write_bytes(b'x')
        (p / 'workflow.json').write_text(json.dumps({'passes': [str(p), '../personal']}))
        unknown = self.root / 'scans' / 'unrelated-folder'
        unknown.mkdir()
        (unknown / 'private.png').write_bytes(b'x')
        retention.prune_scan_images(self.root, apply=True)
        self.assertTrue((outside / 'private.png').exists())
        self.assertTrue((unknown / 'private.png').exists())

    def test_directory_links_are_not_followed(self):
        old = self.scan(1)
        self.scan(2)
        self.scan(3)
        outside = self.root / 'archive'
        outside.mkdir()
        target = outside / 'must-survive.png'
        target.write_bytes(b'x')
        try:
            (old / 'linked').symlink_to(outside, target_is_directory=True)
        except OSError:
            # Windows junctions require no symbolic-link privilege and are
            # equally important: cleanup must not traverse either link kind.
            try:
                import _winapi
                _winapi.CreateJunction(str(outside), str(old / 'linked'))
            except (ImportError, OSError):
                self.skipTest('This account cannot create directory links')
        retention.prune_scan_images(self.root, apply=True)
        self.assertTrue(target.exists())
        with self.assertRaises(ValueError):
            retention.prune_scan_images(old / 'linked', apply=True)

    def test_failed_round_closes_active_manifest(self):
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            with retention.scan_round(self.root) as round_id:
                retention.new_pass_directory(self.root)
                raise RuntimeError('failed')
        meta = json.loads((self.root / 'scan-rounds' / (round_id + '.json')).read_text())
        self.assertFalse(meta['active'])
        self.assertIsNone(retention._CURRENT.get())


if __name__ == '__main__':
    unittest.main()

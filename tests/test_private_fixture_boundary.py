"""Publishing tests never silently reads local captured personal data."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.private_fixtures import private_fixture


class PrivateFixtureBoundaryTests(unittest.TestCase):
    def test_default_does_not_probe_any_local_directory(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'is_file') as probe:
            with self.assertRaises(unittest.SkipTest):
                private_fixture('example.npz')
        probe.assert_not_called()

    def test_explicit_directory_reads_only_requested_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / 'example.txt'
            sample.write_text('synthetic sample', encoding='utf-8')
            with patch.dict(os.environ, {'GENSHIN_PRIVATE_FIXTURES': directory}):
                self.assertEqual('synthetic sample', private_fixture('example.txt').read_text(encoding='utf-8'))
                with self.assertRaises(unittest.SkipTest):
                    private_fixture('missing.npz')
                with self.assertRaises(ValueError):
                    private_fixture('../outside.npz')

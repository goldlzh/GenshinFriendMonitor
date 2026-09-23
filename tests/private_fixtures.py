"""Explicit opt-in for local image replays that contain personal information.

Public distributions intentionally contain no captured game screenshots. Set
GENSHIN_PRIVATE_FIXTURES to your own sample directory to run those optional
replays. Other tests use generated images and isolated temporary databases.
"""
from pathlib import Path
import os
import unittest


def private_fixture(name):
    directory = os.environ.get('GENSHIN_PRIVATE_FIXTURES', '').strip()
    if not directory:
        raise unittest.SkipTest('Private image replay disabled; set GENSHIN_PRIVATE_FIXTURES explicitly')
    root = Path(directory).expanduser().resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Private fixture must remain within its configured directory')
    if not path.is_file():
        raise unittest.SkipTest('Optional private replay sample is unavailable: ' + str(name))
    return path

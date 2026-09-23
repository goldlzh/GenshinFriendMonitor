"""Bounded content versions and optional profile archives; no live input/DB."""
from tests import test_regressions as baseline
from tests.private_fixtures import private_fixture

import sqlite3
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from app.config import CONFIG
from app.evidence_store import (EvidenceStoreMixin, ensure_evidence_schema,
                                evidence_schema_needs_migration, same_field_image)
from app import friend_screen, scan_workflow
from tests import test_uid_menu_freshness as menu_fixture


def encoded(text='field A', *, offset=(0, 0), brightness=0, light=False, dot=False):
    background, ink = ((30, 225) if light else (230, 35))
    image = np.full((42, 270, 3), background, np.uint8)
    cv2.putText(image, text, (8+offset[0], 29+offset[1]), cv2.FONT_HERSHEY_SIMPLEX,
                .7, (ink, ink, ink), 2, cv2.LINE_AA)
    if dot:
        image[26:29, 252:255] = ink
    image = np.clip(image.astype(np.int16)+brightness, 0, 255).astype(np.uint8)
    return cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))[1].tobytes()


class Store(EvidenceStoreMixin):
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(':memory:')
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('''CREATE TABLE friends(id INTEGER PRIMARY KEY,uid TEXT,
            nickname TEXT,signature TEXT,nickname_pending INTEGER DEFAULT 0,
            signature_pending INTEGER DEFAULT 0)''')
        ensure_evidence_schema(self._conn)
        self._conn.execute("INSERT INTO friends VALUES(1,'123456789','name','signature',0,0)")
        self._conn.commit()

    def get_friend(self, fid):
        row = self._conn.execute('SELECT * FROM friends WHERE id=?', (fid,)).fetchone()
        return dict(row) if row else None


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = Store()
        self.addCleanup(self.db._conn.close)
        self.enabled = patch.object(CONFIG, 'save_profile_screenshots', True, create=True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def test_same_name_signature_ignores_role_book_changes(self):
        first = self.db.save_profile_capture(1, b'characters A', baseline.T0)
        second = self.db.save_profile_capture(1, b'characters B', baseline.T1)
        self.assertEqual(first, second)
        rows = self.db.get_profile_captures(1)
        self.assertEqual(1, len(rows))
        self.assertEqual(b'characters B', rows[0]['image'])
        self.assertEqual(baseline.T1, rows[0]['observed_at'])

    def test_name_or_signature_changes_keep_only_latest_two_versions(self):
        self.db.save_profile_capture(1, b'old', baseline.T0, 'name', signature='one')
        self.db.save_profile_capture(1, b'rename', baseline.T1, 'new name', signature='one')
        self.db.save_profile_capture(1, b'signature', baseline.T2, 'new name', signature='two')
        self.assertEqual([b'signature', b'rename'],
                         [row['image'] for row in self.db.get_profile_captures(1)])
        self.db.save_profile_capture(1, b'stale', baseline.T0, 'older name', signature='three')
        self.assertEqual([b'signature', b'rename'],
                         [row['image'] for row in self.db.get_profile_captures(1)])

    def test_unknown_nickname_versions_compare_images_not_empty_text(self):
        self.db.save_profile_capture(1, b'A', baseline.T0, '', nickname_pending=True,
                                     name_image=encoded('name A'))
        self.db.save_profile_capture(1, b'B', baseline.T1, '', nickname_pending=True,
                                     name_image=encoded('name B'))
        self.db.save_profile_capture(1, b'hover B', baseline.T2, '', nickname_pending=True,
                                     name_image=encoded('name B', offset=(1, 1), brightness=5))
        self.assertEqual([b'hover B', b'A'], [row['image'] for row in self.db.get_profile_captures(1)])

    def test_unknown_signature_versions_compare_images_not_none(self):
        self.db.save_profile_capture(1, b'A', baseline.T0, signature=None,
                                     signature_pending=True, signature_image=encoded('line A'))
        self.db.save_profile_capture(1, b'B', baseline.T1, signature=None,
                                     signature_pending=True, signature_image=encoded('line B'))
        self.assertEqual([b'B', b'A'], [row['image'] for row in self.db.get_profile_captures(1)])

    def test_field_history_mandatory_and_independent_when_profiles_disabled(self):
        with patch.object(CONFIG, 'save_profile_screenshots', False):
            self.assertIsNone(self.db.save_profile_capture(1, b'book', baseline.T0))
            for field in ('nickname', 'signature'):
                for index, stamp in enumerate((baseline.T0, baseline.T1, baseline.T2)):
                    self.db.save_field_capture(1, field, encoded('field '+str(index)), stamp)
                rows = self.db.get_field_captures(1, field)
                self.assertEqual(2, len(rows))
                self.assertEqual(encoded('field 2'), rows[0]['image'])
                self.assertEqual(encoded('field 1'), rows[1]['image'])
        self.assertEqual([], self.db.get_profile_captures(1))

    def test_same_field_jitter_does_not_evict_previous_content(self):
        self.db.save_field_capture(1, 'signature', encoded('before'), baseline.T0)
        current = self.db.save_field_capture(1, 'signature', encoded('after'), baseline.T1)
        repeated = self.db.save_field_capture(1, 'signature',
            encoded('after', offset=(-1, 1), brightness=-5), baseline.T2)
        self.assertEqual(current, repeated)
        self.assertEqual([encoded('after'), encoded('before')],
                         [row['image'] for row in self.db.get_field_captures(1, 'signature')])

    def test_whole_foreground_rejects_extra_dot_and_fragment(self):
        for light in (False, True):
            with self.subTest(light=light):
                original = encoded('same signature', light=light)
                self.assertTrue(same_field_image(original, encoded('same signature', light=light,
                                                                offset=(1, -1), brightness=5)))
                self.assertFalse(same_field_image(original, encoded('same signature', light=light, dot=True)))
                self.assertFalse(same_field_image(original, encoded('signature', light=light)))
        self.assertFalse(same_field_image(encoded('value A'), encoded('value B')))

    def test_real_nickname_and_signature_crops_ignore_bounded_jitter(self):
        # Twelve original field crops, without UID/title/profile regions, from
        # a private historical page, enabled explicitly for local replay.
        fixture = private_fixture('evidence-fields.npz')
        def png(image):
            return cv2.imencode('.png', cv2.cvtColor(image, cv2.COLOR_RGB2BGR))[1].tobytes()
        with np.load(fixture, allow_pickle=False) as fields:
            self.assertEqual(12, len(fields.files))
            for name in fields.files:
                image = fields[name]
                original = png(image)
                for amount in (-5, 5):
                    variant = np.clip(image.astype(np.int16)+amount, 0, 255).astype(np.uint8)
                    with self.subTest(field=name, brightness=amount):
                        self.assertTrue(same_field_image(original, png(variant)))
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (1, 1)):
                    variant = cv2.warpAffine(image, np.float32([[1, 0, dx], [0, 1, dy]]),
                        (image.shape[1], image.shape[0]), borderMode=cv2.BORDER_REPLICATE)
                    with self.subTest(field=name, shift=(dx, dy)):
                        self.assertTrue(same_field_image(original, png(variant)))

    def test_manual_value_survives_jitter_and_can_explicitly_be_empty(self):
        self.db._conn.execute('UPDATE friends SET signature_pending=1 WHERE id=1')
        self.db.save_field_capture(1, 'signature', encoded('unreadable'), baseline.T0)
        self.db.clear_field_pending(1, 'signature', corrected_text='人工签名')
        self.assertEqual(0, self.db.get_friend(1)['signature_pending'])
        self.assertEqual('人工签名', self.db.get_field_correction(
            1, 'signature', encoded('unreadable', offset=(1, 1), brightness=-5)))
        self.assertIsNone(self.db.get_field_correction(1, 'signature', encoded('different')))
        self.db.clear_field_pending(1, 'signature', corrected_text='')
        self.assertEqual('', self.db.get_field_correction(1, 'signature', encoded('unreadable')))
        self.assertEqual(1, len(self.db.get_field_captures(1, 'signature')))

    def test_save_and_clear_do_not_commit_enclosing_transaction(self):
        self.db._conn.execute('BEGIN')
        self.db.save_field_capture(1, 'nickname', encoded(), baseline.T0, commit=False)
        self.db.save_profile_capture(1, b'book', baseline.T0, commit=False)
        self.db.clear_field_pending(1, 'nickname', corrected_text='manual', commit=False)
        self.db._conn.rollback()
        self.assertEqual([], self.db.get_field_captures(1, 'nickname'))
        self.assertEqual([], self.db.get_profile_captures(1))

    def test_uid_merge_and_delete_keep_evidence_bounded_and_isolated(self):
        self.db._conn.execute("INSERT INTO friends VALUES(2,'123456789','other','signature',0,0)")
        self.db._conn.execute("INSERT INTO friends VALUES(3,'987654321','third','signature',0,0)")
        self.db._conn.commit()
        self.db.save_profile_capture(1, b'first', baseline.T0)
        self.db.save_profile_capture(2, b'second', baseline.T1)
        self.db.save_profile_capture(3, b'isolated', baseline.T0)
        for fid, word, stamp in ((1, 'first', baseline.T0), (2, 'second', baseline.T1),
                                 (2, 'third', baseline.T2), (3, 'isolated', baseline.T0)):
            self.db.save_field_capture(fid, 'nickname', encoded(word), stamp)
        with self.db._conn:
            self.db._merge_evidence_locked(1, 2, '123456789')
        self.assertEqual(2, len(self.db.get_profile_captures(1)))
        self.assertEqual(2, len(self.db.get_field_captures(1, 'nickname')))
        self.assertEqual([], self.db.get_field_captures(2, 'nickname'))
        with self.db._conn:
            self.db._delete_evidence_locked([1])
        self.assertEqual([], self.db.get_profile_captures(1))
        self.assertEqual([], self.db.get_field_captures(1, 'nickname'))
        self.assertEqual(1, len(self.db.get_profile_captures(3)))
        self.assertEqual(1, len(self.db.get_field_captures(3, 'nickname')))

    def test_uid_merge_preserves_manual_text_of_an_equivalent_older_crop(self):
        self.db._conn.execute("INSERT INTO friends VALUES(2,'123456789','other','signature',0,0)")
        self.db._conn.commit()
        self.db.save_field_capture(1, 'signature', encoded('same'), baseline.T0)
        self.db.clear_field_pending(1, 'signature', corrected_text='manual')
        self.db.save_field_capture(2, 'signature', encoded('same', brightness=5), baseline.T1)
        with self.db._conn:
            self.db._merge_evidence_locked(1, 2, '123456789')
        self.assertEqual(1, len(self.db.get_field_captures(1, 'signature')))
        self.assertEqual('manual', self.db.get_field_correction(1, 'signature', encoded('same')))

    def test_legacy_unknown_signature_establishes_baseline_without_extra_version(self):
        self.db._conn.execute('''INSERT INTO profile_captures
            (friend_id,friend_uid,image,observed_at,nickname) VALUES (1,'123456789',?,?,?)''',
            (b'legacy', baseline.T0, 'name'))
        self.db._conn.commit()
        self.db.save_profile_capture(1, b'known snapshot', baseline.T1, 'name', signature='one')
        rows = self.db.get_profile_captures(1)
        self.assertEqual(1, len(rows))
        self.assertEqual(b'known snapshot', rows[0]['image'])
        self.db.save_profile_capture(1, b'real change', baseline.T2, 'name', signature='two')
        self.assertEqual(2, len(self.db.get_profile_captures(1)))

    def test_schema_upgrade_is_idempotent_and_rolls_back(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE friends(id INTEGER)')
        conn.execute('''CREATE TABLE profile_captures(id INTEGER PRIMARY KEY,friend_id INTEGER,
            friend_uid TEXT,image BLOB,observed_at TEXT,nickname TEXT)''')
        conn.commit()
        self.assertTrue(evidence_schema_needs_migration(conn))
        conn.execute('BEGIN')
        ensure_evidence_schema(conn)
        self.assertFalse(evidence_schema_needs_migration(conn))
        conn.rollback()
        self.assertTrue(evidence_schema_needs_migration(conn))
        with conn:
            ensure_evidence_schema(conn)
            ensure_evidence_schema(conn)
        self.assertFalse(evidence_schema_needs_migration(conn))

    def test_workflow_transfers_signature_original_even_when_unrecognized(self):
        signature = np.full((35, 776, 3), 90, np.uint8)
        obs = SimpleNamespace(entry=SimpleNamespace(signature_pending=True),
                              name_image=np.full((20, 60, 3), 80, np.uint8),
                              avatar_image=np.full((50, 50, 3), 100, np.uint8),
                              signature_image=signature)
        record = scan_workflow.records_from_observations([obs])[0]
        decoded = cv2.imdecode(np.frombuffer(record['signature_image'], np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(signature, decoded)
        self.assertTrue(record['entry'].signature_pending)


class OptionalProfileWorkflowTests(unittest.TestCase):
    setUp = baseline.UidTraversalRegressions.setUp
    simulation = baseline.FastScanRegressions.simulation
    moving_menu = menu_fixture.UidMenuFreshnessTests.moving_menu

    def test_disabling_archive_keeps_both_uid_reads_and_return_guard(self):
        scan, rect, before, state = self.moving_menu()
        with patch.object(CONFIG, 'save_profile_screenshots', False, create=True), \
                patch.object(scan_workflow.profile_layout, 'crop_profile_book') as crop:
            uid = scan.read_uid(rect, before, friend_screen.CardBand(116, 231), 1)
        self.assertEqual('123456789', uid)
        self.assertEqual(2, state['uid_reads'])
        self.assertEqual(1, state['escapes'])
        self.assertEqual({}, scan.profile_images)
        crop.assert_not_called()


if __name__ == '__main__':
    unittest.main()

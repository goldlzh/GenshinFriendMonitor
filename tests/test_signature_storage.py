"""Field review screenshots and manual signature corrections survive rescanning."""
from tests import test_regressions as baseline
from tests import test_presence_monitoring as presence

import unittest
from unittest.mock import patch

import numpy as np

from app.config import CONFIG
from app.database import Database
from app.fast_scan import _png


class SignatureStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.addCleanup(self.db.close)
        pixels = np.full((32, 140, 3), 215, np.uint8)
        pixels[7:26, 14:19] = 50
        pixels[7:12, 14:50] = 50
        pixels[21:26, 14:50] = 50
        self.image = _png(pixels)
        pixels[7:26, 90:95] = 50
        self.changed = _png(pixels)

    def scan(self, *, image=None, seconds=0, signature=None, pending=True, fid=None,
             uid='123456789', home=None):
        item = presence.record(uid, name='昵称', home=home)
        item['id'] = fid
        item['signature_image'] = self.image if image is None else image
        item['entry'].signature = signature
        item['entry'].signature_pending = pending
        return self.db.apply_verified_scan([item], presence.at(seconds), 0., 'isolated field')[0]

    def test_pending_signature_image_is_saved_even_if_profile_screenshots_disabled(self):
        with patch.object(CONFIG, 'save_profile_screenshots', False):
            fid = self.scan(home=b'profile screenshot')
        row = self.db.get_friend(fid)
        self.assertEqual(1, row['signature_pending'])
        self.assertEqual(self.image, self.db.get_field_captures(fid, 'signature')[0]['image'])
        self.assertEqual([], self.db.get_profile_captures(fid))

    def test_unrelated_manual_edit_keeps_signature_pending(self):
        fid = self.scan()
        self.db.update_friend_manual(fid, '123456789', '准确昵称', 59, None, signature_confirmed=False)
        self.assertTrue(self.db.get_friend(fid)['signature_pending'])
        self.assertTrue(self.db.get_complete_roster()['rows'][0]['signature_pending'])
        self.assertIsNone(self.db.get_field_correction(fid, 'signature', self.image))

    def test_explicit_nonempty_and_empty_manual_signature_remain_corrected_next_scan(self):
        fid = self.scan()
        for signature in ('人工正确签名', ''):
            with self.subTest(signature=signature):
                self.db.update_friend_manual(fid, '123456789', '昵称', 60, signature,
                                             signature_confirmed=True)
                self.assertFalse(self.db.get_friend(fid)['signature_pending'])
                snapshot = self.db.get_complete_roster()['rows'][0]
                self.assertEqual((signature, 0), (snapshot['signature'], snapshot['signature_pending']))
                self.scan(seconds=60)
                row = self.db.get_friend(fid)
                self.assertEqual((signature, 0), (row['signature'], row['signature_pending']))
                self.assertEqual(1, len(self.db.get_field_captures(fid, 'signature')))

    def test_changed_signature_image_requires_new_review_and_keeps_two_versions(self):
        fid = self.scan()
        self.db.update_friend_manual(fid, '123456789', '昵称', 60, '准确签名', signature_confirmed=True)
        self.scan(image=self.changed, seconds=60)
        self.assertTrue(self.db.get_friend(fid)['signature_pending'])
        self.assertIsNone(self.db.get_field_correction(fid, 'signature', self.changed))
        self.assertEqual([self.changed, self.image],
                         [row['image'] for row in self.db.get_field_captures(fid, 'signature')])

    def test_confirmed_uid_merge_remaps_field_images_and_preserves_manual_correction(self):
        owner = self.db.add_friend('123456789', '旧昵称', 60, None, 'offline', presence.T0)
        pending = self.scan(uid=None)
        self.db.update_friend_manual(pending, None, '昵称', 60, '人工签名', signature_confirmed=True)
        fid = self.scan(fid=pending, seconds=60)
        self.assertEqual(owner, fid)
        self.assertIsNone(self.db.get_friend(pending))
        self.assertEqual('人工签名', self.db.get_field_correction(owner, 'signature', self.image))
        self.assertEqual(('人工签名', 0),
                         tuple(self.db.get_friend(owner)[key] for key in ('signature', 'signature_pending')))


if __name__ == '__main__':
    unittest.main()

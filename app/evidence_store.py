"""Bounded screenshot versions, independent of OCR and game input.

The host database supplies ``_conn`` and its re-entrant ``_lock``. Schema
changes and ``*_locked`` helpers deliberately do not commit a host transaction.
"""
from __future__ import annotations

import re

import cv2
import numpy as np

from app.config import CONFIG

_UNSET = object()
_FIELDS = frozenset(('nickname', 'signature'))
_PROFILE_COLUMNS = {
    'signature': 'TEXT',
    'nickname_pending': 'INTEGER NOT NULL DEFAULT 0',
    'signature_pending': 'INTEGER NOT NULL DEFAULT 0',
    'name_image': 'BLOB',
    'signature_image': 'BLOB',
    'content_observed_at': 'TEXT',
}


def evidence_schema_needs_migration(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'friends' not in tables:
        return False
    columns = {r[1] for r in conn.execute('PRAGMA table_info(profile_captures)')}
    review_columns = {r[1] for r in conn.execute('PRAGMA table_info(review_captures)')}
    return not _PROFILE_COLUMNS.keys() <= columns or 'corrected_text' not in review_columns


def ensure_evidence_schema(conn):
    """Create/upgrade evidence tables inside the caller's migration transaction."""
    conn.execute('''CREATE TABLE IF NOT EXISTS profile_captures (
        id INTEGER PRIMARY KEY AUTOINCREMENT, friend_id INTEGER NOT NULL,
        friend_uid TEXT NOT NULL, image BLOB NOT NULL, observed_at TEXT NOT NULL,
        nickname TEXT)''')
    columns = {r[1] for r in conn.execute('PRAGMA table_info(profile_captures)')}
    for name, declaration in _PROFILE_COLUMNS.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE profile_captures ADD COLUMN {name} {declaration}')
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_profile_captures_friend
        ON profile_captures(friend_id, observed_at DESC, id DESC)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS review_captures (
        id INTEGER PRIMARY KEY AUTOINCREMENT, friend_id INTEGER NOT NULL,
        field TEXT NOT NULL CHECK(field IN ('nickname','signature')),
        image BLOB NOT NULL, observed_at TEXT NOT NULL, recognized_text TEXT,
        corrected_text TEXT)''')
    review_columns = {r[1] for r in conn.execute('PRAGMA table_info(review_captures)')}
    if 'corrected_text' not in review_columns:
        conn.execute('ALTER TABLE review_captures ADD COLUMN corrected_text TEXT')
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_review_captures_friend_field
        ON review_captures(friend_id, field, observed_at DESC, id DESC)''')


def _decode(payload):
    if not payload:
        return None
    image = cv2.imdecode(np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR)
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB) if image is not None else None


def _foreground(image):
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    background = cv2.medianBlur(gray, 31)
    # Most list text is dark; user-supplied light glyphs must also remain
    # evidence. Local contrast removes mild uniform card illumination changes.
    contrast = np.abs(background.astype(np.int16)-gray).astype(np.uint8)
    binary = (contrast > 25).astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
    keep = [i for i, stat in enumerate(stats) if i and stat[4] >= 3 and stat[3] >= 2]
    mask = np.isin(labels, keep).astype(np.uint8)*255
    return gray, contrast, mask


def _same_raster(first, second):
    """Very strict pixel fallback for strokes touching a crop's outer edge."""
    if first.shape != second.shape or min(first.shape[:2]) < 6:
        return False
    a, b = (cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) for image in (first, second))
    height, width = a.shape
    dx, dy = min(4, (width-1)//2), min(2, (height-1)//2)
    template = a[dy:height-dy, dx:width-dx]
    if float(template.std()) < 2.5:
        return False
    _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(b, template, cv2.TM_CCOEFF_NORMED))
    if score < .995:
        return False
    shift_x, shift_y = point[0]-dx, point[1]-dy
    x1, x2 = max(0, -shift_x), min(width, width-shift_x)
    y1, y2 = max(0, -shift_y), min(height, height-shift_y)
    diff = (first[y1:y2, x1:x2].astype(np.int16)
            - second[y1+shift_y:y2+shift_y, x1+shift_x:x2+shift_x].astype(np.int16))
    # A uniform brightness/tint change is harmless. A new punctuation mark
    # cannot be hidden by the high correlation of a long unchanged field.
    difference = np.abs(diff-np.median(diff, axis=(0, 1), keepdims=True))
    unexplained = (np.max(difference, axis=2) > 10).astype(np.uint8)
    if np.count_nonzero(unexplained) > max(3, unexplained.size*.0005):
        return False
    _, _, stats, _ = cv2.connectedComponentsWithStats(unexplained)
    return not any(stat[cv2.CC_STAT_AREA] >= 3 for stat in stats[1:])


def same_field_image(first, second):
    """Same complete field at native scale, with bounded rasterization jitter.

    This only deduplicates a known friend's evidence, never identifies a friend.
    All visible strokes must agree in both directions, including punctuation;
    no stretching, OCR equivalence or empty-string equality is used.
    """
    if first is None or second is None:
        return False
    if bytes(first) == bytes(second):
        return True
    images = [_decode(first), _decode(second)]
    if any(image is None for image in images):
        return False
    if _same_raster(*images):
        return True
    values = [_foreground(image) for image in images]
    masks = [value[2] for value in values]
    boxes = []
    for mask in masks:
        ys, xs = np.where(mask != 0)
        boxes.append((int(xs.min()), int(ys.min()), int(xs.max())+1, int(ys.max())+1)
                     if len(xs) else None)
    if any(box is None for box in boxes):
        # Truly flat empty fields can differ slightly in card illumination.
        # Textured/unrecognised images are not declared blank from missing ink.
        return (all(box is None for box in boxes)
                and all(float(value[0].std()) <= 2.5 for value in values))
    (ax, ay, ar, ab), (bx, by, br, bb) = boxes
    if (abs((ar-ax)-(br-bx)) > 2 or abs((ab-ay)-(bb-by)) > 2
            or abs(ax-bx) > 4 or abs(ay-by) > 2):
        return False
    areas = [int(np.count_nonzero(mask)) for mask in masks]
    if min(areas)/max(areas) < .90:
        return False
    height, width = (max(mask.shape[axis] for mask in masks) for axis in (0, 1))
    masks = [cv2.copyMakeBorder(mask, 0, height-mask.shape[0], 0, width-mask.shape[1],
                               cv2.BORDER_CONSTANT, value=0) for mask in masks]
    search = cv2.copyMakeBorder(masks[1], 2, 2, 4, 4, cv2.BORDER_CONSTANT, value=0)
    _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(search, masks[0], cv2.TM_CCORR_NORMED))
    if score < .96:
        return False
    aligned = search[point[1]:point[1]+height, point[0]:point[0]+width]
    for original, other in ((masks[0], aligned), (aligned, masks[0])):
        unexplained = ((original > 0) &
                       (cv2.dilate(other, np.ones((3, 3), np.uint8)) == 0)).astype(np.uint8)
        _, _, stats, _ = cv2.connectedComponentsWithStats(unexplained)
        if any(stat[cv2.CC_STAT_AREA] >= 3 for stat in stats[1:]):
            return False
    return True


def _field_comparison(first, second, field):
    pending = field+'_pending'
    image_key = 'name_image' if field == 'nickname' else 'signature_image'
    if (not first.get(pending) and not second.get(pending)
            and first.get(field) is not None and second.get(field) is not None):
        return first[field] == second[field]
    if first.get(image_key) is not None and second.get(image_key) is not None:
        return same_field_image(first[image_key], second[image_key])
    return None


def _same_profile_content(first, second):
    matches = [_field_comparison(first, second, field) for field in _FIELDS]
    if False in matches:
        return False
    return True if all(match is True for match in matches) else None


def _check_capture(image_bytes, observed_at):
    if not isinstance(image_bytes, (bytes, bytearray, memoryview)) or not image_bytes:
        raise ValueError('截图不能为空')
    if not isinstance(observed_at, str) or not observed_at:
        raise ValueError('截图必须包含采集时间')
    return bytes(image_bytes)


def _check_field(field):
    if field not in _FIELDS:
        raise ValueError('截图字段必须是 nickname 或 signature')


class EvidenceStoreMixin:
    def _prune_profile_captures_locked(self, fid, uid):
        self._conn.execute('DELETE FROM profile_captures WHERE friend_id=? AND friend_uid IS NOT ?',
                           (fid, uid))
        kept = []
        for row in self._conn.execute('''SELECT * FROM profile_captures WHERE friend_id=?
                ORDER BY observed_at DESC,id DESC''', (fid,)).fetchall():
            row = dict(row)
            if len(kept) < 2 and not any(
                    row['image'] == other['image'] or _same_profile_content(row, other) is True
                    for other in kept):
                kept.append(row)
            else:
                self._conn.execute('DELETE FROM profile_captures WHERE id=?', (row['id'],))

    def save_profile_capture(self, fid, image_bytes, observed_at, nickname=None, *,
                             signature=_UNSET, name_image=None, signature_image=None,
                             nickname_pending=None, signature_pending=None, commit=True, force=False):
        """Keep at most two name/signature versions, not character-showcase versions."""
        # A reviewed manual import has its own explicit save-image choice.
        if not force and not getattr(CONFIG, 'save_profile_screenshots', True):
            return None
        payload = _check_capture(image_bytes, observed_at)
        with self._lock:
            friend = self.get_friend(fid)
            if friend is None or re.fullmatch(r'[0-9]{9}', friend['uid'] or '') is None:
                raise ValueError('只有已确认 UID 的好友才能保存主页截图')
            current = dict(nickname=friend['nickname'] if nickname is None else nickname,
                           signature=friend.get('signature') if signature is _UNSET else signature,
                           nickname_pending=bool(friend.get('nickname_pending') if nickname_pending is None
                                                 else nickname_pending),
                           signature_pending=bool(friend.get('signature_pending') if signature_pending is None
                                                  else signature_pending),
                           name_image=name_image, signature_image=signature_image)
            rows = [dict(row) for row in self._conn.execute('''SELECT * FROM profile_captures
                WHERE friend_id=? AND friend_uid=? ORDER BY observed_at DESC,id DESC''',
                (fid, friend['uid']))]
            existing = next((row for row in rows if _same_profile_content(row, current) is True), None)
            # Legacy rows lack signature metadata. Establish an accurate new
            # snapshot in the newest slot rather than inventing a content change.
            baseline = (rows[0] if rows and existing is None
                        and _same_profile_content(rows[0], current) is None else None)
            values = (current['nickname'], current['signature'], int(current['nickname_pending']),
                      int(current['signature_pending']), name_image, signature_image, observed_at)
            if existing is not None or baseline is not None:
                previous = existing or baseline
                if observed_at >= previous['observed_at']:
                    self._conn.execute('''UPDATE profile_captures SET
                        nickname=?,signature=?,nickname_pending=?,signature_pending=?,
                        name_image=?,signature_image=?,content_observed_at=?,observed_at=?,image=?
                        WHERE id=?''', (*values, observed_at, payload, previous['id']))
                capture_id = previous['id']
            else:
                cursor = self._conn.execute('''INSERT INTO profile_captures
                    (friend_id,friend_uid,image,observed_at,nickname,signature,nickname_pending,
                     signature_pending,name_image,signature_image,content_observed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (fid, friend['uid'], payload, observed_at, *values))
                capture_id = cursor.lastrowid
            self._prune_profile_captures_locked(fid, friend['uid'])
            if commit:
                self._conn.commit()
            return capture_id

    def get_profile_captures(self, fid):
        with self._lock:
            return [dict(row) for row in self._conn.execute('''SELECT p.* FROM profile_captures p
                JOIN friends f ON f.id=p.friend_id AND f.uid=p.friend_uid
                WHERE p.friend_id=? ORDER BY p.observed_at DESC,p.id DESC LIMIT 2''', (fid,))]

    def profile_capture_count(self, fid):
        return len(self.get_profile_captures(fid))

    def migrate_profile_captures(self, transform):
        changed = 0
        with self._lock, self._conn:
            for row in self._conn.execute('SELECT id,friend_uid,image FROM profile_captures').fetchall():
                original = bytes(row['image'])
                payload = transform(original, row['friend_uid'])
                if not isinstance(payload, (bytes, bytearray, memoryview)) or not payload:
                    raise ValueError('主页截图转换结果不能为空')
                payload = bytes(payload)
                if payload != original:
                    self._conn.execute('UPDATE profile_captures SET image=? WHERE id=?', (payload, row['id']))
                    changed += 1
        return changed

    def _prune_field_captures_locked(self, fid, field):
        kept = []
        for row in self._conn.execute('''SELECT * FROM review_captures WHERE friend_id=? AND field=?
                ORDER BY observed_at DESC,id DESC''', (fid, field)).fetchall():
            row = dict(row)
            same = next((other for other in kept if same_field_image(row['image'], other['image'])), None)
            if same is not None:
                # A UID merge can bring a newer unconfirmed duplicate beside
                # an older manually calibrated crop. Keep that calibration.
                if same['corrected_text'] is None and row['corrected_text'] is not None:
                    same['corrected_text'] = row['corrected_text']
                    self._conn.execute('UPDATE review_captures SET corrected_text=? WHERE id=?',
                                       (row['corrected_text'], same['id']))
                self._conn.execute('DELETE FROM review_captures WHERE id=?', (row['id'],))
            elif len(kept) < 2:
                kept.append(row)
            else:
                self._conn.execute('DELETE FROM review_captures WHERE id=?', (row['id'],))

    def save_field_capture(self, fid, field, image_bytes, observed_at, recognized_text=None, *, commit=True):
        """Mandatory review evidence; intentionally ignores the homepage switch."""
        _check_field(field)
        payload = _check_capture(image_bytes, observed_at)
        with self._lock:
            if self.get_friend(fid) is None:
                raise ValueError('好友记录不存在')
            rows = self._conn.execute('''SELECT * FROM review_captures WHERE friend_id=? AND field=?
                ORDER BY observed_at DESC,id DESC''', (fid, field)).fetchall()
            previous = next((row for row in rows if same_field_image(row['image'], payload)), None)
            if previous is not None:
                if observed_at > previous['observed_at']:
                    self._conn.execute('''UPDATE review_captures SET observed_at=?,recognized_text=?
                        WHERE id=?''', (observed_at, recognized_text, previous['id']))
                capture_id = previous['id']
            else:
                cursor = self._conn.execute('''INSERT INTO review_captures
                    (friend_id,field,image,observed_at,recognized_text) VALUES (?,?,?,?,?)''',
                    (fid, field, payload, observed_at, recognized_text))
                capture_id = cursor.lastrowid
            self._prune_field_captures_locked(fid, field)
            if commit:
                self._conn.commit()
            return capture_id

    def get_field_captures(self, fid, field):
        _check_field(field)
        with self._lock:
            return [dict(row) for row in self._conn.execute('''SELECT * FROM review_captures
                WHERE friend_id=? AND field=? ORDER BY observed_at DESC,id DESC LIMIT 2''', (fid, field))]

    def get_field_correction(self, fid, field, image_bytes):
        """Return a matching manual value, including '', or None if unconfirmed."""
        _check_field(field)
        with self._lock:
            for row in self._conn.execute('''SELECT image,corrected_text FROM review_captures
                    WHERE friend_id=? AND field=? AND corrected_text IS NOT NULL
                    ORDER BY observed_at DESC,id DESC''', (fid, field)):
                if same_field_image(row['image'], image_bytes):
                    return row['corrected_text']
        return None

    def clear_field_pending(self, fid, field, *, corrected_text=_UNSET, commit=True):
        """Confirm the latest crop's manual text, retaining both historical crops."""
        _check_field(field)
        with self._lock:
            friend = self.get_friend(fid)
            if friend is None:
                raise ValueError('好友记录不存在')
            text = friend.get(field) if corrected_text is _UNSET else corrected_text
            if text is not None and not isinstance(text, str):
                raise ValueError('人工校准文字必须是字符串或空值')
            if text is not None:
                self._conn.execute('''UPDATE review_captures SET corrected_text=? WHERE id=(
                    SELECT id FROM review_captures WHERE friend_id=? AND field=?
                    ORDER BY observed_at DESC,id DESC LIMIT 1)''', (text, fid, field))
            self._conn.execute(f'UPDATE friends SET {field}_pending=0 WHERE id=?', (fid,))
            if commit:
                self._conn.commit()

    def _merge_evidence_locked(self, keep_id, other_id, uid):
        self._conn.execute('UPDATE profile_captures SET friend_id=? WHERE friend_id=?', (keep_id, other_id))
        self._prune_profile_captures_locked(keep_id, uid)
        self._conn.execute('UPDATE review_captures SET friend_id=? WHERE friend_id=?', (keep_id, other_id))
        for field in _FIELDS:
            self._prune_field_captures_locked(keep_id, field)

    def _delete_evidence_locked(self, ids=None):
        if ids is not None:
            ids = tuple(ids)
            if not ids:
                return
        for table in ('profile_captures', 'review_captures'):
            if ids is None:
                self._conn.execute(f'DELETE FROM {table}')
            else:
                marks = ','.join('?' for _ in ids)
                self._conn.execute(f'DELETE FROM {table} WHERE friend_id IN ({marks})', ids)

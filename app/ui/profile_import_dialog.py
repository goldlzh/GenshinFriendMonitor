"""Clipboard profile review. OCR runs off the UI thread; saving remains explicit."""
from __future__ import annotations

import re

import numpy as np
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QThread, Qt
from PySide6.QtGui import QImage, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout,
    QHBoxLayout, QScrollArea, QSizePolicy,
)

from app.profile_import import recognize_profile


class ProfileReader(QThread):
    def __init__(self, image, parent=None):
        super().__init__(parent)
        self.image = image
        self.draft = None
        self.error = ''

    def run(self):
        try:
            self.draft = recognize_profile(self.image)
        except Exception as error:
            self.error = str(error)


class ProfilePreview(QLabel):
    def __init__(self, text):
        super().__init__(text)
        self.original = QPixmap()
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.setMinimumSize(200, 140)
        self.setMaximumHeight(310)

    def set_image(self, image):
        self.original = QPixmap.fromImage(image)
        self._scale()

    def _scale(self):
        if not self.original.isNull():
            size = self.contentsRect().adjusted(8, 8, -8, -8).size()
            self.setPixmap(self.original.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale()


class ProfileImportDialog(QDialog):
    def __init__(self, friends, account_uid, parent=None, *, image=None):
        super().__init__(parent)
        self.setWindowTitle('粘贴好友主页 · 核对后录入')
        available = self.screen().availableGeometry()
        self.resize(min(800, available.width()-40), min(790, available.height()-60))
        self.friends = list(friends)
        self.worker = None
        self.image_bytes = None
        self._cancel_requested = False
        root = QVBoxLayout(self)
        intro = QLabel(f'保存到账号 {account_uid}。识别 UID、昵称、冒险等级和签名，请对照截图核对。')
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.paste_button = QPushButton('粘贴主页截图（Ctrl+V）')
        self.paste_button.clicked.connect(self.paste_clipboard)
        self.original_button = QPushButton('查看原图')
        self.original_button.setEnabled(False)
        self.original_button.clicked.connect(self.show_original)
        image_actions = QHBoxLayout()
        image_actions.addWidget(self.paste_button, 1)
        image_actions.addWidget(self.original_button)
        root.addLayout(image_actions)
        self.preview = ProfilePreview('截图复制到剪贴板后，点击上方按钮粘贴。\n支持完整游戏画面或裁剪后的完整主页。')
        self.preview.setFocusPolicy(Qt.StrongFocus)
        self.preview.setStyleSheet('background: #f2f3f5; border: 1px solid #d6d9dd; padding: 6px;')
        root.addWidget(self.preview)
        self.status = QLabel('录入资料不会推断在线状态，也不会替代完整好友名单扫描。')
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        form = QFormLayout()
        self.target = QComboBox()
        self.target.addItem('按 UID 录入：已有则更新，否则新建档案', None)
        for friend in self.friends:
            if not friend.get('uid'):
                name = friend.get('nickname') or '昵称待校准'
                self.target.addItem(f"补充现有档案：{name}（#{friend['id']}，UID 待补）", friend['id'])
        self.uid = QLineEdit()
        self.uid.setPlaceholderText('9 位好友 UID，必填')
        self.nickname = QLineEdit()
        self.nickname.setPlaceholderText('核对昵称，必填')
        self.level = QSpinBox()
        self.level.setRange(0, 60)
        self.level.setSpecialValueText('未知，保留已有等级')
        self.signature = QPlainTextEdit()
        self.signature.setMaximumHeight(65)
        self.signature.setPlaceholderText('未识别时可手动填写；不更新签名则保留原值')
        self.update_signature = QCheckBox('保存 / 更新签名（留空表示确认为空签名）')
        self.signature.textChanged.connect(lambda: self.update_signature.setChecked(True))
        self.save_image = QCheckBox('保存这张主页截图（每人保留最新两版资料）')
        self.save_image.setChecked(True)
        form.addRow('录入到：', self.target)
        form.addRow('好友 UID：', self.uid)
        form.addRow('昵称：', self.nickname)
        form.addRow('冒险等级：', self.level)
        form.addRow('签名：', self.signature)
        form.addRow('', self.update_signature)
        form.addRow('', self.save_image)
        root.addLayout(form)
        self.target_hint = QLabel()
        self.target_hint.setWordWrap(True)
        self.uid.textChanged.connect(self._update_hint)
        self.nickname.textChanged.connect(self._update_hint)
        self.target.currentIndexChanged.connect(self._update_hint)
        root.addWidget(self.target_hint)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Save).setText('确认录入')
        self.buttons.button(QDialogButtonBox.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)
        self._set_busy(False)
        self._update_hint()
        if image is not None and not image.isNull():
            self.set_image(image)

    def _update_hint(self):
        uid = self.uid.text().strip()
        known = next((friend for friend in self.friends if uid and friend.get('uid') == uid), None)
        if self.target.currentData() is not None:
            text = '将为选中的档案补齐 UID；如该 UID 已有档案，将合并这两条记录并保留历史。'
        elif known:
            text = f"将更新已有档案：{known.get('nickname') or uid}。在线状态和历史时长保留。"
        else:
            text = '新建资料可在“显示历史档案”中查看；后续扫描确认后进入当前名单。'
            if any(not friend.get('uid') and friend.get('nickname') == self.nickname.text().strip()
                   for friend in self.friends):
                text += ' 已有同名的 UID 待补档案，可在“录入到”中明确选择。'
        self.target_hint.setText(text)

    def _set_busy(self, busy):
        for widget in (self.paste_button, self.target, self.uid, self.nickname, self.level,
                       self.signature, self.update_signature, self.save_image):
            widget.setEnabled(not busy)
        self.buttons.button(QDialogButtonBox.Save).setEnabled(not busy and self.image_bytes is not None)

    def paste_clipboard(self):
        if self.worker is not None and self.worker.isRunning():
            return
        image = QApplication.clipboard().image()
        if image.isNull():
            self.status.setText('剪贴板中没有图片。请先复制好友主页截图，再粘贴。')
            return
        self.set_image(image)

    def set_image(self, image):
        if self.worker is not None and self.worker.isRunning():
            return
        image = image.convertToFormat(QImage.Format_RGB888)
        if image.isNull():
            return
        array = np.frombuffer(image.bits(), dtype=np.uint8).reshape(image.height(), image.bytesPerLine())
        rgb = array[:, :image.width()*3].reshape(image.height(), image.width(), 3).copy()
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        buffer.open(QIODevice.WriteOnly)
        saved = image.save(buffer, 'PNG')
        buffer.close()
        if not saved:
            self.status.setText('截图无法读取，请重新复制。')
            return
        self.image_bytes = bytes(encoded)
        self.preview.set_image(image)
        self.original_button.setEnabled(True)
        self.uid.clear()
        self.nickname.clear()
        self.level.setValue(0)
        self.signature.clear()
        self.update_signature.setChecked(False)
        self.target.setCurrentIndex(0)
        self.status.setText('正在识别主页资料……首次加载 OCR 可能需要几秒。')
        self._set_busy(True)
        if self.worker is not None:
            self.worker.deleteLater()
        self.worker = ProfileReader(rgb, self)
        self.worker.finished.connect(self._recognized)
        self.worker.start()

    def show_original(self):
        if self.image_bytes is None:
            return
        viewer = QDialog(self)
        viewer.setWindowTitle('主页原图 · 可滚动查看')
        available = self.screen().availableGeometry()
        viewer.resize(min(1100, available.width()-40), min(760, available.height()-60))
        layout = QVBoxLayout(viewer)
        scroll = QScrollArea()
        picture = QLabel()
        pixmap = QPixmap()
        pixmap.loadFromData(self.image_bytes)
        picture.setPixmap(pixmap)
        scroll.setWidget(picture)
        layout.addWidget(scroll)
        viewer.exec()
        viewer.deleteLater()

    def _recognized(self):
        if self._cancel_requested:
            super().reject()
            return
        self._set_busy(False)
        draft = self.worker.draft
        if draft is None:
            self.status.setText('自动识别未完成，可对照截图手动填写。' + self.worker.error)
            return
        self.uid.setText(draft.uid)
        self.nickname.setText(draft.nickname)
        self.level.setValue(draft.level or 0)
        self.signature.setPlainText(draft.signature or '')
        self.update_signature.setChecked(draft.signature is not None)
        self.status.setText(('未可靠识别：' + '、'.join(draft.missing) + '。请对照截图补充或保持未知。')
                            if draft.missing else '识别完成。请核对 UID、昵称和签名后点击“确认录入”。')

    def values(self):
        return dict(uid=self.uid.text().strip(), nickname=self.nickname.text().strip(),
                    level=self.level.value() or None, signature=self.signature.toPlainText().strip(),
                    image_bytes=self.image_bytes, friend_id=self.target.currentData(),
                    signature_confirmed=self.update_signature.isChecked(), save_image=self.save_image.isChecked())

    def accept(self):
        if self.worker is not None and self.worker.isRunning():
            return
        if self.image_bytes is None:
            self.status.setText('请先粘贴好友主页截图。')
        elif not re.fullmatch(r'[1-9][0-9]{8}', self.uid.text().strip()):
            self.status.setText('请核对并填写正确的 9 位好友 UID。')
            self.uid.setFocus()
        elif not self.nickname.text().strip():
            self.status.setText('请核对并填写好友昵称。')
            self.nickname.setFocus()
        else:
            super().accept()

    def reject(self):
        if self.worker is not None and self.worker.isRunning():
            self._cancel_requested = True
            self.status.setText('正在结束识别，完成后关闭；本次资料不会保存。')
            self.buttons.setEnabled(False)
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Paste):
            self.paste_clipboard()
            event.accept()
        else:
            super().keyPressEvent(event)

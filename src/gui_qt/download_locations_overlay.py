"""Borderless in-window overlay to manage download scan locations — toggle the
default Downloads folder, toggle the per-game cache, and add/remove extra
folders. Reads/writes the same Utils.download_locations settings as the Tk app
(backward compatible). ``on_done(True)`` on Save, ``on_done(False)`` on
cancel / Esc / backdrop click.

Modeled on ``gui_qt/confirm_overlay.py`` (replaces the old QDialog version —
gaming-mode opens top-level windows behind the app).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QListWidget,
    QPushButton, QFrame,
)

import Utils.download_locations as dl
from gui_qt.theme_qt import active_palette, _c


class DownloadLocationsOverlay(QWidget):
    CARD_W = 560
    CARD_H = 420

    # pick_folder's callback fires on the portal WORKER thread; marshal the
    # result to the GUI thread via this Signal before touching any widget.
    _folder_picked = Signal(object)

    def __init__(self, host: QWidget, on_done):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        p = active_palette()

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("DownloadLocationsCard")
        self._card.setStyleSheet(
            f"#DownloadLocationsCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        title_lbl = QLabel(self.tr("Download locations"))
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        intro = QLabel(self.tr("Folders scanned for mod archives in the Downloads tab."))
        intro.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        intro.setWordWrap(True)
        v.addWidget(intro)

        self._default_cb = QCheckBox()
        v.addWidget(self._default_cb)
        self._cache_cb = QCheckBox(self.tr("Scan this game's download cache"))
        v.addWidget(self._cache_cb)

        line = QFrame(); line.setFrameShape(QFrame.HLine); v.addWidget(line)
        extra_lbl = QLabel(self.tr("Additional folders:"))
        extra_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')};")
        v.addWidget(extra_lbl)
        self._list = QListWidget()
        v.addWidget(self._list, 1)

        row = QHBoxLayout()
        add = QPushButton(self.tr("Add folder…"))
        add.setObjectName("FormButton")
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(self._add)
        rem = QPushButton(self.tr("Remove selected"))
        rem.setObjectName("FormButton")
        rem.setCursor(Qt.PointingHandCursor)
        rem.clicked.connect(self._remove)
        row.addWidget(add); row.addWidget(rem); row.addStretch(1)
        v.addLayout(row)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(False))
        bar.addWidget(cancel)
        save = QPushButton(self.tr("Save"))
        save.setObjectName("PrimaryButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._save)
        bar.addWidget(save)
        v.addLayout(bar)

        self._folder_picked.connect(self._on_folder_picked)
        self._load()

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    @classmethod
    def show_over(cls, host, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, on_done)

    # -- settings ------------------------------------------------------------
    def _load(self):
        default = dl.get_default_downloads_dir()
        self._default_cb.setText(self.tr("Scan default Downloads folder ({0})").format(default))
        self._default_cb.setChecked(not dl.is_default_downloads_disabled())
        self._cache_cb.setChecked(not dl.is_cache_default_disabled())
        self._list.clear()
        for p in dl.load_extra_download_locations():
            self._list.addItem(p)

    def _add(self):
        from Utils.portal_filechooser import pick_folder
        pick_folder("Add download folder",
                    lambda path: self._folder_picked.emit(path))

    def _on_folder_picked(self, path):
        if not path or self._done:
            return
        folder = str(path)
        existing = {self._list.item(i).text()
                    for i in range(self._list.count())}
        if folder not in existing:
            self._list.addItem(folder)

    def _remove(self):
        for it in self._list.selectedItems():
            self._list.takeItem(self._list.row(it))

    def _save(self):
        extras = [self._list.item(i).text() for i in range(self._list.count())]
        dl.write_config(
            extras,
            not self._default_cb.isChecked(),
            not self._cache_cb.isChecked())
        self._finish(True)

    # -- internals ----------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(420, w), max(320, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _finish(self, result: bool):
        if self._done:
            return
        self._done = True
        self._host.removeEventFilter(self)
        cb = self._on_done
        self.hide()
        self.deleteLater()
        if cb is not None:
            cb(result)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._finish(False)
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if not self._card.geometry().contains(event.position().toPoint()):
            self._finish(False)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

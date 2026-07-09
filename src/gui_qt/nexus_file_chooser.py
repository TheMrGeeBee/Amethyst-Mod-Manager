"""In-window overlay shown when a Nexus mod has more than one MAIN file — the
user picks which one to install. NOT a separate window: on Steam Deck gaming mode
a top-level window (even a QDialog) can open behind the app, so this is a
borderless child widget that covers the host with a dimmed backdrop and a centered
card. Qt equivalent of the Tk `_FileChooserOverlay`.

Usage:
    NexusFileChooser.show_over(host, mod_name, files, on_pick=callback)
`on_pick(file_or_None)` is called when the user picks (Install / double-click) or
cancels (Cancel / backdrop click).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QFrame,
)

from gui_qt.theme_qt import active_palette, _c


def _fmt_size_bytes(b: int) -> str:
    b = int(b or 0)
    if b <= 0:
        return ""
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if b >= div:
            return f"{b / div:.1f}{unit}"
    return f"{b}B"


class NexusFileChooser(QWidget):
    """A dimmed, click-absorbing backdrop with a centered card. Lives inside the
    host widget (no separate top-level window)."""

    CARD_W = 560
    CARD_H = 440

    def __init__(self, host: QWidget, mod_name: str, files: list, on_pick):
        super().__init__(host)
        self._host = host
        self._on_pick = on_pick
        self._done = False
        p = active_palette()

        # Full-host dimmed backdrop (absorbs clicks → cancel). Scope the
        # stylesheet to this widget's objectName so the semi-transparent black
        # does NOT cascade into the card's child labels (which would paint a
        # black band behind the text).
        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        # Centered card.
        self._card = QFrame(self)
        self._card.setObjectName("_FileChooserCard")
        self._card.setStyleSheet(
            f"#_FileChooserCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)

        hdr = QLabel(self.tr("'{0}' has multiple main files.").format(mod_name))
        hdr.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        hdr.setWordWrap(True)
        v.addWidget(hdr)
        sub = QLabel(self.tr("Select which file to install:"))
        sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        v.addWidget(sub)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(
            f"QListWidget {{ font-size:14px; background:{_c(p,'BG_LIST')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; }}"
            f"QListWidget::item {{ padding:8px 6px; color:{_c(p,'TEXT_MAIN')};"
            f" border-bottom:1px solid {_c(p,'BORDER')}; }}"
            f"QListWidget::item:selected {{ background:{_c(p,'BG_SELECT')};"
            f" color:{_c(p,'TEXT_ON_ACCENT')}; }}")
        for f in files:
            name = f.name or f.file_name or f"File {f.file_id}"
            size = (f.size_in_bytes or 0) or (f.size_kb * 1024 if f.size_kb else 0)
            bits = []
            if f.version:
                bits.append(f"v{f.version}")
            sz = _fmt_size_bytes(size)
            if sz:
                bits.append(sz)
            detail = "   —   ".join(bits)
            item = QListWidgetItem(f"{name}\n{detail}" if detail else name)
            item.setData(Qt.UserRole, f)
            self._list.addItem(item)
        self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(lambda _i: self._pick())
        v.addWidget(self._list, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        install = QPushButton(self.tr("Install"))
        install.setObjectName("PrimaryButton")    # blue accent, matches other overlays
        install.setCursor(Qt.PointingHandCursor)
        install.clicked.connect(self._pick)
        bar.addWidget(install)
        v.addLayout(bar)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    @classmethod
    def show_over(cls, host, mod_name, files, on_pick):
        # Anchor to the top-level window so the backdrop covers the whole app.
        top = host.window() if host is not None else None
        return cls(top or host, mod_name, files, on_pick)

    # -- internals ----------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(320, w), max(240, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _pick(self):
        item = self._list.currentItem()
        self._finish(item.data(Qt.UserRole) if item is not None else None)

    def _finish(self, result):
        if self._done:
            return
        self._done = True
        self._host.removeEventFilter(self)
        cb = self._on_pick
        self.hide()
        self.deleteLater()
        if cb is not None:
            cb(result)

    def mousePressEvent(self, event):
        # Click on the dim backdrop (outside the card) cancels.
        if not self._card.geometry().contains(event.position().toPoint()):
            self._finish(None)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._finish(None)
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

"""Generic in-window list-picker overlay.

A dimmed borderless child overlay (NOT a top-level window — gaming-mode opens
top-levels behind the app) with a centered card: title, a scrollable list of
choices, and a Cancel button. Double-click or Select → ``on_pick(value)``; Cancel
/ Esc / backdrop click → ``on_pick(None)``.

Used for choosing a target profile or a target separator from the modlist menu.
Items are ``(display_label, value)`` pairs. Modeled on ``nexus_file_chooser.py``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QFrame,
)

from gui_qt.theme_qt import active_palette, _c


class ListPickerOverlay(QWidget):
    CARD_W = 420
    CARD_H = 420

    def __init__(self, host: QWidget, title: str, items, on_pick,
                 select_label: str = "Select"):
        super().__init__(host)
        self._host = host
        self._on_pick = on_pick
        self._done = False
        p = active_palette()

        self.setStyleSheet("background: rgba(0,0,0,140);")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("_PickerCard")
        self._card.setStyleSheet(
            f"#_PickerCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)

        hdr = QLabel(title)
        hdr.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        hdr.setWordWrap(True)
        v.addWidget(hdr)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(
            f"QListWidget {{ font-size:14px; }}"
            f"QListWidget::item {{ padding:7px 6px;"
            f" border-bottom:1px solid {_c(p,'BORDER')}; }}")
        for label, value in items:
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, value)
            self._list.addItem(it)
        if self._list.count():
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
        sel = QPushButton(select_label)
        sel.setObjectName("PrimaryButton")
        sel.setCursor(Qt.PointingHandCursor)
        sel.clicked.connect(self._pick)
        bar.addWidget(sel)
        v.addLayout(bar)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()
        self._list.setFocus()

    @classmethod
    def show_over(cls, host, title, items, on_pick, select_label="Select"):
        top = host.window() if host is not None else None
        return cls(top or host, title, items, on_pick, select_label=select_label)

    # -- internals ----------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(300, w), max(220, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _pick(self):
        item = self._list.currentItem()
        self._finish(item.data(Qt.UserRole) if item is not None else None)

    def _finish(self, result):
        if self._done:
            return
        self._done = True
        try:
            self._host.removeEventFilter(self)
        except Exception:
            pass
        cb = self._on_pick
        self.hide()
        self.deleteLater()
        if cb is not None:
            cb(result)

    def mousePressEvent(self, event):
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

"""Generic in-window list-picker overlay.

A dimmed borderless child overlay (see gui_qt/overlay_base.py) with a centered
card: title, a scrollable list of choices, and a Cancel button. Double-click or
Select → ``on_pick(value)``; Cancel / Esc / backdrop click → ``on_pick(None)``.

Used for choosing a target profile or a target separator from the modlist menu.
Items are ``(display_label, value)`` pairs.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton,
)

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class ListPickerOverlay(OverlayBase):
    CARD_W = 420
    CARD_H = 420
    MIN_W = 300
    MIN_H = 220
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, title: str, items, on_pick,
                 select_label: str = "Select"):
        super().__init__(host, on_done=on_pick)
        p = active_palette()

        _card, v = self._make_card("_PickerCard", margins=(16, 14, 16, 14))

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

        self._present()
        self._list.setFocus()

    @classmethod
    def show_over(cls, host, title, items, on_pick, select_label="Select"):
        top = host.window() if host is not None else None
        return cls(top or host, title, items, on_pick, select_label=select_label)

    # -- internals ----------------------------------------------------------
    def _pick(self):
        item = self._list.currentItem()
        self._finish(item.data(Qt.UserRole) if item is not None else None)

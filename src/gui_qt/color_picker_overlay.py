"""Borderless in-window colour-picker overlay.

A dimmed child overlay (NOT a top-level window — gaming-mode opens top-levels
behind the app) with a centered card embedding Qt's non-native ``QColorDialog``
as a plain child widget (``Qt.Widget`` flags + ``NoButtons``), plus our own
Cancel / OK bar. ``on_done(QColor)`` on confirm, ``on_done(None)`` on cancel.

Modeled on ``gui_qt/confirm_overlay.py``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QColorDialog,
)

from gui_qt.theme_qt import active_palette, _c


class ColorPickerOverlay(QWidget):
    CARD_W = 620
    CARD_H = 480

    def __init__(self, host: QWidget, title: str, initial: QColor, on_done):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        p = active_palette()

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("ColorPickerCard")
        self._card.setStyleSheet(
            f"#ColorPickerCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        self._picker = QColorDialog(self._card)
        self._picker.setWindowFlags(Qt.Widget)
        self._picker.setOptions(QColorDialog.DontUseNativeDialog
                                | QColorDialog.NoButtons)
        if initial is not None and initial.isValid():
            self._picker.setCurrentColor(initial)
        v.addWidget(self._picker, 1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        ok = QPushButton(self.tr("OK"))
        ok.setObjectName("PrimaryButton")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(lambda: self._finish(self._picker.currentColor()))
        bar.addWidget(ok)
        v.addLayout(bar)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    @classmethod
    def show_over(cls, host, title, initial, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, title, initial, on_done)

    # -- internals ----------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(420, w), max(360, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _finish(self, result):
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
            self._finish(None)
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if not self._card.geometry().contains(event.position().toPoint()):
            self._finish(None)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

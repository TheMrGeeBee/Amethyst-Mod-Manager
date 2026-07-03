"""Generic borderless in-window confirmation overlay.

A dimmed child overlay (NOT a top-level window — gaming-mode opens top-levels
behind the app) with a centered card: title, body text, and Confirm / Cancel
buttons. ``on_done(True)`` on confirm, ``on_done(False)`` on cancel / Esc.

Modeled on ``gui_qt/remove_previous_overlay.py`` but parameterised so it can back
any yes/no prompt (e.g. the Profile Settings remove confirmations).

Pass ``cancel_label=None`` for a single-button message card (OK-only) — the
in-app replacement for ``QMessageBox.warning``/``critical``/``information``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
)

from gui_qt.theme_qt import active_palette, _c


class ConfirmOverlay(QWidget):
    CARD_W = 480
    CARD_H = 240

    def __init__(self, host: QWidget, title: str, body: str, on_done,
                 confirm_label: str = "Remove",
                 cancel_label: str | None = "Cancel",
                 danger: bool = True,
                 card_h: int | None = None):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        self._card_h = card_h if card_h is not None else self.CARD_H
        p = active_palette()

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("ConfirmCard")
        self._card.setStyleSheet(
            f"#ConfirmCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}"
            f" #DangerButton {{ background:#6b3333; color:#fff;"
            f" border:none; border-radius:4px; padding:6px 14px;"
            f" font-weight:600; }}"
            f" #DangerButton:hover {{ background:#8c4444; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        body_lbl = QLabel(body)
        body_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        body_lbl.setWordWrap(True)
        v.addWidget(body_lbl)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        if cancel_label is not None:
            cancel = QPushButton(cancel_label)
            cancel.setObjectName("FormButton")
            cancel.setCursor(Qt.PointingHandCursor)
            cancel.clicked.connect(lambda: self._finish(False))
            bar.addWidget(cancel)
        confirm = QPushButton(confirm_label)
        confirm.setObjectName("DangerButton" if danger else "PrimaryButton")
        confirm.setCursor(Qt.PointingHandCursor)
        confirm.clicked.connect(lambda: self._finish(True))
        bar.addWidget(confirm)
        v.addLayout(bar)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    @classmethod
    def show_over(cls, host, title, body, on_done, **kw):
        top = host.window() if host is not None else None
        return cls(top or host, title, body, on_done, **kw)

    @classmethod
    def show_message(cls, host, title, body, on_done=None, ok_label="OK"):
        """OK-only message card (QMessageBox.warning/critical replacement)."""
        return cls.show_over(host, title, body, on_done,
                             confirm_label=ok_label, cancel_label=None,
                             danger=False)

    # -- internals ----------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self._card_h, self._host.height() - 40)
        self._card.setFixedSize(max(340, w), max(180, h))
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

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

"""Generic borderless in-window confirmation overlay.

A dimmed child overlay (see gui_qt/overlay_base.py) with a centered card:
title, body text, and Confirm / Cancel buttons. ``on_done(True)`` on confirm,
``on_done(False)`` on cancel / Esc.

Pass ``cancel_label=None`` for a single-button message card (OK-only) — the
in-app replacement for ``QMessageBox.warning``/``critical``/``information``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c, contrast_text


class ConfirmOverlay(OverlayBase):
    CARD_W = 480
    CARD_H = 240
    MIN_H = 180
    ESC_RESULT = False

    def __init__(self, host: QWidget, title: str, body: str, on_done,
                 confirm_label: str = "Remove",
                 cancel_label: str | None = "Cancel",
                 danger: bool = True,
                 card_h: int | None = None):
        super().__init__(host, on_done=on_done, card_h=card_h)
        p = active_palette()

        _card, v = self._make_card(
            "ConfirmCard",
            extra_qss=(
                f" #DangerButton {{ background:{_c(p,'BTN_DANGER')};"
                f" color:{contrast_text(_c(p,'BTN_DANGER'))};"
                f" border:none; border-radius:4px; padding:6px 14px;"
                f" font-weight:600; }}"
                f" #DangerButton:hover {{ background:{_c(p,'BTN_DANGER_HOV')}; }}"))

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

        self._present()

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

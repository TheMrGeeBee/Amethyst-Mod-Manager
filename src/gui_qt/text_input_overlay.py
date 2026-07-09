"""Generic borderless in-window text-input overlay.

A dimmed child overlay (see gui_qt/overlay_base.py) with a centered card:
title, prompt, a line edit and Cancel / OK buttons. ``on_done(text)`` on
confirm, ``on_done(None)`` on cancel / Esc / backdrop click. Replaces the
native ``QInputDialog.getText`` / ``getInt`` prompts; pass a ``QIntValidator``
for numeric input.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QLineEdit, QPushButton

from gui_qt.overlay_base import OverlayBase
from gui_qt.theme_qt import active_palette, _c


class TextInputOverlay(OverlayBase):
    CARD_W = 480
    CARD_H = 190
    CLICK_OUTSIDE_CANCELS = True

    def __init__(self, host: QWidget, title: str, prompt: str, on_done,
                 initial: str = "", ok_label: str = "OK", validator=None):
        super().__init__(host, on_done=on_done)
        p = active_palette()

        _card, v = self._make_card("TextInputCard")

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        prompt_lbl = QLabel(prompt)
        prompt_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        prompt_lbl.setWordWrap(True)
        v.addWidget(prompt_lbl)

        self._edit = QLineEdit()
        if validator is not None:
            self._edit.setValidator(validator)
        self._edit.setText(initial)
        self._edit.selectAll()
        self._edit.returnPressed.connect(self._confirm)
        v.addWidget(self._edit)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish(None))
        bar.addWidget(cancel)
        ok = QPushButton(ok_label)
        ok.setObjectName("PrimaryButton")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._confirm)
        bar.addWidget(ok)
        v.addLayout(bar)

        self._present()
        self._edit.setFocus()

    @classmethod
    def show_over(cls, host, title, prompt, on_done, **kw):
        top = host.window() if host is not None else None
        return cls(top or host, title, prompt, on_done, **kw)

    # -- internals ----------------------------------------------------------
    def _confirm(self):
        self._finish(self._edit.text())

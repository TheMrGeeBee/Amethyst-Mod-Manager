"""Generic borderless in-window text-input overlay.

A dimmed child overlay (NOT a top-level window — gaming-mode opens top-levels
behind the app) with a centered card: title, prompt, a line edit and
Cancel / OK buttons. ``on_done(text)`` on confirm, ``on_done(None)`` on
cancel / Esc / backdrop click. Replaces the native ``QInputDialog.getText`` /
``getInt`` prompts; pass a ``QIntValidator`` for numeric input.

Modeled on ``gui_qt/confirm_overlay.py``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QFrame,
)

from gui_qt.theme_qt import active_palette, _c


class TextInputOverlay(QWidget):
    CARD_W = 480
    CARD_H = 190

    def __init__(self, host: QWidget, title: str, prompt: str, on_done,
                 initial: str = "", ok_label: str = "OK", validator=None):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        p = active_palette()

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("TextInputCard")
        self._card.setStyleSheet(
            f"#TextInputCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

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

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()
        self._edit.setFocus()

    @classmethod
    def show_over(cls, host, title, prompt, on_done, **kw):
        top = host.window() if host is not None else None
        return cls(top or host, title, prompt, on_done, **kw)

    # -- internals ----------------------------------------------------------
    def _confirm(self):
        self._finish(self._edit.text())

    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(340, w), max(160, h))
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

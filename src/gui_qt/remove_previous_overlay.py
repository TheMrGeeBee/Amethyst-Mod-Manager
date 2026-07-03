"""In-window overlay shown after a Change Version install lands as a NEW mod
(different folder name): offer to remove the previous version. Qt equivalent of
the Tk ``_prompt_remove_previous_version`` CTkAlert — a dimmed child overlay (NOT
a top-level window), like `gui_qt/set_prefix_overlay.py`.

`on_done(result)` is called with:
    "remove"  — delete the old mod; the new one inherits its modlist slot + state
    "keep"    — leave both mods
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
)

from gui_qt.theme_qt import active_palette, _c


class RemovePreviousOverlay(QWidget):
    CARD_W = 480
    CARD_H = 260

    def __init__(self, host: QWidget, old_name: str, new_name: str, on_done):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        p = active_palette()

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("RemovePrevCard")
        self._card.setStyleSheet(
            f"#RemovePrevCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        title = QLabel(self.tr("Remove previous version?"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        body = QLabel(
            self.tr("'{0}' was installed as a new mod (different folder name) because it did not replace '{1}'.\n\nRemove the previous version '{2}'? The new mod will take its position in the modlist.\n\nChoose Keep if this is an optional/alternative variant rather than a replacement.").format(new_name, old_name, old_name))
        body.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        body.setWordWrap(True)
        v.addWidget(body)
        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        keep = QPushButton(self.tr("Keep"))
        keep.setObjectName("FormButton")        # neutral, like other buttons
        keep.setCursor(Qt.PointingHandCursor)
        keep.clicked.connect(lambda: self._finish("keep"))
        bar.addWidget(keep)
        remove = QPushButton(self.tr("Remove"))
        remove.setObjectName("PrimaryButton")   # accent primary action
        remove.setCursor(Qt.PointingHandCursor)
        remove.clicked.connect(lambda: self._finish("remove"))
        bar.addWidget(remove)
        v.addLayout(bar)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    @classmethod
    def show_over(cls, host, old_name, new_name, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, old_name, new_name, on_done)

    # -- internals ----------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(340, w), max(200, h))
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
            self._finish("keep")
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

"""In-window overlay shown when installing a mod whose folder already exists.
Qt equivalent of the Tk ``_ReplaceModDialog`` — a dimmed child overlay (NOT a
top-level window; Steam-Deck gaming mode opens those behind the app), like
`gui_qt/set_prefix_overlay.py` / `gui_qt/nexus_file_chooser.py`.

`on_done(result)` is called with:
    "replace"        — wipe the existing folder + reinstall (keep its position)
    "rename:<name>"  — install as a NEW mod under <name>
    "cancel"         — abort the install
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QFrame,
)

from gui_qt.theme_qt import active_palette, _c


class ModExistsOverlay(QWidget):
    CARD_W = 460
    CARD_H = 240

    def __init__(self, host: QWidget, mod_name: str, conflict: bool, on_done):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        p = active_palette()

        # Scope the dim to THIS widget only (an un-scoped `background:` cascades
        # to every child, overriding the buttons' themed QSS → they render as raw
        # grey Qt buttons).
        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("ExistsCard")
        self._card.setStyleSheet(
            f"#ExistsCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        title = QLabel(self.tr("Mod Already Exists"))
        title.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title)

        if conflict:
            body_text = (f"'{mod_name}' is also already installed.\n"
                         "Pick a different name, or choose another option.")
        else:
            body_text = (f"'{mod_name}' is already installed.\n"
                         "How would you like to handle the existing mod?")
        body = QLabel(body_text)
        body.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
        body.setWordWrap(True)
        v.addWidget(body)

        # Inline rename field (hidden until "Rename…" is pressed).
        self._rename_row = QWidget()
        rr = QHBoxLayout(self._rename_row)
        rr.setContentsMargins(0, 0, 0, 0); rr.setSpacing(6)
        self._entry = QLineEdit()
        self._entry.setPlaceholderText(self.tr("New mod name…"))
        self._entry.setText(mod_name)
        self._entry.returnPressed.connect(self._confirm_rename)
        rr.addWidget(self._entry, 1)
        confirm = QPushButton(self.tr("OK"))
        confirm.setObjectName("PrimaryButton")
        confirm.setCursor(Qt.PointingHandCursor)
        confirm.clicked.connect(self._confirm_rename)
        rr.addWidget(confirm)
        self._rename_row.setVisible(False)
        v.addWidget(self._rename_row)

        v.addStretch(1)

        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")       # neutral, like other buttons
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._finish("cancel"))
        bar.addWidget(cancel)
        rename = QPushButton(self.tr("Rename…"))
        rename.setObjectName("FormButton")
        rename.setCursor(Qt.PointingHandCursor)
        rename.clicked.connect(self._show_rename)
        bar.addWidget(rename)
        replace = QPushButton(self.tr("Replace All"))
        replace.setObjectName("PrimaryButton")   # accent primary action
        replace.setCursor(Qt.PointingHandCursor)
        replace.clicked.connect(lambda: self._finish("replace"))
        bar.addWidget(replace)
        v.addLayout(bar)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    @classmethod
    def show_over(cls, host, mod_name, conflict, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, mod_name, conflict, on_done)

    # -- internals ----------------------------------------------------------
    def _show_rename(self):
        self._rename_row.setVisible(True)
        self._entry.setFocus()
        self._entry.selectAll()

    def _confirm_rename(self):
        from Utils.mod_name_utils import sanitize_mod_folder_name
        name = sanitize_mod_folder_name(self._entry.text().strip())
        if not name:
            return
        self._finish(f"rename:{name}")

    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(320, w), max(180, h))
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
            self._finish("cancel")
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

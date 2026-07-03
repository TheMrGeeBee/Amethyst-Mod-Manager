"""Mod-note editor — borderless in-window overlay.

Edit the free-text note attached to a mod (or apply one to several). A dimmed
child overlay (NOT a top-level window — gaming-mode opens top-levels behind the
app) with a multiline text box, Save / Cancel, and an optional Remove. Qt port of
the Tk note editor (gui/modlist_panel._open_note_editor_by_name / _for_multi).

``on_save(text)`` is called on Save; ``on_remove()`` on Remove. All widgets built
once with real parents.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QTextEdit,
)

from gui_qt.theme_qt import active_palette, _c


class NoteEditorOverlay(QWidget):
    CARD_W = 520
    CARD_H = 320

    def __init__(self, host: QWidget, title: str, initial: str,
                 on_save, on_remove, allow_remove: bool = False):
        super().__init__(host)
        self._host = host
        self._on_save = on_save
        self._on_remove = on_remove
        self._done = False
        p = active_palette()

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("_NoteCard")
        self._card.setStyleSheet(
            f"#_NoteCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(8)

        title_lbl = QLabel(self.tr("Note — {0}").format(title))
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:15px;")
        v.addWidget(title_lbl)

        self._edit = QTextEdit()
        self._edit.setPlainText(initial or "")
        self._edit.setStyleSheet(
            f"QTextEdit {{ background:{_c(p,'BG_LIST')}; color:{_c(p,'TEXT_MAIN')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:4px; }}")
        v.addWidget(self._edit, 1)

        bar = QHBoxLayout()
        if allow_remove:
            rm = QPushButton(self.tr("Remove note"))
            rm.setObjectName("FormButton")
            rm.setCursor(Qt.PointingHandCursor)
            rm.clicked.connect(self._remove)
            bar.addWidget(rm)
        bar.addStretch(1)
        cancel = QPushButton(self.tr("Cancel"))
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self._close)
        bar.addWidget(cancel)
        save = QPushButton(self.tr("Save"))
        save.setObjectName("PrimaryButton")
        save.setCursor(Qt.PointingHandCursor)
        save.clicked.connect(self._save)
        bar.addWidget(save)
        v.addLayout(bar)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()
        self._edit.setFocus()

    @classmethod
    def show_over(cls, host, title, initial, on_save, on_remove,
                  allow_remove=False):
        top = host.window() if host is not None else None
        return cls(top or host, title, initial, on_save, on_remove,
                   allow_remove=allow_remove)

    # -- internals ----------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(360, w), max(220, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _save(self):
        if self._done:
            return
        text = self._edit.toPlainText()
        self._close()
        if self._on_save is not None:
            self._on_save(text)

    def _remove(self):
        if self._done:
            return
        self._close()
        if self._on_remove is not None:
            self._on_remove()

    def _close(self):
        if self._done:
            return
        self._done = True
        try:
            self._host.removeEventFilter(self)
        except Exception:
            pass
        self.hide()
        self.deleteLater()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._close()
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

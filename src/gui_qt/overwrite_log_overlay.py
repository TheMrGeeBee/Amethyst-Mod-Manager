"""Read-only Overwrite-log viewer — borderless in-window overlay.

Shows the files swept into the deploy target's ``overwrite/`` per restore, newest
restore first, parsed from ``.mm_overwrite_log.txt``. Qt port of
``gui/modlist_panel._show_overwrite_log``. A dimmed child overlay (NOT a top-level
window — gaming-mode opens top-levels behind the app) with a scrollable body and a
Close button. All widgets built once with real parents.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QTextEdit,
)

from gui_qt.theme_qt import active_palette, _c


def parse_overwrite_log(text: str) -> "list[tuple[str, list[str]]]":
    """Split the overwrite log into (header, files) sections, newest first.
    Faithful port of gui/modlist_panel._parse_overwrite_log."""
    sections: "list[tuple[str, list[str]]]" = []
    cur_header: "str | None" = None
    cur_files: "list[str]" = []
    for raw in (text or "").splitlines():
        line = raw.rstrip("\n")
        if line.startswith("# "):
            if cur_header is not None:
                sections.append((cur_header, cur_files))
            cur_header = line[2:].strip()
            cur_files = []
        elif line.strip():
            if cur_header is not None:
                cur_files.append(line)
    if cur_header is not None:
        sections.append((cur_header, cur_files))
    sections.reverse()
    return sections


class OverwriteLogOverlay(QWidget):
    CARD_W = 640
    CARD_H = 520

    def __init__(self, host: QWidget, sections: "list[tuple[str, list[str]]]"):
        super().__init__(host)
        self._host = host
        self._done = False
        p = active_palette()

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        self._card = QFrame(self)
        self._card.setObjectName("_OvlLogCard")
        self._card.setStyleSheet(
            f"#_OvlLogCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel(self.tr("Files swept into Overwrite (newest restore first)"))
        title.setStyleSheet(f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:14px;")
        header.addWidget(title)
        header.addStretch(1)
        close = QPushButton(self.tr("Close"))
        close.setObjectName("FormButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._finish)
        header.addWidget(close)
        v.addLayout(header)

        body = QTextEdit()
        body.setReadOnly(True)
        body.setStyleSheet(
            f"QTextEdit {{ background:{_c(p,'BG_LIST')}; color:{_c(p,'TEXT_MAIN')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:4px; }}")
        body.setHtml(self._render(sections, p))
        v.addWidget(body, 1)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()
        self.setFocus()

    @classmethod
    def show_over(cls, host, sections):
        top = host.window() if host is not None else None
        return cls(top or host, sections)

    @staticmethod
    def _render(sections, p) -> str:
        dim = _c(p, "TEXT_DIM")
        main = _c(p, "TEXT_MAIN")
        if not sections:
            return (f"<div style='color:{dim}'>No files have entered overwrite "
                    "yet.</div>")
        parts: list[str] = []
        for hdr, files in sections:
            parts.append(f"<div style='color:{main};font-weight:600;"
                         f"margin-top:8px'>{escape(hdr)}</div>")
            for f in files:
                parts.append(f"<div style='color:{dim};margin-left:12px'>"
                             f"{escape(f)}</div>")
        return "".join(parts)

    # -- internals ----------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(360, w), max(240, h))
        self._card.move((self.width() - self._card.width()) // 2,
                        (self.height() - self._card.height()) // 2)

    def _finish(self):
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
            self._finish()
        else:
            super().keyPressEvent(event)

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

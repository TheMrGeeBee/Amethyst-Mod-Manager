"""Borderless archive-unpack picker overlay (Qt).

Qt counterpart of the Tk ``gui/bsa_unpack_overlay.py``. Lists each plugin in the
selected mod together with every sibling archive (``Foo.bsa``, ``Foo - Main.ba2``,
``Foo - Textures.ba2``) that auto-loads with it; clicking a row's Unpack extracts
that whole group in one go. Archives with no matching plugin get a trailing
"(no matching plugin)" group.

Grouping/size/count come from ``Utils.bsa_pack_ops.collect_unpack_groups`` (shared
with Tk). ``on_done(list[Path])`` is called with the chosen group's archives; the
overlay closes itself. Follows the ``confirm_overlay.py`` convention with a scroll
body (like ``download_locations_overlay.py``).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QScrollArea,
)

import Utils.bsa_pack_ops as ops
from gui_qt.theme_qt import active_palette, _c


class BsaUnpackOverlay(QWidget):
    CARD_W = 620
    CARD_H = 520

    def __init__(self, host: QWidget, *, mod_name: str, mod_dir: Path,
                 plugin_exts, on_done):
        super().__init__(host)
        self._host = host
        self._on_done = on_done
        self._done = False
        p = active_palette()

        self.setObjectName("OverlayBackdrop")
        self.setStyleSheet("#OverlayBackdrop { background: rgba(0,0,0,150); }")
        self.setGeometry(host.rect())

        groups = ops.collect_unpack_groups(mod_dir, plugin_exts)
        all_archives = [a for g in groups for a in g.archives]
        kind_label = ops.unpack_kind_label(all_archives) if all_archives else "Archive"

        self._card = QFrame(self)
        self._card.setObjectName("UnpackCard")
        self._card.setStyleSheet(
            f"#UnpackCard {{ background:{_c(p,'BG_PANEL')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:8px; }}")
        v = QVBoxLayout(self._card)
        v.setContentsMargins(18, 16, 18, 14)
        v.setSpacing(8)

        title_lbl = QLabel(self.tr("Unpack {0} — {1}").format(kind_label, mod_name))
        title_lbl.setStyleSheet(
            f"color:{_c(p,'TEXT_MAIN')}; font-weight:600; font-size:16px;")
        v.addWidget(title_lbl)

        # -- scrollable group list -----------------------------------------
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 6, 0)
        bl.setSpacing(6)
        if not groups:
            empty = QLabel(self.tr("No archive files in this mod folder."))
            empty.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:13px;")
            bl.addWidget(empty)
        else:
            for g in groups:
                bl.addWidget(self._group_row(g, p))
        bl.addStretch(1)
        scroll.setWidget(body)
        v.addWidget(scroll, 1)

        hint = QLabel(
            self.tr("Unpacking extracts every archive under the selected plugin into "
            "this mod's folder, deletes those archives, removes the plugin if "
            "it was a generated stub, and re-enables the unpacked files in the "
            "Mod Files tab."))
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        v.addWidget(hint)

        bar = QHBoxLayout()
        bar.addStretch(1)
        close = QPushButton(self.tr("Close"))
        close.setObjectName("FormButton")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(lambda: self._finish(None))
        bar.addWidget(close)
        v.addLayout(bar)

        host.installEventFilter(self)
        self._reposition()
        self.show()
        self.raise_()

    def _group_row(self, g: ops.UnpackGroup, p) -> QWidget:
        row = QFrame()
        row.setObjectName("UnpackRow")
        row.setStyleSheet(
            f"#UnpackRow {{ background:{_c(p,'BG_DEEP')};"
            f" border:1px solid {_c(p,'BORDER')}; border-radius:6px; }}")
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 8, 10, 8)
        h.setSpacing(10)

        info = QVBoxLayout()
        info.setSpacing(2)
        name = QLabel(g.label)
        name_col = _c(p, "TEXT_DIM") if g.is_orphan else _c(p, "TEXT_MAIN")
        name.setStyleSheet(f"color:{name_col}; font-weight:600;")
        name.setWordWrap(True)
        info.addWidget(name)
        for a in g.archives:
            sub = QLabel(self.tr("  • {0}").format(a.name))
            sub.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
            sub.setWordWrap(True)
            info.addWidget(sub)
        size_mb = g.total_bytes / (1024 * 1024)
        if g.total_files >= 0:
            totals = f"{g.total_files} file(s) — {size_mb:.1f} MiB"
        else:
            totals = f"unreadable — {size_mb:.1f} MiB"
        totals_lbl = QLabel(totals)
        totals_lbl.setStyleSheet(f"color:{_c(p,'TEXT_DIM')}; font-size:11px;")
        info.addWidget(totals_lbl)
        h.addLayout(info, 1)

        btn = QPushButton(self.tr("Unpack"))
        btn.setObjectName("PrimaryButton")
        btn.setCursor(Qt.PointingHandCursor)
        archives = list(g.archives)
        btn.clicked.connect(lambda: self._finish(archives))
        h.addWidget(btn, 0, Qt.AlignTop)
        return row

    @classmethod
    def show_over(cls, host, *, mod_name, mod_dir, plugin_exts, on_done):
        top = host.window() if host is not None else None
        return cls(top or host, mod_name=mod_name, mod_dir=mod_dir,
                   plugin_exts=plugin_exts, on_done=on_done)

    # -- internals ----------------------------------------------------------
    def _reposition(self):
        self.setGeometry(self._host.rect())
        w = min(self.CARD_W, self._host.width() - 40)
        h = min(self.CARD_H, self._host.height() - 40)
        self._card.setFixedSize(max(400, w), max(300, h))
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

    def eventFilter(self, obj, event):
        if obj is self._host and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

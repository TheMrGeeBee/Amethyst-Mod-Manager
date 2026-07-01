"""BAIN sub-package picker — Qt port of gui/bain_dialog.py.

Unlike the FOMOD wizard there are no steps/groups/conditions — just a flat
checklist of sub-packages to merge. Opened (as a tab) when a collection's
deferred BAIN mod needs the user to choose sub-packages.

On OK it calls ``on_done({"selected": [name, ...]})``; Cancel calls
``on_done(None)`` (mirrors the neutral ``resolve_bain`` contract). Default check
state = each sub-package's ``default_selected`` (00-prefixed core packages), or a
restored saved selection when provided.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QScrollArea, QFrame, QTextEdit,
)

from gui_qt.theme_qt import active_palette, _c
from Utils.bain_installer import BainSubPackage


class BainPickerView(QWidget):
    def __init__(self, subpackages: "list[BainSubPackage]", mod_root: str,
                 mod_name: str, on_done, *, readme_text: str | None = None,
                 saved_selections: dict | None = None, parent=None):
        super().__init__(parent)
        self._subpackages = subpackages
        self._mod_root = mod_root
        self._mod_name = (mod_name or "").strip()
        self._on_done = on_done or (lambda _r: None)
        self._done = False
        self._p = active_palette()
        self._boxes: list[tuple[QCheckBox, str]] = []

        saved = None
        if saved_selections and isinstance(saved_selections.get("selected"), list):
            saved = set(saved_selections["selected"])

        self._build(readme_text, saved)

    def _c(self, k):
        return _c(self._p, k)

    def _build(self, readme_text, saved):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        title = QLabel(f"Choose sub-packages — {self._mod_name}"
                       if self._mod_name else "Choose sub-packages")
        title.setStyleSheet(
            f"color:{self._c('TEXT_MAIN')}; font-weight:600; font-size:15px;")
        root.addWidget(title)

        subtitle = QLabel("This mod is a BAIN package. Select which sub-packages "
                          "to install.")
        subtitle.setStyleSheet(f"color:{self._c('TEXT_DIM')}; font-size:12px;")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        # Optional readme pane.
        if readme_text:
            ro = QTextEdit()
            ro.setReadOnly(True)
            ro.setPlainText(readme_text)
            ro.setMaximumHeight(140)
            ro.setStyleSheet(
                f"QTextEdit {{ background:{self._c('BG_LIST')};"
                f" color:{self._c('TEXT_DIM')}; border:1px solid {self._c('BORDER')};"
                f" border-radius:4px; }}")
            root.addWidget(ro)

        # Checklist (scrollable).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        body = QFrame()
        body.setObjectName("_BainBody")
        body.setStyleSheet(
            f"#_BainBody {{ background:{self._c('BG_PANEL')};"
            f" border:1px solid {self._c('BORDER')}; border-radius:6px; }}")
        blay = QVBoxLayout(body)
        blay.setContentsMargins(10, 8, 10, 8)
        blay.setSpacing(4)
        for pkg in self._subpackages:
            checked = (pkg.name in saved) if saved is not None else pkg.default_selected
            cb = QCheckBox(pkg.display_name or pkg.name)
            cb.setChecked(bool(checked))
            cb.setStyleSheet(f"color:{self._c('TEXT_MAIN')}; font-size:13px;")
            blay.addWidget(cb)
            self._boxes.append((cb, pkg.name))
        blay.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # Buttons.
        bar = QHBoxLayout()
        bar.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self._cancel)
        bar.addWidget(cancel)
        ok = QPushButton("Install")
        ok.setObjectName("PrimaryButton")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self._ok)
        bar.addWidget(ok)
        root.addLayout(bar)

    def _ok(self):
        if self._done:
            return
        self._done = True
        selected = [name for cb, name in self._boxes if cb.isChecked()]
        self._on_done({"selected": selected})

    def _cancel(self):
        if self._done:
            return
        self._done = True
        self._on_done(None)

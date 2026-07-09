"""Framework-status banner shown above the Plugins-tab columns.

A thin vertical stack of colored rows, one per framework the active game declares
(SKSE, BepInEx, RED4ext, …), each saying whether it's installed / staged / present
but disabled / missing. Display-only, mirroring the Tk plugin-panel banner. Data
comes from `Utils.framework_detect.detect_frameworks` (toolkit-neutral); this
widget only maps each state to the matching theme colors.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel

from gui_qt.theme_qt import active_palette, _c
from Utils.framework_detect import (
    STATE_INSTALLED, STATE_NOT_DEPLOYED, STATE_NOT_ENABLED, STATE_MISSING,
)

ROW_H = 22

# state → (bg palette key, fg palette key). Dedicated FRAMEWORK_* keys (their own
# "Framework detection" section in the theme editor); seeded from the same colours
# the shared tinted rows used, but independently editable.
_STATE_COLORS = {
    STATE_INSTALLED:    ("FRAMEWORK_INSTALLED_BG", "FRAMEWORK_INSTALLED_FG"),
    STATE_NOT_DEPLOYED: ("FRAMEWORK_STAGED_BG",    "FRAMEWORK_STAGED_FG"),
    STATE_NOT_ENABLED:  ("FRAMEWORK_DISABLED_BG",  "FRAMEWORK_DISABLED_FG"),
    STATE_MISSING:      ("FRAMEWORK_MISSING_BG",   "FRAMEWORK_MISSING_FG"),
}


class FrameworkBanner(QWidget):
    """Call `set_statuses(list[FrameworkStatus])` to (re)build the rows. Hides
    itself when the list is empty so the columns sit flush."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(0, 0, 0, 0)
        self._v.setSpacing(1)
        self.hide()

    def set_statuses(self, statuses) -> None:
        # Clear existing rows.
        while self._v.count():
            it = self._v.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        if not statuses:
            self.hide()
            return
        p = active_palette()
        for st in statuses:
            bg_key, fg_key = _STATE_COLORS.get(st.state, _STATE_COLORS[STATE_MISSING])
            lbl = QLabel(st.message)
            lbl.setFixedHeight(ROW_H)
            lbl.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            lbl.setStyleSheet(
                f"background:{_c(p, bg_key)}; color:{_c(p, fg_key)};"
                f" padding-left:10px;")
            self._v.addWidget(lbl)
        self.show()

"""A thin row of read-only 'pill' stat labels for a panel footer.

Each stat is shown in the same rounded, accent-bordered pill as the Nexus footer
username label (gui_qt/nexus_footer.py:NexusFooterLabel) so the modlist / plugins
stat rows match that look. The bar is data-driven: ``set_stats`` takes an ordered
list of ``(label, value)`` pairs and rebuilds the pills.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel

from gui_qt.theme_qt import active_palette, _c


# Left inset of the pill text — matches the footer buttons' left padding so a
# left-aligned pill's first letter sits at the button's text left edge.
_PILL_TEXT_INSET = 12


class _StatPill(QLabel):
    """One rounded pill: 'Label: value' (borderless). Text is left-aligned with a
    left inset matching the footer buttons, so first letters can line up."""

    def __init__(self, accent: str, parent=None, text_color: str = "#ffffff"):
        super().__init__(parent)
        self._accent = accent
        self.setObjectName("StatPill")
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setStyleSheet(
            f"QLabel#StatPill {{ color: {text_color}; border-radius: 4px;"
            f" padding: 2px {_PILL_TEXT_INSET}px; }}")


class StatsBar(QWidget):
    """A left-aligned row of stat pills. Call ``set_stats([(label, value), …])``
    to (re)populate; pills are reused when the count matches so scrolling/redraw
    stays cheap. ``placeholder`` shows a dim '…' pill before the first update.

    ``spacing`` sets the gap between pills (match the button row's spacing when
    aligning pills under buttons)."""

    def __init__(self, placeholder: str = "", spacing: int = 6, parent=None):
        super().__init__(parent)
        p = active_palette()
        self._accent = _c(p, "ACCENT")
        # Theme foreground so pill text reads on the light BottomBar too.
        self._text_color = _c(p, "TEXT_MAIN")
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(spacing)
        self._pills: list[_StatPill] = []
        self._row.addStretch(1)          # keep pills left-aligned
        if placeholder:
            self.set_stats([("", placeholder)])

    def align_pills_to_widths(self, widths: list) -> None:
        """Give each pill (except the last) a MINIMUM width matching the button
        below it, so short values line up under their buttons — but the pill can
        still grow past that when its text needs more room (so multi-digit
        counters are never clipped; the alignment then degrades gracefully). A
        width of 0/None leaves that pill fully natural-sized."""
        for pill, w in zip(self._pills, widths):
            if w:
                pill.setMinimumWidth(int(w))
            else:
                pill.setMinimumWidth(0)

    def set_stats(self, stats: list) -> None:
        """*stats* = ordered list of (label, value). A blank label renders just
        the value (used for the placeholder)."""
        # Grow / shrink the reused pill pool to match the stat count.
        while len(self._pills) < len(stats):
            pill = _StatPill(self._accent, text_color=self._text_color)
            self._pills.append(pill)
            # Insert before the trailing stretch (which is the last item).
            self._row.insertWidget(self._row.count() - 1, pill)
        while len(self._pills) > len(stats):
            pill = self._pills.pop()
            pill.setParent(None)
            pill.deleteLater()
        for pill, (label, value) in zip(self._pills, stats):
            pill.setText(self.tr("{0}: {1}").format(label, value) if label else str(value))
            pill.show()

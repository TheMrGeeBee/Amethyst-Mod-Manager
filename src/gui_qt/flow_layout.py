"""FlowLayout — a QLayout that arranges its items left-to-right and wraps to a
new row when the next item would overflow the available width.

Used for the footer/toolbar button rows so that longer translated labels (German
and French run 30-50% wider than English) reflow onto a second line instead of
clipping or pushing the row past the panel edge. When everything fits on one
line it looks identical to a plain QHBoxLayout.

Port of Qt's canonical FlowLayout example (height-for-width), adapted to honour
each item's own margins and a configurable spacing.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QMargins, QPoint, QRect, QSize
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None,
                 margin: int = 0, spacing: int = 4) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        if parent is not None:
            self.setContentsMargins(margin, margin, margin, margin)

    # ---- QLayout plumbing -------------------------------------------------
    def addItem(self, item: QLayoutItem) -> None:      # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:   # noqa: N802
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:   # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:     # noqa: N802
        return Qt.Orientation(0)

    def setSpacing(self, spacing: int) -> None:           # noqa: N802
        self._spacing = spacing

    def spacing(self) -> int:
        return self._spacing

    # ---- height-for-width -------------------------------------------------
    def hasHeightForWidth(self) -> bool:                  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:          # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:           # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:                          # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:                       # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m: QMargins = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    # ---- core reflow ------------------------------------------------------
    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m: QMargins = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            w = item.sizeHint()
            next_x = x + w.width() + self._spacing
            if next_x - self._spacing > effective.right() and line_height > 0:
                # Would overflow the row → wrap to the next line.
                x = effective.x()
                y = y + line_height + self._spacing
                next_x = x + w.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), w))
            x = next_x
            line_height = max(line_height, w.height())

        return y + line_height - rect.y() + m.bottom()

"""ImageView — a simple full-size image viewer that opens as a tab (lightbox).

Used by the FOMOD wizard when an option image is clicked. Shows the image scaled
to fit the tab, in a scroll area; clicking toggles between fit-to-window and 100%.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QScrollArea, QFrame


class ImageView(QWidget):
    def __init__(self, image_path: Path, parent=None):
        super().__init__(parent)
        self._pm = QPixmap(str(image_path))
        self._actual = False

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setAlignment(Qt.AlignCenter)
        self._label = QLabel()
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setCursor(Qt.PointingHandCursor)
        self._label.setToolTip(self.tr("Click to toggle 100% / fit"))
        self._label.mousePressEvent = lambda _e: self._toggle()
        self._scroll.setWidget(self._label)
        v.addWidget(self._scroll)
        self._render()

    def _render(self):
        if self._pm.isNull():
            self._label.setText(self.tr("Image could not be loaded"))
            return
        if self._actual:
            self._scroll.setWidgetResizable(False)
            self._label.setPixmap(self._pm)
            self._label.resize(self._pm.size())
        else:
            self._scroll.setWidgetResizable(True)
            area = self._scroll.viewport().size()
            self._label.setPixmap(self._pm.scaled(
                area.width(), area.height(),
                Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _toggle(self):
        self._actual = not self._actual
        self._render()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._actual:
            self._render()

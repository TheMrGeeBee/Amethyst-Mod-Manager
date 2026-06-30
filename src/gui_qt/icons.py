"""Qt icon loading — reuses the existing PNG assets in src/icons/.

The Tk app loads these via gui.theme.load_icon (→ CTkImage). The Qt app loads
the same files into QIcon. Icons are cached by (name, size).
"""

from __future__ import annotations

from pathlib import Path
from PySide6.QtGui import QIcon, QPixmap, QTransform, QPainter, QColor
from PySide6.QtCore import QSize, Qt

# src/icons/ — same dir the Tk app uses (gui/ is a sibling of icons/).
_ICONS_DIR = Path(__file__).resolve().parent.parent / "icons"

_cache: dict[tuple[str, int], QIcon] = {}


def icon(name: str, size: int = 18) -> QIcon:
    """Return a QIcon for icons/<name> scaled to *size* px (square).

    Missing files yield an empty QIcon (button shows text only).
    """
    key = (name, size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    path = _ICONS_DIR / name
    if not path.is_file():
        ic = QIcon()
    else:
        pm = QPixmap(str(path))
        if not pm.isNull():
            pm = pm.scaled(QSize(size, size), Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
        ic = QIcon(pm)
    _cache[key] = ic
    return ic


def icon_rotated(name: str, degrees: int, size: int = 18,
                 color: str | None = None) -> QIcon:
    """Return a QIcon for icons/<name> rotated *degrees* clockwise, scaled to
    *size* px, optionally recoloured to *color* (tints the opaque pixels while
    keeping the alpha shape). Used e.g. for up/down chevrons from arrow.png."""
    key = (f"{name}@{degrees}#{color or ''}", size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    path = _ICONS_DIR / name
    if not path.is_file():
        ic = QIcon()
    else:
        pm = QPixmap(str(path))
        if not pm.isNull():
            if degrees:
                pm = pm.transformed(QTransform().rotate(degrees),
                                    Qt.SmoothTransformation)
            pm = pm.scaled(QSize(size, size), Qt.KeepAspectRatio,
                           Qt.SmoothTransformation)
            if color:
                tinted = QPixmap(pm.size())
                tinted.fill(Qt.transparent)
                p = QPainter(tinted)
                p.drawPixmap(0, 0, pm)          # original (for its alpha shape)
                p.setCompositionMode(QPainter.CompositionMode_SourceIn)
                p.fillRect(tinted.rect(), QColor(color))
                p.end()
                pm = tinted
        ic = QIcon(pm)
    _cache[key] = ic
    return ic

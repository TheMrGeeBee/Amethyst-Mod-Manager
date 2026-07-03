"""SelectorButton — a dropdown that shows the current selection and exposes a
list of choices plus pinned action items at the bottom of the menu.

Used to consolidate the top bar's game and profile controls: one button each,
instead of a +/⚙/combo cluster. Selecting a list item changes the current
choice (fires on_select); the bottom action items fire their own callbacks and
never become the selection.
"""

from __future__ import annotations

from typing import Callable
from PySide6.QtWidgets import QToolButton, QMenu
from PySide6.QtGui import QActionGroup
from PySide6.QtCore import Qt, QSize


class SelectorButton(QToolButton):
    def __init__(self, *, items=None, current=None, actions=None,
                 on_select: "Callable[[str], None] | None" = None,
                 prefix="", min_width=170, icon=None, icon_px=18,
                 item_icons=None, parent=None):
        """*items*   — list of selectable labels.
        *current*   — initially selected label (defaults to items[0]).
        *actions*   — list of (label, callback) pinned below a separator.
        *on_select* — called with the chosen label when a list item is picked.
        *prefix*    — text shown before the current label on the button itself
                      (e.g. "Profile: "); not part of the selectable values.
        *icon*      — a QIcon to show INSTEAD of the current-label text (the
                      button becomes an icon button; the menu is unchanged).
        *item_icons* — {label: QIcon} shown next to each menu entry, and on the
                      button face beside the current label (text stays). Only
                      used in text mode (ignored when *icon* replaces the text).
        """
        super().__init__(parent)
        self._items: list[str] = list(items or [])
        self._actions = list(actions or [])
        self._on_select = on_select
        self._prefix = prefix
        self._icon = icon
        self._item_icons: dict = dict(item_icons or {})
        self._item_icon_px = icon_px
        self._current = current or (self._items[0] if self._items else "")
        self._highlighted: str | None = None   # green "active/deployed" item
        self.setObjectName("ActionButton")   # share the flat toolbar styling
        self.setProperty("split", True)       # gets the arrow-room padding
        # Split button: a text section + a separate arrow section on the right
        # (the whole thing opens the menu, like the mockup's Proton dropdown).
        self.setPopupMode(QToolButton.MenuButtonPopup)
        self.setCursor(Qt.PointingHandCursor)
        if icon is not None:
            self.setIcon(icon)
            self.setIconSize(QSize(icon_px, icon_px))
            self.setToolButtonStyle(Qt.ToolButtonIconOnly)
            # Icon-only selector (e.g. the play-bar gear): no split arrow
            # section — the split QSS padding (28px arrow room) would leave no
            # space for the glyph in a square button. InstantPopup keeps the
            # whole face clickable; hide the default menu-indicator overlay.
            self.setProperty("split", False)
            self.setPopupMode(QToolButton.InstantPopup)
            self.setStyleSheet(
                "QToolButton { padding: 0; }"
                "QToolButton::menu-indicator { image: none; }")
        else:
            self.setToolButtonStyle(Qt.ToolButtonTextOnly)
            self.setMinimumWidth(min_width)
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        # The text section (left of the split) also opens the menu — a selector
        # has no separate primary action. Open on *press* (like the arrow
        # section does natively): waiting for the release made the body
        # highlight first and the arrow section catch up later, which read as
        # lag; on press, aboutToShow sets menuOpen before the next repaint so
        # both halves light up together with the menu. (InstantPopup — the
        # icon-only mode — already opens on press natively.)
        if icon is None:
            self.pressed.connect(self.showMenu)
        # A dynamic `menuOpen` property (toggled while the menu is shown) drives
        # the open-state highlight in QSS — reliable across QStyles, unlike the
        # :pressed/:on pseudo-states for a MenuButtonPopup tool button.
        self.setProperty("menuOpen", False)
        self._menu.aboutToShow.connect(lambda: self._set_menu_open(True))
        self._menu.aboutToHide.connect(lambda: self._set_menu_open(False))
        self._group: QActionGroup | None = None
        self._rebuild()

    def _set_menu_open(self, on: bool):
        self.setProperty("menuOpen", on)
        # Re-evaluate the stylesheet against the new property value.
        self.style().unpolish(self)
        self.style().polish(self)

    # -- public API ---------------------------------------------------------
    def set_items(self, items, current=None, item_icons=None):
        self._items = list(items)
        if item_icons is not None:
            self._item_icons = dict(item_icons)
        if current is not None:
            self._current = current
        elif self._current not in self._items and self._items:
            self._current = self._items[0]
        self._rebuild()

    def set_item_icons(self, item_icons: dict):
        """Replace the {label: QIcon} map without touching the item list."""
        self._item_icons = dict(item_icons or {})
        self._rebuild()

    def current(self) -> str:
        return self._current

    def set_current(self, label: str):
        if label in self._items:
            self._current = label
            self._rebuild()

    def set_highlighted_item(self, label: str | None):
        """Mark one item as 'active' — its menu entry is coloured green and, when
        it's also the current selection, the button itself goes green. Used to
        show the deployed profile (or active game). None clears it."""
        if getattr(self, "_highlighted", None) != label:
            self._highlighted = label
            self._rebuild()

    # -- internals ----------------------------------------------------------
    def _rebuild(self):
        if self._icon is None:
            label = self._current or "—"
            # No trailing glyph — the split-button's arrow section shows it now.
            self.setText(f"{self._prefix}{label}")
            # The current item's icon is drawn ourselves in paintEvent (to the
            # left of the still-centred text). Keep the QToolButton in text-only
            # mode so Qt centres the label; using its built-in icon slot would
            # left-align the icon+text group instead.
            self.setToolButtonStyle(Qt.ToolButtonTextOnly)
        # Tint the button green (via the `deployed` property) when the current
        # selection IS the highlighted/active item; QSS reads the property.
        self.setProperty("deployed", self._highlighted is not None
                         and self._current == self._highlighted)
        self.style().unpolish(self); self.style().polish(self)
        # Enlarge the menu-entry icons to match the bigger face icon (Qt draws
        # action icons at PM_SmallIconSize = 16px otherwise, which reads tiny).
        self._menu.setStyleSheet(
            f"QMenu {{ icon-size: {self._item_icon_px}px; }}"
            if self._item_icons else "")
        self._menu.clear()
        # Exclusive action group → the selectable items render as radio buttons.
        self._group = QActionGroup(self._menu)
        self._group.setExclusive(True)
        for label in self._items:
            a = self._menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(label == self._current)
            item_icon = self._item_icons.get(label)
            if item_icon is not None:
                a.setIcon(item_icon)
            if self._highlighted is not None and label == self._highlighted:
                # QAction can't set foreground colour; mark the deployed item
                # with a green check + bold so it reads as active in the list.
                a.setText(f"{label}   ✓ deployed")
                f = a.font(); f.setBold(True); a.setFont(f)
            self._group.addAction(a)
            a.triggered.connect(lambda _=False, l=label: self._choose(l))
        if self._items and self._actions:
            self._menu.addSeparator()
        self._add_actions(self._menu, self._actions)

    def _add_actions(self, menu, actions):
        """Append pinned action entries to *menu*. Each entry is (label, cb)
        where cb is either a callable or a list of (label, cb) pairs (→ a
        submenu, nested arbitrarily deep)."""
        for label, cb in actions:
            if isinstance(cb, list):
                sub = menu.addMenu(label)
                self._add_actions(sub, cb)
            elif cb is not None:
                a = menu.addAction(label)
                a.triggered.connect(lambda _=False, c=cb: c())
            else:
                menu.addAction(label)

    def paintEvent(self, event):
        # Let the base class paint the (centred) text + chrome, then draw the
        # current item's icon ourselves pinned to the LEFT edge of the button —
        # the label stays centred (QToolButton's own icon slot would left-align
        # the icon+text group together instead).
        super().paintEvent(event)
        if self._icon is not None:
            return
        face = self._item_icons.get(self._current)
        if face is None:
            return
        from PySide6.QtGui import QPainter
        from PySide6.QtCore import QRect
        px = self._item_icon_px
        x = 8   # left-edge inset
        y = (self.height() - px) // 2
        p = QPainter(self)
        face.paint(p, QRect(x, y, px, px))
        p.end()

    def _choose(self, label):
        if label != self._current:
            self._current = label
            self._rebuild()
            if self._on_select:
                self._on_select(label)

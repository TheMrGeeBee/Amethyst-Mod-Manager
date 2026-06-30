"""Plugin-tab view + delegate (Plugins tab, v1).

A QTreeView over PluginModel with a delegate that paints: enable checkbox, name
(dimmed when disabled), the ESL 'L' cyan badge + master indicator in the Flags
column, the lock column, and the load-order index. Single-click the checkbox to
toggle (persists to plugins.txt).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRect, QSize, QEvent, QTimer
from PySide6.QtGui import QColor, QFont, QPen, QBrush, QPainter
from PySide6.QtWidgets import (
    QTreeView, QStyledItemDelegate, QStyle, QAbstractItemView, QHeaderView,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.icons import icon
from gui_qt.modlist_header import TkStyleHeader
from gui_qt.plugin_model import (
    PluginModel, RowRole, PFlagsRole, PHighlightRole,
    COL_NAME, COL_FLAGS, COL_LOCK, COL_INDEX,
)
from gui_qt.plugin_state import (
    PF_MISSING, PF_LATE, PF_VMM, PF_ESL, PF_LOOT, PF_DIRTY, PF_TAGS, PF_MASTER,
)

# Flag bit → icon filename, painted left→right (order matches the Tk app).
# ESL is drawn as a cyan "L" text badge (handled specially), not an icon.
_PLUGIN_FLAG_ICONS = [
    (PF_MISSING, "warning2.png"),
    (PF_LATE, "warning.png"),
    (PF_VMM, "info.png"),
    (PF_LOOT, "Loot_info.png"),
    (PF_DIRTY, "brush.png"),
    (PF_TAGS, "tag.png"),
]

ROW_H = 33
CHECK_BOX = 17
FONT_PX = 14
LOCK_SZ = 17

# Per-column min/default widths; Plugin Name (col 0) auto-fills like the modlist.
COL_DEFAULTS = {COL_FLAGS: 80, COL_LOCK: 40, COL_INDEX: 60}
COL_MINS = {COL_NAME: 120, COL_FLAGS: 60, COL_LOCK: 36, COL_INDEX: 50}
NAME_MIN = COL_MINS[COL_NAME]


class PluginDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        p = active_palette()
        self.c_row = QColor(_c(p, "BG_ROW"))
        self.c_row_alt = QColor(_c(p, "BG_ROW_ALT"))
        self.c_sel = QColor(_c(p, "BG_SELECT"))
        self.c_hover = QColor(_c(p, "BG_ROW_HOVER"))
        self.c_text = QColor(_c(p, "TEXT_MAIN"))
        self.c_text_dim = QColor(_c(p, "TEXT_DIM"))
        self.c_text_on_sel = QColor(_c(p, "TEXT_ON_ACCENT"))
        self.c_border = QColor(_c(p, "BORDER"))
        self.c_check = QColor(_c(p, "ACCENT"))   # checkbox fill when enabled (blue)
        self.c_check_off = QColor(_c(p, "BG_DEEP"))
        self.c_esl = QColor(_c(p, "TONE_BLUE_SOFT"))
        self.c_master = QColor(_c(p, "TEXT_WARN"))
        # Cross-panel highlight tints (exact Tk conflict colours).
        self.c_hl_higher = QColor("#108d00")
        self.c_hl_lower = QColor("#9a0e0e")
        self.c_hl_anchor = QColor("#A45500")

    def sizeHint(self, opt, index):
        return QSize(opt.rect.width(), ROW_H)

    def paint(self, p, opt, index):
        r = opt.rect
        row = index.data(RowRole)
        p.save()
        p.setRenderHint(p.RenderHint.Antialiasing, False)

        selected = bool(opt.state & QStyle.State_Selected)
        hl = index.data(PHighlightRole) or 0
        highlighted = False
        if selected:
            p.fillRect(r, self.c_sel)
        elif hl == 2:
            p.fillRect(r, self.c_hl_anchor); highlighted = True
        elif hl == 1:
            p.fillRect(r, self.c_hl_higher); highlighted = True
        elif hl == -1:
            p.fillRect(r, self.c_hl_lower); highlighted = True
        elif opt.state & QStyle.State_MouseOver:
            p.fillRect(r, self.c_hover)
        else:
            p.fillRect(r, self.c_row_alt if index.row() % 2 else self.c_row)

        enabled = bool(row and row.enabled)
        vanilla = bool(row and row.vanilla)
        # Vanilla plugins are greyed (dim) regardless of enabled state.
        text_color = self.c_text_on_sel if (selected or highlighted) else (
            self.c_text_dim if (vanilla or not enabled) else self.c_text)
        col = index.column()

        if col == COL_NAME:
            self._paint_name(p, r, row, enabled, vanilla, text_color)
        elif col == COL_FLAGS:
            self._paint_flags(p, r, index.data(PFlagsRole) or 0)
        elif col == COL_LOCK:
            self._paint_lock(p, r, index.model().is_locked(index.row()))
        elif col == COL_INDEX:
            p.setPen(text_color)
            _f = QFont(); _f.setPixelSize(FONT_PX); p.setFont(_f)
            p.drawText(r, Qt.AlignVCenter | Qt.AlignHCenter,
                       index.data(Qt.DisplayRole) or "")
        p.restore()

    def _lock_rect(self, r):
        return QRect(r.center().x() - LOCK_SZ // 2,
                     r.top() + (r.height() - LOCK_SZ) // 2, LOCK_SZ, LOCK_SZ)

    def _paint_lock(self, p, r, locked):
        lk = self._lock_rect(r)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(QPen(self.c_border, 1))
        p.setBrush(QBrush(self.c_check_off))
        p.drawRoundedRect(lk, 3, 3)
        if locked:
            ic = icon("lock.png", LOCK_SZ - 2)
            if not ic.isNull():
                ic.paint(p, lk.adjusted(1, 1, -1, -1))
        p.setRenderHint(p.RenderHint.Antialiasing, False)

    def _paint_name(self, p, r, row, enabled, vanilla, text_color):
        box = QRect(r.left() + 10, r.top() + (r.height() - CHECK_BOX) // 2,
                    CHECK_BOX, CHECK_BOX)
        p.setRenderHint(p.RenderHint.Antialiasing, True)
        p.setPen(QPen(self.c_border, 1))
        # Vanilla: always-on but dimmed (greyed fill + grey tick) to read as
        # locked/non-interactive. Otherwise green when enabled, hollow when not.
        fill = (self.c_check_off if vanilla else
                (self.c_check if enabled else self.c_check_off))
        p.setBrush(QBrush(fill))
        p.drawRoundedRect(box, 3, 3)
        if enabled:
            p.setPen(QPen(self.c_text_dim if vanilla else QColor("white"), 2))
            p.drawLine(box.left() + 4, box.center().y() + 1,
                       box.center().x() - 1, box.bottom() - 4)
            p.drawLine(box.center().x() - 1, box.bottom() - 4,
                       box.right() - 3, box.top() + 4)
        p.setRenderHint(p.RenderHint.Antialiasing, False)

        tx = box.right() + 10
        p.setPen(text_color)
        _f = QFont(); _f.setPixelSize(FONT_PX); p.setFont(_f)
        name_rect = QRect(tx, r.top(), r.right() - tx - 6, r.height())
        elided = p.fontMetrics().elidedText(row.name if row else "",
                                            Qt.ElideRight, name_rect.width())
        p.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, elided)

    def _paint_flags(self, p, r, bits):
        # Ordered flag glyphs: the icon flags, then the ESL 'L' badge. (There is
        # no master indicator — Tk doesn't show one; masters are implied by ext.)
        items = []
        for bit, name in _PLUGIN_FLAG_ICONS:
            if bits & bit:
                items.append(("icon", name))
        if bits & PF_ESL:
            items.append(("esl", None))
        if not items:
            return
        sz = 18
        total = len(items) * sz + (len(items) - 1) * 4
        x = r.left() + max(4, (r.width() - total) // 2)
        cy = r.center().y()
        for kind, name in items:
            cell = QRect(x, cy - sz // 2, sz, sz)
            if kind == "esl":
                f = QFont(); f.setBold(True); f.setPixelSize(13); p.setFont(f)
                p.setPen(self.c_esl)
                p.drawText(cell, Qt.AlignCenter, "L")
            else:
                ic = icon(name, sz)
                if not ic.isNull():
                    ic.paint(p, cell)
            x += sz + 4

    def editorEvent(self, event, model, opt, index):
        if event.type() != QEvent.MouseButtonRelease:
            return False
        pos = event.position().toPoint()
        if index.column() == COL_NAME:
            box = QRect(opt.rect.left() + 6, opt.rect.top(), 26, opt.rect.height())
            if box.contains(pos):
                model.toggle(index.row())
                return True
        elif index.column() == COL_LOCK:
            if self._lock_rect(opt.rect).contains(pos):
                model.toggle_lock(index.row())
                return True
        return False


class PluginView(QTreeView):
    def __init__(self, model: PluginModel, parent=None):
        super().__init__(parent)
        self.setModel(model)
        self.setItemDelegate(PluginDelegate(self))
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(False)
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        self._plugin_owner: dict = {}
        self._search_hidden: set[int] = set()
        # Custom drag-reorder (vanilla pinned at top, locked rows immovable).
        self._drag_rows: list[int] = []
        self._drag_active = False
        self._press_row = -1
        self._press_pos = None
        self._drop_slot = -1
        self._DRAG_THRESHOLD = 6
        self._scroll_zone = 40
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(16)
        self._scroll_timer.timeout.connect(self._autoscroll_tick)
        self._last_mouse_y = 0

        # Same Tk-style column resize as the modlist (boundary drag, fill-width,
        # no overflow). Plugin Name (col 0) is the fill column.
        h = TkStyleHeader(self, COL_MINS, COL_DEFAULTS)
        self.setHeader(h)
        h.setMinimumSectionSize(min(COL_MINS.values()))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for col, w in COL_DEFAULTS.items():
            self.setColumnWidth(col, w)

        # Marker strip (coloured-tick gutter beside the scrollbar) — same as the
        # modlist, driven by PHighlightRole.
        from gui_qt.marker_strip import install_marker_strip
        install_marker_strip(self, PHighlightRole)
        self._reposition_marker_strip()

    def _reposition_marker_strip(self):
        from gui_qt.marker_strip import reposition_marker_strip
        reposition_marker_strip(self)

    # ---- search filtering ------------------------------------------------
    def set_search_hidden(self, rows: set[int]) -> None:
        """Hide the given rows (search box). Empty set shows everything."""
        self._search_hidden = set(rows or ())
        for r in range(self.model().rowCount()):
            self.setRowHidden(r, self.rootIndex(), r in self._search_hidden)
        sb = self.verticalScrollBar()
        if sb is not None:
            sb.update()

    # ---- cross-panel highlights ------------------------------------------
    def set_plugin_owner(self, owner: dict):
        """owner maps plugin filename (lower) → owning mod name."""
        self._plugin_owner = dict(owner or {})

    def selected_owner_mods(self, owner: dict) -> set:
        """The mods that own the currently-selected plugins."""
        m = self.model()
        mods: set = set()
        for idx in self.selectionModel().selectedRows():
            r = m.row(idx.row())
            mod = (owner or {}).get(r.name.lower())
            if mod:
                mods.add(mod)
        return mods

    def set_highlight_from_mods(self, mod_names: set, bsa_higher: set,
                                bsa_lower: set, owner: dict,
                                bsa_index_path=None):
        """Highlight plugins from a modlist selection (Tk parity):
          - orange (anchor): plugins of the selected mod(s) — unconditional.
          - green/red: plugins of mods in a *BSA* conflict with the selection,
            and ONLY plugins that actually own a BSA. Loose-file conflicts do
            NOT colour plugins (a standalone plugin loads no archive contents).
        owner maps plugin filename(lower) → mod name."""
        # Invert owner → mod → [plugin names(lower)].
        mod_to_plugins: dict[str, list[str]] = {}
        for plugin, mod in (owner or {}).items():
            mod_to_plugins.setdefault(mod, []).append(plugin)

        bsa_filter = self._bsa_owning_plugins(
            (bsa_higher or set()) | (bsa_lower or set()),
            mod_to_plugins, bsa_index_path)

        hl: dict[str, int] = {}
        for mod in (bsa_lower or set()):
            for pl in mod_to_plugins.get(mod, []):
                if pl in bsa_filter:
                    hl[pl] = -1
        for mod in (bsa_higher or set()):
            for pl in mod_to_plugins.get(mod, []):
                if pl in bsa_filter:
                    hl[pl] = 1
        for mod in (mod_names or set()):
            for pl in mod_to_plugins.get(mod, []):
                hl[pl] = 2   # anchor wins over conflict tint
        self.model().set_highlights(hl)
        self.viewport().update()

    def _bsa_owning_plugins(self, mods: set, mod_to_plugins: dict,
                            bsa_index_path) -> set:
        """{plugin filename(lower)} for plugins in *mods* that own a BSA via
        basename match — reuses the backend's _bsa_owning_plugin (Tk parity)."""
        if not mods or bsa_index_path is None:
            return set()
        try:
            from Utils.bsa_filemap import read_bsa_index, _bsa_owning_plugin
        except Exception:
            return set()
        idx = read_bsa_index(bsa_index_path) or {}
        result: set = set()
        for mod in mods:
            archives = idx.get(mod)
            if not archives:
                continue
            plugins = mod_to_plugins.get(mod, [])
            stems = {p.rsplit(".", 1)[0].lower(): p for p in plugins}
            if not stems:
                continue
            for bsa_name, _mt, _paths in archives:
                bsa_stem = bsa_name.rsplit(".", 1)[0]
                owning = _bsa_owning_plugin(bsa_stem, set(stems.keys()))
                if owning is not None and owning in stems:
                    result.add(stems[owning])
        return result

    # ---- custom drag-reorder ---------------------------------------------
    def _drag_block_for(self, row: int) -> list[int] | None:
        m = self.model()
        if not m.is_movable(row):
            return None
        sel = sorted({i.row() for i in self.selectionModel().selectedRows()})
        if row in sel and len(sel) > 1:
            carry = [r for r in sel if m.is_movable(r)]
            # Contiguous only (model.move_rows requires it).
            if carry and carry[-1] - carry[0] == len(carry) - 1:
                return carry
        return [row]

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            idx = self.indexAt(event.position().toPoint())
            self._press_row = idx.row() if idx.isValid() else -1
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.LeftButton) or self._press_row < 0:
            super().mouseMoveEvent(event)
            return
        if not self._drag_active:
            if self._press_pos is None or (
                    event.position().toPoint() - self._press_pos
            ).manhattanLength() < self._DRAG_THRESHOLD:
                return
            block = self._drag_block_for(self._press_row)
            if block is None:
                self._press_row = -1
                return
            self._drag_active = True
            self._drag_rows = block
            self.setCursor(Qt.ClosedHandCursor)
        self._last_mouse_y = event.position().toPoint().y()
        self._update_drop_slot(self._last_mouse_y)
        if not self._scroll_timer.isActive():
            self._scroll_timer.start()
        self.viewport().update()

    def mouseReleaseEvent(self, event):
        if self._drag_active:
            self._scroll_timer.stop()
            if self._drag_rows and self._drop_slot >= 0:
                self.model().move_rows(self._drag_rows, self._drop_slot)
            self._drag_active = False
            self._drag_rows = []
            self._drop_slot = -1
            self.unsetCursor()
            self.viewport().update()
            self._press_row = -1
            return
        self._press_row = -1
        super().mouseReleaseEvent(event)

    def _update_drop_slot(self, y: int):
        m = self.model()
        n = m.rowCount()
        for r in range(n):
            rect = self.visualRect(m.index(r, 0))
            if rect.top() <= y < rect.bottom():
                self._drop_slot = r if y < rect.center().y() else r + 1
                return
        first = self.visualRect(m.index(0, 0)) if n else None
        if first is not None and y < first.top():
            self._drop_slot = 0
        else:
            self._drop_slot = n
        self._drop_slot = max(0, min(self._drop_slot, n))

    def _autoscroll_tick(self):
        if not self._drag_active:
            self._scroll_timer.stop()
            return
        h = self.viewport().height()
        y = self._last_mouse_y
        zone = self._scroll_zone
        bar = self.verticalScrollBar()
        step = 0
        if y < zone:
            step = -int(2 + (zone - y) / zone * 22)
        elif y > h - zone:
            step = int(2 + (y - (h - zone)) / zone * 22)
        if step:
            bar.setValue(bar.value() + step)
            self._update_drop_slot(y)
            self.viewport().update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._drag_active or self._drop_slot < 0:
            return
        m = self.model()
        n = m.rowCount()
        if self._drop_slot >= n:
            y = self.visualRect(m.index(n - 1, 0)).bottom()
        else:
            y = self.visualRect(m.index(self._drop_slot, 0)).top()
        p = QPainter(self.viewport())
        pen = QPen(QColor("#5aa9ff")); pen.setWidth(2); p.setPen(pen)
        p.drawLine(0, y, self.viewport().width(), y)
        p.end()

    def showEvent(self, event):
        super().showEvent(event)
        self._fit_name_to_width()
        self._reposition_marker_strip()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_name_to_width()
        if hasattr(self, "_marker_strip"):
            self._reposition_marker_strip()

    def _fit_name_to_width(self):
        vp = self.viewport().width()
        if vp <= 0:
            return
        from gui_qt.plugin_model import COLUMNS
        others = sum(self.columnWidth(c) for c in range(len(COLUMNS))
                     if c != COL_NAME and not self.isColumnHidden(c))
        target = vp - others
        h = self.header()
        if target >= NAME_MIN:
            if target != self.columnWidth(COL_NAME):
                h.resizeSection(COL_NAME, target)
            return
        h.resizeSection(COL_NAME, NAME_MIN)
        deficit = (NAME_MIN + others) - vp
        for c in reversed([c for c in range(len(COLUMNS))
                           if c != COL_NAME and not self.isColumnHidden(c)]):
            if deficit <= 0:
                break
            room = self.columnWidth(c) - COL_MINS.get(c, 40)
            if room <= 0:
                continue
            take = min(room, deficit)
            h.resizeSection(c, self.columnWidth(c) - take)
            deficit -= take

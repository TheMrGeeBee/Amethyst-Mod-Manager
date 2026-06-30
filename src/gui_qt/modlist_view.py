"""Modlist view — QTreeView + ModListModel + ModRowDelegate.

Internal-move drag-reorder (model.beginMoveRows preserves selection/scroll);
TkStyleHeader owns column resizing; column state persists via column_state.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QRect
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import QTreeView, QAbstractItemView, QHeaderView

from gui_qt.modlist_model import (
    ModListModel, COLUMNS, COL_NAME, COL_PRIORITY, COL_FLAGS, COL_CONFLICTS,
    COL_INSTALLED, COL_VERSION, HighlightRole,
)
from PySide6.QtWidgets import QWidget
from gui_qt.modlist_delegate import ModRowDelegate
from gui_qt import column_state
from gui_qt.modlist_header import TkStyleHeader

# Per-column default width + minimum (design px), mirroring the Tk app's
# _layout_columns data_defaults / data_mins. Name auto-fills the leftover.
COL_DEFAULTS = {
    COL_FLAGS: 70, COL_CONFLICTS: 95, COL_INSTALLED: 100,
    COL_VERSION: 90, COL_PRIORITY: 75,
}
COL_MINS = {
    COL_NAME: 120, COL_FLAGS: 60, COL_CONFLICTS: 90, COL_INSTALLED: 90,
    COL_VERSION: 80, COL_PRIORITY: 70,
}
NAME_MIN = COL_MINS[COL_NAME]


class ModListView(QTreeView):
    def __init__(self, model: ModListModel, parent=None):
        super().__init__(parent)
        self.setModel(model)
        self.setItemDelegate(ModRowDelegate(self))

        self.setRootIsDecorated(False)        # flat list, not a tree
        self.setUniformRowHeights(False)      # separators are taller
        self.setAlternatingRowColors(False)   # delegate paints zebra itself
        self.setMouseTracking(True)
        self.setExpandsOnDoubleClick(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        # Custom drag-reorder (NOT Qt InternalMove): we drive the reorder by
        # hand so separators (spanned rows) drag correctly and autoscroll near
        # the edges is fast/continuous like the Tk app. See _press/_move/_release.
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.setDragEnabled(False)
        self.setAcceptDrops(False)

        # Drag state.
        self._drag_rows: list[int] = []   # source rows being carried (block)
        self._drag_active = False
        self._press_row = -1
        self._press_pos = None
        self._drop_slot = -1              # insertion row for the drop indicator
        self._DRAG_THRESHOLD = 6          # px before a press becomes a drag

        # Continuous autoscroll while dragging near an edge (Tk cadence).
        self._scroll_zone = 40            # px from edge that triggers scroll
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(16)   # ~60fps, smooth + fast
        self._scroll_timer.timeout.connect(self._autoscroll_tick)
        self._last_mouse_y = 0

        # Right-click context menu.
        self.staging_dir = None   # set by the window for Open-folder
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

        # Separator state persistence (profile dir set by the window on reload).
        self.profile_dir = None
        # Rows hidden by the filter side panel / search box (unioned with the
        # collapse hiding). _searching = a non-empty query is active (collapse
        # is then bypassed so matches inside collapsed separators show).
        self._filter_hidden: set[int] = set()
        self._search_hidden: set[int] = set()
        self._searching: bool = False
        self.doubleClicked.connect(self._on_double_click)

        self._restoring = True
        self._configure_header()
        self._restore_column_state()
        self._restoring = False

        # Persist on user changes (debounced to coalesce drag-resize bursts).
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_column_state)
        h = self.header()
        # sectionResized is handled by _on_section_resized (which saves);
        # only moves + sort-indicator need a direct save hook here.
        h.sectionMoved.connect(lambda *a: self._schedule_save())
        h.sortIndicatorChanged.connect(lambda *a: self._schedule_save())

        # Marker strip: a coloured-tick gutter beside the scrollbar showing
        # highlighted/conflicting rows (Tk parity). Shared with the plugins panel.
        from gui_qt.marker_strip import install_marker_strip
        install_marker_strip(self, HighlightRole)
        self._reposition_marker_strip()

    def _reposition_marker_strip(self):
        from gui_qt.marker_strip import reposition_marker_strip
        reposition_marker_strip(self)

    def _configure_header(self):
        # Custom Tk-style header: owns all resizing (boundary drag moves the
        # line between two columns, total constant, no overflow). All sections
        # Fixed so Qt never auto-resizes.
        h = TkStyleHeader(self, COL_MINS, COL_DEFAULTS)
        self.setHeader(h)
        h.setMinimumSectionSize(min(COL_MINS.values()))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # NOTE: live sorting intentionally NOT enabled — the modlist is
        # priority-ordered and the Tk app's column-sort has special semantics.
        for col, w in COL_DEFAULTS.items():
            self.setColumnWidth(col, w)
        self._fitting = False
        h.sectionResized.connect(lambda *a: self._schedule_save())
        h.sectionMoved.connect(lambda *a: self._schedule_save())

    # ---- separator collapse/expand ---------------------------------------
    def load_separator_state(self):
        """Read collapsed/lock state for the active profile into the model and
        apply row hiding. Called by the window after a modlist reload."""
        collapsed, locks = set(), {}
        if self.profile_dir is not None:
            try:
                from Utils.profile_state import (
                    read_collapsed_seps, read_separator_locks)
                collapsed = read_collapsed_seps(self.profile_dir)
                locks = read_separator_locks(self.profile_dir)
            except Exception:
                pass
        self.model().set_separator_state(collapsed, locks)
        self._apply_separator_spanning()
        self.apply_collapse()

    def _apply_separator_spanning(self):
        """Separator rows span all columns so the band + centred name + the
        right-side lock box use the full row width."""
        m = self.model()
        for r in range(m.rowCount()):
            self.setFirstColumnSpanned(r, self.rootIndex(),
                                       m.entry(r).is_separator)

    def apply_collapse(self):
        """Hide rows under a collapsed separator, the filter panel, OR the search
        box. When a search is active it OVERRIDES collapse (Tk parity — a match
        inside a collapsed separator is still revealed); the filter still applies.
        """
        flt = self._filter_hidden
        srch = self._search_hidden
        if self._searching:
            # Search drives visibility; collapse is ignored so matches surface.
            for r in range(self.model().rowCount()):
                self.setRowHidden(r, self.rootIndex(),
                                  r in srch or r in flt)
            return
        hidden = self.model().hidden_rows()
        for r in range(self.model().rowCount()):
            self.setRowHidden(r, self.rootIndex(),
                              r in hidden or r in flt)

    def set_filter_hidden(self, rows: set[int]) -> None:
        """Set the rows the filter panel wants hidden, then reapply visibility.
        Empty set clears the filter. Repaints the marker strip too."""
        self._filter_hidden = set(rows or ())
        self.apply_collapse()
        sb = self.verticalScrollBar()
        if sb is not None:
            sb.update()

    def set_search_hidden(self, rows: set[int], *, active: bool = None) -> None:
        """Set the rows the search box wants hidden, then reapply visibility.
        `active` marks whether a query is in effect (defaults to: any rows
        hidden) so collapse is bypassed only while searching."""
        self._search_hidden = set(rows or ())
        self._searching = bool(self._search_hidden) if active is None else active
        self.apply_collapse()
        sb = self.verticalScrollBar()
        if sb is not None:
            sb.update()

    def _on_double_click(self, index):
        if index.isValid() and self.model().entry(index.row()).is_separator:
            self._toggle_collapse_row(index.row())

    def _toggle_collapse_row(self, row):
        self.model().toggle_collapse(row)
        self.apply_collapse()
        self._save_separator_state()
        self.viewport().update()

    def _toggle_lock_row(self, row):
        self.model().toggle_sep_lock(row)
        self._save_separator_state()
        self.viewport().update()

    def set_all_collapsed(self, collapsed: bool):
        """Collapse or expand every separator (Expand all / Collapse all)."""
        self.model().set_all_collapsed(collapsed)
        self.apply_collapse()
        self._save_separator_state()
        self.viewport().update()
        sb = self.verticalScrollBar()
        if sb is not None:
            sb.update()

    def _save_separator_state(self):
        if self.profile_dir is None:
            return
        try:
            from Utils.profile_state import (
                write_collapsed_seps, write_separator_locks)
            m = self.model()
            write_collapsed_seps(self.profile_dir, m._collapsed)
            write_separator_locks(self.profile_dir, m._sep_locks)
        except Exception as exc:
            print(f"[gui_qt] separator state save failed: {exc}", flush=True)

    # ---- fill width: Name absorbs leftover on window resize ---------------
    def showEvent(self, event):
        super().showEvent(event)
        self._fit_name_to_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_name_to_width()
        if hasattr(self, "_marker_strip"):
            self._reposition_marker_strip()

    def _fit_name_to_width(self):
        """Keep the table exactly filling the viewport on window resize.

        Growing window: Name absorbs the extra. Shrinking window: Name gives
        back down to its minimum, then the data columns cascade down to their
        own minimums (so columns never get cut off / overflow the panel)."""
        vp = self.viewport().width()
        if vp <= 0:
            return
        h = self.header()
        others = sum(self.columnWidth(c) for c in range(len(COLUMNS))
                     if c != COL_NAME and not self.isColumnHidden(c))
        target_name = vp - others

        if target_name >= NAME_MIN:
            if target_name != self.columnWidth(COL_NAME):
                h.resizeSection(COL_NAME, target_name)
            return

        # Not enough room even at Name's minimum: pin Name to min, then shrink
        # the data columns (right-to-left) toward their minimums to fit.
        h.resizeSection(COL_NAME, NAME_MIN)
        deficit = (NAME_MIN + others) - vp
        for c in reversed([c for c in range(len(COLUMNS))
                           if c != COL_NAME and not self.isColumnHidden(c)]):
            if deficit <= 0:
                break
            room = self.columnWidth(c) - COL_MINS.get(c, 60)
            if room <= 0:
                continue
            take = min(room, deficit)
            h.resizeSection(c, self.columnWidth(c) - take)
            deficit -= take

    # ---- cross-panel highlights ------------------------------------------
    def set_conflict_maps(self, overrides, overridden_by,
                          bsa_overrides, bsa_overridden_by):
        """Store the loose + BSA override maps so a mod selection can resolve
        which mods it beats (higher/green) and which beat it (lower/red)."""
        self._overrides = {k: set(v) for k, v in (overrides or {}).items()}
        self._overridden_by = {k: set(v) for k, v in (overridden_by or {}).items()}
        self._bsa_overrides = {k: set(v) for k, v in (bsa_overrides or {}).items()}
        self._bsa_overridden_by = {k: set(v)
                                   for k, v in (bsa_overridden_by or {}).items()}
        # Re-apply any active highlight against the fresh maps.
        self._refresh_self_highlights()

    def selected_mod_names(self) -> set[str]:
        """Names of the selected mods. A selected separator contributes all the
        mods in its block (Tk parity)."""
        m = self.model()
        names: set[str] = set()
        from gui_qt.modlist_model import _BOUNDARY_NAMES
        for idx in self.selectionModel().selectedRows():
            e = m.entry(idx.row())
            if e.is_separator:
                if e.name in _BOUNDARY_NAMES:
                    continue
                for r in m.sep_block_rows(idx.row()):
                    names.add(m.entry(r).name)
            else:
                names.add(e.name)
        return names

    def conflict_partners(self, names: set[str]) -> tuple[set[str], set[str]]:
        """For a set of mod names, return (higher, lower): the mods they beat
        (loose+BSA) and the mods that beat them, excluding the selection."""
        ov = getattr(self, "_overrides", {})
        ob = getattr(self, "_overridden_by", {})
        bov = getattr(self, "_bsa_overrides", {})
        bob = getattr(self, "_bsa_overridden_by", {})
        higher: set[str] = set()
        lower: set[str] = set()
        for n in names:
            higher |= ov.get(n, set()) | bov.get(n, set())
            lower |= ob.get(n, set()) | bob.get(n, set())
        higher -= names
        lower -= names
        return higher, lower

    def bsa_conflict_partners(self, names: set[str]) -> tuple[set[str], set[str]]:
        """Like conflict_partners but BSA-only — used to colour plugins (Tk only
        tints plugins for BSA conflicts, never loose-file ones)."""
        bov = getattr(self, "_bsa_overrides", {})
        bob = getattr(self, "_bsa_overridden_by", {})
        higher: set[str] = set()
        lower: set[str] = set()
        for n in names:
            higher |= bov.get(n, set())
            lower |= bob.get(n, set())
        higher -= names
        lower -= names
        return higher, lower

    def _refresh_self_highlights(self):
        """Recompute green/red tints from the current mod selection."""
        names = self.selected_mod_names()
        if not names:
            return
        higher, lower = self.conflict_partners(names)
        self.model().set_highlights(higher=higher, lower=lower)

    def set_highlighted_mods(self, mods: set[str] | None):
        """Highlight (orange) the mods owning a plugin selected in the Plugins
        panel; clears the green/red conflict tint."""
        self.model().set_highlights(anchor=mods or set())
        # Scroll the first highlighted mod into view (Tk parity).
        if mods:
            m = self.model()
            for r in range(m.rowCount()):
                if not m.entry(r).is_separator and m.entry(r).name in mods:
                    self.scrollTo(m.index(r, 0),
                                  QAbstractItemView.PositionAtCenter)
                    break

    # ---- custom drag-reorder ---------------------------------------------
    def _visible_rows(self) -> list[int]:
        """Rows currently visible (not hidden under a collapsed separator)."""
        m = self.model()
        return [r for r in range(m.rowCount())
                if not self.isRowHidden(r, self.rootIndex())]

    def _drag_block_for(self, row: int) -> list[int] | None:
        """The rows to carry when a drag starts on *row*, or None if *row* is
        un-draggable (boundary separator / locked mod). A locked separator
        carries its whole block; everything else carries the selected rows (or
        just itself)."""
        m = self.model()
        e = m.entry(row)
        from gui_qt.modlist_model import _BOUNDARY_NAMES
        if e.name in _BOUNDARY_NAMES:
            return None
        if not e.is_separator and e.locked:
            return None
        # A collapsed or locked separator carries its whole block (its mods are
        # hidden / pinned to it), so the group moves together. An expanded
        # separator moves alone — it just re-marks where a group begins.
        if e.is_separator and (m.is_sep_locked(e.display_name)
                               or m.is_collapsed(e.display_name)):
            return [row] + list(m.sep_block_rows(row))
        # Multi-select: carry every selected, draggable row if this row is part
        # of the selection; otherwise just this row.
        sel = sorted({i.row() for i in self.selectionModel().selectedRows()})
        if row in sel and len(sel) > 1:
            carry = [r for r in sel
                     if m.entry(r).name not in _BOUNDARY_NAMES
                     and not (not m.entry(r).is_separator and m.entry(r).locked)]
            return carry or [row]
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
            if self._press_pos is None:
                return
            if (event.position().toPoint() - self._press_pos).manhattanLength() \
                    < self._DRAG_THRESHOLD:
                return
            block = self._drag_block_for(self._press_row)
            if block is None:
                self._press_row = -1
                return
            self._drag_active = True
            self._drag_rows = block
            self.setCursor(Qt.ClosedHandCursor)
        # Live drag: track cursor, compute drop slot, run autoscroll.
        self._last_mouse_y = event.position().toPoint().y()
        self._update_drop_slot(self._last_mouse_y)
        if not self._scroll_timer.isActive():
            self._scroll_timer.start()
        self.viewport().update()

    def mouseReleaseEvent(self, event):
        if self._drag_active:
            self._scroll_timer.stop()
            self._commit_drop()
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
        """Compute the model row the block would insert *before*, from cursor Y.
        Snaps to the gap nearest the cursor among visible rows."""
        m = self.model()
        n = m.rowCount()
        vis = self._visible_rows()
        if not vis:
            self._drop_slot = 0
            return
        # Find the visible row under the cursor; drop before/after by half-row.
        for r in vis:
            rect = self.visualRect(m.index(r, 0))
            if rect.top() <= y < rect.bottom():
                self._drop_slot = r if y < rect.center().y() else r + 1
                return
        # Above the first / below the last visible row.
        first_rect = self.visualRect(m.index(vis[0], 0))
        if y < first_rect.top():
            self._drop_slot = vis[0]
        else:
            self._drop_slot = vis[-1] + 1
        self._drop_slot = max(0, min(self._drop_slot, n))

    def _commit_drop(self):
        if not self._drag_rows or self._drop_slot < 0:
            return
        dest = self._drop_slot
        src = self._drag_rows
        m = self.model()
        # A LONE separator drops exactly where released (like a mod) — it just
        # marks where a new group begins. Only a separator carrying a whole
        # block (locked sep) snaps to a block boundary so it stays self-contained.
        carrying_block = (len(src) > 1 and m.entry(src[0]).is_separator)
        if carrying_block:
            dest = m._resolve_drop_dest(dest, separator_drag=True)
        m.move_block(src, dest)
        # After a structural change, re-apply spanning + collapse hiding.
        self._apply_separator_spanning()
        self.apply_collapse()

    # ---- continuous autoscroll (Tk cadence: fast, proportional to depth) ---
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
            depth = (zone - y) / zone               # 0..1
            step = -int(2 + depth * 22)             # up to ~24 px/tick
        elif y > h - zone:
            depth = (y - (h - zone)) / zone
            step = int(2 + depth * 22)
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
            ref = self.visualRect(m.index(n - 1, 0))
            y = ref.bottom()
        else:
            ref = self.visualRect(m.index(self._drop_slot, 0))
            y = ref.top()
        p = QPainter(self.viewport())
        pen = QPen(QColor("#5aa9ff"))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(0, y, self.viewport().width(), y)
        p.end()

    # ---- context menu -----------------------------------------------------
    def _on_context_menu(self, pos):
        from gui_qt.modlist_menu import show_context_menu
        index = self.indexAt(pos)
        if index.isValid():
            show_context_menu(self, self.viewport().mapToGlobal(pos), index)

    # ---- column-state persistence (keyed by logical column name) ----------
    def _schedule_save(self):
        if not self._restoring:
            self._save_timer.start()

    def _save_column_state(self):
        h = self.header()
        widths = {COLUMNS[c]: self.columnWidth(c) for c in range(len(COLUMNS))}
        order = [COLUMNS[h.logicalIndex(v)] for v in range(len(COLUMNS))]
        hidden = {COLUMNS[c] for c in range(len(COLUMNS)) if self.isColumnHidden(c)}
        sc = h.sortIndicatorSection()
        sort_col = COLUMNS[sc] if 0 <= sc < len(COLUMNS) else None
        ascending = h.sortIndicatorOrder() == Qt.AscendingOrder
        column_state.save_state(widths, order, hidden, sort_col, ascending)

    def _restore_column_state(self):
        st = column_state.load_state()
        if not (st["widths"] or st["order"] or st["hidden"] or st["sort_col"]):
            return
        name_to_col = {n: i for i, n in enumerate(COLUMNS)}
        for name, w in st["widths"].items():
            if name in name_to_col and name != "Mod Name":  # name stays stretch
                self.setColumnWidth(name_to_col[name], w)
        for name in st["hidden"]:
            if name in name_to_col:
                self.setColumnHidden(name_to_col[name], True)
        h = self.header()
        for visual, name in enumerate(st["order"]):
            if name in name_to_col:
                cur = h.visualIndex(name_to_col[name])
                if cur != -1 and cur != visual:
                    h.moveSection(cur, visual)
        if st["sort_col"] in name_to_col:
            order = Qt.AscendingOrder if st["ascending"] else Qt.DescendingOrder
            # Set the indicator only (live sort not enabled yet).
            self.header().setSortIndicator(name_to_col[st["sort_col"]], order)


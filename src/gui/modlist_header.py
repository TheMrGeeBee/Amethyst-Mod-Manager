"""
Header column drag/resize/visibility mixin for ModListPanel.

Wraps three concerns that all sit on the header row:
- Column boundary drag → resize neighbours, persisted via save_column_widths.
- Column reorder drag (label drag-to-move) → persisted via save_column_order.
- Column visibility menu (⋮ button) → persisted via save_column_hidden.

Host must own the column geometry (_COL_X, _COL_W, _col_pos, _col_order,
_col_hidden, _col_w_override, _DATA_COL_TITLES, _canvas_w) and the header
widgets (_header, _header_labels, _col_menu_btn, _col_menu_popup), plus
provide _layout_columns, _update_header, _redraw, and _on_header_click.
"""

import tkinter as tk

from Utils.ui_config import (
    save_column_widths, save_column_order, save_column_hidden,
)
from gui.ctk_components import CTkPopupMenu
from gui.theme import (
    ACCENT,
    BG_HEADER, BG_SELECT_BAR,
    scaled,
)
import gui.theme as _theme


class ModListHeaderColumnsMixin:
    """Header column resize, reorder, and show/hide menu for ModListPanel."""

    # _COL_W slot indices: 0=checkbox, 1=name, 2=cat, 3=flags, 4=conflicts,
    # 5=installed, 6=priority, 7=version, 8=size.
    _COL_MIN_W = {1: 120, 2: 95, 3: 70, 4: 95, 5: 95, 6: 80, 7: 80, 8: 75}

    def _slot_to_data_col(self, slot: int) -> int:
        """Convert a _COL_W/X slot index to the data-col index it currently holds."""
        if slot == 1:
            return 1
        visible = [dc for dc in self._col_order if dc not in self._col_hidden]
        idx = slot - 2
        if 0 <= idx < len(visible):
            return visible[idx]
        return slot

    # ------------------------------------------------------------------
    # Column resize drag (bound to divider widgets)
    # ------------------------------------------------------------------

    def _on_divider_drag_start(self, event: tk.Event, col: int) -> None:
        """Start a column resize drag. col = _COL_W slot index of the left column."""
        self._col_drag_col = col
        self._col_drag_start_x = event.x_root
        n_visible = len([dc for dc in self._col_order if dc not in self._col_hidden])
        self._col_drag_max_slot = n_visible + 1
        self._col_drag_snap = {
            slot: (self._slot_to_data_col(slot), self._COL_W[slot])
            for slot in range(1, self._col_drag_max_slot + 1)
        }

    def _on_header_col_drag_motion(self, event: tk.Event) -> None:
        if self._col_drag_col is None:
            return
        col = self._col_drag_col
        delta = event.x_root - self._col_drag_start_x
        snap = self._col_drag_snap

        max_slot = getattr(self, "_col_drag_max_slot", 7)
        left_slots = list(range(col, 0, -1))
        right_slots = list(range(col + 1, max_slot + 1))

        def distribute(slots: list[int], budget: int) -> dict[int, int]:
            """Shrink/grow slots greedily.
            budget>0 grows the first slot; budget<0 shrinks slots in order."""
            new_w: dict[int, int] = {}
            remaining = budget
            for s in slots:
                dc, orig = snap[s]
                mn = scaled(self._COL_MIN_W.get(dc, 30))
                if remaining >= 0:
                    new_w[s] = orig + remaining
                    remaining = 0
                else:
                    can_shrink = orig - mn
                    take = max(remaining, -can_shrink)
                    new_w[s] = orig + take
                    remaining -= take
                    if remaining == 0:
                        break
            for s in slots:
                if s not in new_w:
                    new_w[s] = snap[s][1]
            return new_w

        if delta < 0:
            left_new = distribute(left_slots, delta)
            actual = sum(left_new[s] - snap[s][1] for s in left_slots)
            right_new = distribute(right_slots, -actual)
        else:
            right_new = distribute(right_slots, -delta)
            actual = sum(snap[s][1] - right_new[s] for s in right_slots)
            left_new = distribute(left_slots, actual)

        for s, (dc, _) in snap.items():
            w = left_new.get(s, right_new.get(s, snap[s][1]))
            self._col_w_override[dc] = w

        self._layout_columns(self._canvas_w)
        self._update_header(self._canvas_w)
        self._redraw()

    def _on_header_col_drag_end(self, event: tk.Event) -> None:
        self._col_drag_col = None
        save_column_widths(self._col_w_override)

    def _on_divider_drag_reset(self, event: tk.Event, col: int) -> None:
        """Double-click a divider to reset both adjacent columns to auto width."""
        left_dc = self._slot_to_data_col(col)
        right_dc = self._slot_to_data_col(col + 1)
        self._col_w_override.pop(left_dc, None)
        self._col_w_override.pop(right_dc, None)
        save_column_widths(self._col_w_override)
        self._layout_columns(self._canvas_w)
        self._update_header(self._canvas_w)
        self._redraw()

    # ------------------------------------------------------------------
    # Column visibility menu
    # ------------------------------------------------------------------

    def _show_column_menu(self) -> None:
        """Popup menu to toggle column visibility. Persists to amethyst.ini."""
        # Reuse a single popup instance — recreating it leaks <FocusOut>/click
        # bindings on the app toplevel (added with add="+") which then cause
        # the menu to instantly dismiss on subsequent opens.
        menu = self._col_menu_popup
        if menu is None or not menu.winfo_exists():
            menu = CTkPopupMenu(self.winfo_toplevel(), width=200, title="")
            self._col_menu_popup = menu
        else:
            menu.clear()
        for dc in self._col_order:
            title = self._DATA_COL_TITLES.get(dc, f"Col {dc}")
            visible = dc not in self._col_hidden
            prefix = "☑  " if visible else "☐  "
            menu.add_command(
                prefix + title,
                lambda d=dc: self._toggle_column_hidden(d),
                font=("Cantarell", _theme.FONT_NORMAL[1]),
            )
        try:
            btn = self._col_menu_btn
            x = btn.winfo_rootx()
            y = btn.winfo_rooty() + btn.winfo_height()
            menu.popup(x - scaled(170), y)
        except Exception:
            menu.popup()

    def _toggle_column_hidden(self, dc: int) -> None:
        """Toggle a column's visibility and persist."""
        became_visible = False
        if dc in self._col_hidden:
            self._col_hidden.discard(dc)
            became_visible = True
        else:
            # Don't allow hiding every non-name column; keep at least one visible.
            if len(self._col_hidden) + 1 >= len(self._col_order):
                return
            self._col_hidden.add(dc)
        save_column_hidden(self._col_hidden)
        self._layout_columns(self._canvas_w)
        self._update_header(self._canvas_w)
        self._redraw()
        # Size column (8) folder sizes are only computed when visible — trigger a
        # rescan now so values populate the first time the column is enabled.
        if became_visible and dc == 8 and not self._mod_sizes:
            self._scan_meta_flags_async()

    # ------------------------------------------------------------------
    # Column reorder drag (header label drag-to-move)
    # ------------------------------------------------------------------

    def _on_hdr_drag_start(self, event: tk.Event, data_col: int,
                           sort_key: str | None) -> None:
        self._hdr_drag_col = data_col
        self._hdr_drag_sort_key = sort_key
        self._hdr_drag_start_x = event.x_root
        self._hdr_drag_moved = False

    def _on_hdr_drag_motion(self, event: tk.Event) -> None:
        if self._hdr_drag_col is None:
            return
        dx = abs(event.x_root - self._hdr_drag_start_x)
        if dx > 5:
            self._hdr_drag_moved = True
        if not self._hdr_drag_moved:
            return
        x_root, y_root = event.x_root, event.y_root
        if self._hdr_drag_ghost is None:
            dc = self._hdr_drag_col
            title = self._DATA_COL_TITLES.get(dc, "")
            self._hdr_drag_ghost = tk.Label(
                self._header, text=title,
                font=(_theme.FONT_FAMILY, _theme.FS11, "bold"),
                fg=ACCENT, bg=BG_SELECT_BAR, relief="solid", bd=1,
                padx=4,
            )
        hdr_x = self._header.winfo_rootx()
        hdr_y = self._header.winfo_rooty()
        ghost_x = x_root - hdr_x - 20
        self._hdr_drag_ghost.place(x=ghost_x, y=2, height=scaled(24))
        self._hdr_drag_ghost.lift()
        self._hdr_drag_highlight(event.x_root)

    def _hdr_drag_highlight(self, x_root: int) -> None:
        """Update header label backgrounds to show the drop target."""
        hdr_x = self._header.winfo_rootx()
        local_x = x_root - hdr_x
        target_slot = self._hdr_slot_at(local_x)
        visible = [dc for dc in self._col_order if dc not in self._col_hidden]
        slot_data_cols = [0, 1] + visible
        for k, lbl in enumerate(self._header_labels):
            slot = k
            dc = slot_data_cols[slot] if slot < len(slot_data_cols) else 0
            is_movable = dc in (2, 3, 4, 5, 6, 7, 8)
            if is_movable and slot == target_slot:
                lbl.configure(bg=BG_SELECT_BAR)
            else:
                lbl.configure(bg=BG_HEADER)

    def _hdr_slot_at(self, local_x: int) -> int:
        """Return the slot index at header-local x, clamped to visible movable range."""
        visible = [dc for dc in self._col_order if dc not in self._col_hidden]
        n_visible = len(visible)
        if n_visible == 0:
            return 2
        max_slot = n_visible + 1
        for slot in range(max_slot, 1, -1):
            if local_x >= self._COL_X[slot]:
                return slot
        return 2

    def _on_hdr_drag_end(self, event: tk.Event) -> None:
        if self._hdr_drag_col is None:
            return
        dc = self._hdr_drag_col
        moved = self._hdr_drag_moved
        sort_key = getattr(self, "_hdr_drag_sort_key", None)
        if self._hdr_drag_ghost is not None:
            self._hdr_drag_ghost.destroy()
            self._hdr_drag_ghost = None
        for lbl in self._header_labels:
            lbl.configure(bg=BG_HEADER)
        self._hdr_drag_col = None
        self._hdr_drag_moved = False
        if not moved:
            # Treat as a click — sort if sortable
            if sort_key:
                self._on_header_click(sort_key)
            return
        hdr_x = self._header.winfo_rootx()
        local_x = event.x_root - hdr_x
        target_slot = self._hdr_slot_at(local_x)
        src_slot = self._col_pos.get(dc, -1)
        visible = [c for c in self._col_order if c not in self._col_hidden]
        n_visible = len(visible)
        if src_slot == target_slot or target_slot < 2 or target_slot > (n_visible + 1):
            return
        # Swap within the visible list, then splice back into _col_order
        # preserving hidden-column positions.
        src_k = src_slot - 2
        tgt_k = target_slot - 2
        if src_k < 0 or src_k >= n_visible or tgt_k < 0 or tgt_k >= n_visible:
            return
        visible[src_k], visible[tgt_k] = visible[tgt_k], visible[src_k]
        new_order: list[int] = []
        vi = 0
        for c in self._col_order:
            if c in self._col_hidden:
                new_order.append(c)
            else:
                new_order.append(visible[vi])
                vi += 1
        self._col_order = new_order
        save_column_order(new_order)
        # Rebuild header labels so bindings use the new order.
        for lbl in self._header_labels:
            lbl.destroy()
        self._header_labels.clear()
        self._layout_columns(self._canvas_w)
        self._update_header(self._canvas_w)
        self._redraw()

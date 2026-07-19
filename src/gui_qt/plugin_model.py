"""Plugin-tab model — QAbstractTableModel over PluginRow list.

Columns: Plugin Name, Flags, Lock, Index (checkbox painted into col 0 by the
delegate). Toggling enable writes back to plugins.txt via plugin_state.save.
"""

from __future__ import annotations

# Crash-proof diagnostic prints (Flatpak stdout can raise BrokenPipeError and
# kill worker threads). See Utils.app_log.safe_print.
from Utils.app_log import safe_print as print  # noqa: A004

from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, Signal, QT_TRANSLATE_NOOP,
)

from gui_qt.plugin_state import PluginRow, save_plugins, compute_game_indexes

COL_NAME = 0
COL_FLAGS = 1
COL_LOCK = 2
COL_PRIORITY = 3    # list-position counter (000, 001…), labelled "P"
COL_GAME_INDEX = 4  # MO2-style hex load index the game assigns (00, FE:000…)
COLUMNS = ["Plugin Name", "Flags", "", "P", "Index"]
# headerData() translates these at display time (self.tr(COLUMNS[i])); register
# the literals so lupdate extracts them under the PluginModel context. Must be
# explicit literal calls — lupdate can't see through a loop variable.
_COL_TR = (
    QT_TRANSLATE_NOOP("PluginModel", "Plugin Name"),
    QT_TRANSLATE_NOOP("PluginModel", "Flags"),
    QT_TRANSLATE_NOOP("PluginModel", "P"),
    QT_TRANSLATE_NOOP("PluginModel", "Index"),
)

RowRole = Qt.UserRole + 1      # the PluginRow
PFlagsRole = Qt.UserRole + 2   # int flag bitmask
PHighlightRole = Qt.UserRole + 3  # 0 none, 3 master(green), 2 anchor(orange), 1 higher, -1 lower


class PluginModel(QAbstractTableModel):
    # Emitted after the plugin order / enable state is persisted (reorder or
    # toggle). BSA load order follows plugin load order, so the window listens
    # to this to recompute BSA conflicts. See _save().
    order_changed = Signal()
    # plugins.txt write failed — the window surfaces a toast.
    save_failed = Signal(str)

    def __init__(self, rows: list[PluginRow] | None = None):
        super().__init__()
        self._rows: list[PluginRow] = rows or []
        self._game = None
        self._profile = None
        self._locks: dict[str, bool] = {}     # plugin name (lower) → locked
        self._profile_dir = None
        # Cross-panel highlight: plugin names (lower) → code (2 anchor / 1 / -1).
        self._highlights: dict[str, int] = {}
        # plugin name (lower) → non-default userlist group (flags tooltip).
        self._ul_groups: dict[str, str] = {}
        self._game_indexes: list[str] = []

    def set_rows(self, rows, game=None, profile=None, profile_dir=None):
        self.beginResetModel()
        self._rows = rows
        self._game = game
        self._profile = profile
        self._profile_dir = profile_dir
        self._locks = {}
        self._highlights = {}
        self._game_indexes = compute_game_indexes(self._rows)
        if profile_dir is not None:
            try:
                from Utils.profile_state import read_plugin_locks
                self._locks = read_plugin_locks(profile_dir) or {}
            except Exception:
                self._locks = {}
        self.endResetModel()

    def is_locked(self, i: int) -> bool:
        return bool(self._locks.get(self._rows[i].name.lower(), False))

    def set_userlist_groups(self, groups: dict[str, str]) -> None:
        """groups maps plugin name (lower) → non-default userlist group name.
        Feeds the Flags-column tooltip (Tk parity)."""
        self._ul_groups = dict(groups or {})

    def userlist_group(self, name: str) -> str | None:
        """Non-default userlist group for *name* (or None). Read by the delegate
        to append a 'Group: …' line to the userlist-dot tooltip."""
        return self._ul_groups.get(name.lower())

    def enabled_lower(self) -> set[str]:
        """Set of enabled plugin filenames (lowercase). Feeds the LOOT tooltip's
        requirement/incompatibility filtering."""
        return {r.name.lower() for r in self._rows if r.enabled}

    def all_lower(self) -> set[str]:
        """Set of ALL plugin filenames (lowercase), regardless of enabled state."""
        return {r.name.lower() for r in self._rows}

    def set_highlights(self, highlights: dict[str, int]) -> None:
        """highlights maps plugin name (lower) → code (3 master / 2 anchor /
        1 higher / -1 lower). Replaces the whole map and repaints."""
        self._highlights = dict(highlights or {})
        if self._rows:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._rows) - 1, COL_GAME_INDEX),
                                  [PHighlightRole])

    def toggle_lock(self, i: int):
        name = self._rows[i].name.lower()
        self._locks[name] = not self._locks.get(name, False)
        idx = self.index(i, COL_LOCK)
        self.dataChanged.emit(idx, idx, [])
        if self._profile_dir is not None:
            try:
                from Utils.profile_state import write_plugin_locks
                write_plugin_locks(self._profile_dir, self._locks)
            except Exception as exc:
                print(f"[gui_qt] plugin locks save failed: {exc}", flush=True)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            # "" (the lock column) stays empty; others are translated.
            return self.tr(COLUMNS[section]) if COLUMNS[section] else ""
        if (orientation == Qt.Horizontal and role == Qt.DecorationRole
                and section == COL_LOCK
                and not getattr(self, "_suppress_header_deco", False)):
            # The lock column has no text label; show a lock icon instead so
            # the header reads (matches the per-row lock glyph). TkStyleHeader
            # centres it and suppresses this during its chrome pass.
            from gui_qt.icons import icon
            return icon("lock.png", 14)
        return None

    def row(self, i: int) -> PluginRow:
        return self._rows[i]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        col = index.column()
        if role == RowRole:
            return r
        if role == PFlagsRole:
            return r.flags
        if role == PHighlightRole:
            return self._highlights.get(r.name.lower(), 0)
        if role == Qt.DisplayRole:
            if col == COL_NAME:
                return r.name
            if col == COL_PRIORITY:
                return f"{index.row():03d}"
            if col == COL_GAME_INDEX:
                i = index.row()
                return self._game_indexes[i] if i < len(self._game_indexes) else ""
            return ""
        # Flags-column tooltips are handled per-icon by PluginDelegate.helpEvent
        # (Tk parity), not via a whole-cell Qt.ToolTipRole.
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def toggle(self, i: int):
        r = self._rows[i]
        if r.vanilla:
            return   # vanilla plugins are always-on; can't be disabled
        r.enabled = not r.enabled
        # Whole row: enabled state dims the text in every column.
        self.dataChanged.emit(self.index(i, 0),
                              self.index(i, len(COLUMNS) - 1),
                              [RowRole, Qt.DisplayRole])
        # Disabling/enabling a plugin renumbers every following plugin's game
        # index, so refresh the whole column (not just this row).
        self._refresh_game_indexes()
        self._save()

    def set_enabled(self, indices, enabled: bool):
        """Enable/disable the given rows (skips vanilla — always-on), persist +
        repaint. Mirrors toggle() for the context menu's Enable/Disable items."""
        changed = [i for i in indices
                   if 0 <= i < len(self._rows) and not self._rows[i].vanilla]
        if not changed:
            return
        for i in changed:
            self._rows[i].enabled = enabled
        lo, hi = min(changed), max(changed)
        self.dataChanged.emit(self.index(lo, 0),
                              self.index(hi, len(COLUMNS) - 1),
                              [RowRole, Qt.DisplayRole])
        self._refresh_game_indexes()
        self._save()

    def is_movable(self, i: int) -> bool:
        """A row may be dragged unless it's vanilla (pinned) or user-locked."""
        if not (0 <= i < len(self._rows)):
            return False
        if self._rows[i].vanilla:
            return False
        return not self.is_locked(i)

    def _first_movable(self) -> int:
        """Lowest row index a non-vanilla plugin may occupy (after the pinned
        vanilla block at the top)."""
        i = 0
        while i < len(self._rows) and self._rows[i].vanilla:
            i += 1
        return i

    def move_rows(self, src_rows: list[int], dest: int) -> bool:
        """Move a contiguous block of movable rows so it lands before *dest*.
        Vanilla rows stay pinned at the top; locked rows never move. Persists
        order to loadorder.txt (+ plugins.txt) on success."""
        src = sorted(set(src_rows))
        if not src or any(not self.is_movable(i) for i in src):
            return False
        # Block must be contiguous for beginMoveRows.
        if src[-1] - src[0] != len(src) - 1:
            return False
        first, last = src[0], src[-1]
        floor = self._first_movable()
        dest = max(floor, min(dest, len(self._rows)))
        if first <= dest <= last + 1:
            return False   # no-op / inside the moved span
        if not self.beginMoveRows(QModelIndex(), first, last, QModelIndex(), dest):
            return False
        block = self._rows[first:last + 1]
        del self._rows[first:last + 1]
        insert_at = dest if dest < first else dest - len(block)
        self._rows[insert_at:insert_at] = block
        self.endMoveRows()
        self._refresh_game_indexes()
        self._save()
        return True

    def _refresh_game_indexes(self):
        """Recompute the cached MO2-style game indexes after an order/enabled
        change and repaint that column (data() reads the cache)."""
        self._game_indexes = compute_game_indexes(self._rows)
        if self._rows:
            self.dataChanged.emit(self.index(0, COL_GAME_INDEX),
                                  self.index(len(self._rows) - 1, COL_GAME_INDEX),
                                  [Qt.DisplayRole])

    def _save(self):
        if self._game is not None and self._profile:
            try:
                save_plugins(self._game, self._profile, self._rows)
            except Exception as exc:
                print(f"[gui_qt] plugins.txt save failed: {exc}", flush=True)
                self.save_failed.emit(f"Plugins save failed: {exc}")
                return
            # loadorder.txt / plugins.txt are now on disk — let the window
            # recompute BSA conflicts (BSA winners follow plugin load order).
            self.order_changed.emit()

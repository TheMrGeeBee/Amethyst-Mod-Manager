"""Modlist model — QAbstractTableModel over the ModEntry list.

Columns: Mod Name, Flags, Conflicts, Installed, Version, Priority (the checkbox
is painted into column 0 by the delegate). Fed by read_modlist; version /
installed / flags / conflicts are optional dicts keyed by mod name (blank when
absent). Index 0 = highest priority; the Priority column shows a descending
number (highest-priority row = largest value).
"""

from __future__ import annotations

from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QMimeData, QByteArray, Signal,
)

from Utils.modlist import ModEntry, read_modlist
from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME
from gui_qt.modlist_sort import (
    DIVIDER_NAME, build_display, uninvert_display, make_divider, is_reverse,
)

# UI-only boundary separators: pinned + locked, never written to modlist.txt.
# Overwrite floats at the top, Root Folder at the bottom (Tk parity).
_BOUNDARY_NAMES = (OVERWRITE_NAME, ROOT_FOLDER_NAME)
# All UI-only pinned rows: boundaries + the reverse-mode float divider.
_PINNED_NAMES = _BOUNDARY_NAMES + (DIVIDER_NAME,)


# Column indices. Order mirrors the Tk app: Category right after Name, Size last.
COL_NAME = 0
COL_CATEGORY = 1
COL_FLAGS = 2
COL_CONFLICTS = 3
COL_INSTALLED = 4
COL_VERSION = 5
COL_PRIORITY = 6
COL_SIZE = 7
COLUMNS = ["Mod Name", "Category", "Flags", "Conflicts", "Installed",
           "Version", "Priority", "Size"]

# Custom roles for the delegate.
EntryRole = Qt.UserRole + 1        # the ModEntry
ConflictRole = Qt.UserRole + 2     # int: 0 none, 1 wins, -1 loses, 2 mixed (loose)
PriorityRole = Qt.UserRole + 3     # int display priority
FlagsRole = Qt.UserRole + 4        # int bitmask (gui_qt.modlist_data.FLAG_*)
BsaConflictRole = Qt.UserRole + 5  # int: BSA/BA2 archive conflict code
HighlightRole = Qt.UserRole + 6    # int: 0 none, 1 higher(green), -1 lower(red),
                                   #      2 anchor(orange, plugin-selected mod)

_MIME = "application/x-amethyst-modrows"


class ModListModel(QAbstractTableModel):
    # Mods were enabled/disabled and modlist.txt saved. Payload is
    # list[(mod_name, now_enabled)] — the window syncs plugins.txt to it
    # (Tk parity: _sync_plugins_for_toggle).
    enabled_changed = Signal(object)
    # modlist.txt write failed — the window surfaces a toast (console print
    # alone loses the user's reorder/toggle silently).
    save_failed = Signal(str)

    def __init__(self, entries: list[ModEntry] | None = None,
                 versions: dict[str, str] | None = None,
                 installed: dict[str, str] | None = None,
                 conflicts: dict[str, int] | None = None):
        super().__init__()
        # Source of truth: entries in natural modlist.txt order (boundaries
        # included). _entries is the DISPLAY list — when no column sort is
        # active it IS _natural (same object); a sort derives a permutation
        # (plus the divider row in reverse-priority mode). Both hold the same
        # ModEntry objects, so per-entry edits need no translation; save()
        # always writes from _natural.
        self._natural: list[ModEntry] = entries or []
        self._entries: list[ModEntry] = self._natural
        # Active column sort ("name"/"category"/…/"priority") + direction.
        self._sort_key: str | None = None
        self._sort_ascending: bool = True
        # Reverse-mode divider entry, reused across rebuilds so an unchanged
        # layout compares identical (no spurious layoutChanged).
        self._divider: ModEntry = make_divider()
        self._versions = versions or {}
        self._installed = installed or {}
        self._categories: dict[str, str] = {}
        # Formatted mod folder sizes ("12 MB"). Computed lazily — only when the
        # Size column is visible — so a default-hidden Size costs no disk walk.
        self._sizes: dict[str, str] = {}
        # Raw byte counts backing the Size column sort.
        self._size_bytes: dict[str, int] = {}
        self._conflicts = conflicts or {}
        self._bsa_conflicts: dict[str, int] = {}
        self._flags: dict[str, int] = {}
        # Per-mod user note text (for the Note flag's hover tooltip). Kept in sync
        # with the FLAG_NOTE bit; empty when no note.
        self._notes: dict[str, str] = {}
        # Mods modified in the Mod Files tab (excluded files / strip prefixes).
        # Kept separate from the meta-derived flags so a meta refresh doesn't
        # drop it; OR'd into the FlagsRole bitmask. See modlist_data.FLAG_MODIFIED_MF.
        self._modified_mf: set[str] = set()
        # Filemap-derived flag overlays (computed during the conflict/filemap
        # rebuild, not from meta.ini): mods with pre-RTX (natives/x64) files and
        # mods that own files with a custom root-routing rule. OR'd into FlagsRole.
        self._prertx_mods: set[str] = set()
        self._root_rule_mods: set[str] = set()
        # Highlight state: mod names tinted green (wins over selection) / red
        # (loses to selection), and a set of "anchor" mods (orange) — the mod a
        # selected plugin belongs to. Driven by the view's cross-panel wiring.
        self._hl_higher: set[str] = set()
        self._hl_lower: set[str] = set()
        self._hl_anchor: set[str] = set()
        # When set, toggle/reorder edits are written back here.
        self.modlist_path = None
        # Optional callback invoked after a save (e.g. to rebuild conflicts).
        self.on_saved = None
        # Separator state (keyed by separator display name), persisted per
        # profile. collapsed → its mods are hidden; locked → its block can't be
        # dragged. Set via set_separator_state(); persistence lives in the view.
        self._collapsed: set[str] = set()
        self._sep_locks: dict[str, bool] = {}
        # Per-row memo for _separator_highlight (block walk is O(block size)
        # and data() asks per paint). Cleared on highlight/collapse/entry edits.
        self._sep_hl_cache: dict[int, int] = {}

    # ---- loading ----------------------------------------------------------
    @classmethod
    def from_modlist(cls, modlist_path, **kw) -> "ModListModel":
        return cls(read_modlist(modlist_path), **kw)

    @staticmethod
    def _with_boundaries(entries: list[ModEntry]) -> list[ModEntry]:
        """Wrap raw entries with the pinned Overwrite (top) + Root Folder
        (bottom) boundary separators. They're locked + UI-only."""
        body = [e for e in entries if e.name not in _PINNED_NAMES]
        top = ModEntry(OVERWRITE_NAME, True, True, True)
        bot = ModEntry(ROOT_FOLDER_NAME, True, True, True)
        return [top] + body + [bot]

    def set_entries(self, entries: list[ModEntry]) -> None:
        self.beginResetModel()
        self._natural = self._with_boundaries(entries)
        self._entries = self._derive_display()
        self._sep_hl_cache.clear()
        self.endResetModel()

    # ---- column sorting -----------------------------------------------------
    def set_sort(self, key: str | None, ascending: bool = True) -> None:
        """Set (or clear with None) the active column sort and rebuild the
        display order. The natural order is untouched."""
        key = key or None
        ascending = bool(ascending)
        if (key, ascending) == (self._sort_key, self._sort_ascending):
            return
        self._sort_key = key
        self._sort_ascending = ascending
        self._rebuild_display()

    def sort_state(self) -> tuple[str | None, bool]:
        return self._sort_key, self._sort_ascending

    @property
    def reverse_mode_active(self) -> bool:
        """True in reverse-priority mode (priority ascending, 0 at top)."""
        return is_reverse(self._sort_key, self._sort_ascending)

    def natural_entries(self) -> list[ModEntry]:
        """Entries in natural modlist.txt order (boundaries included). Any
        code that rebuilds/persists the body MUST start from this, never from
        the display order."""
        return self._natural

    def _sort_ctx(self) -> dict:
        """Per-name data dicts for the sort key functions. Flags are the
        effective bits (meta flags + the filemap/Mod-Files overlays)."""
        flags: dict[str, int] = {}
        for e in self._natural:
            if not e.is_separator:
                flags[e.name] = self._effective_flags(e.name)
        return {
            "categories": self._categories,
            "versions": self._versions,
            "installed": self._installed,
            "size_bytes": self._size_bytes,
            "flags": flags,
            "conflicts": self._conflicts,
        }

    def _derive_display(self) -> list[ModEntry]:
        if not self._sort_key:
            return self._natural
        return build_display(self._natural, self._sort_key,
                             self._sort_ascending, self._sort_ctx(),
                             divider=self._divider)

    def _rebuild_display(self) -> None:
        """Re-derive the display list from the natural order + active sort.
        Uses layoutChanged with a persistent-index remap (by entry identity)
        so selection/scroll follow the rows. No-op if the order is unchanged."""
        old = self._entries
        new = self._derive_display()
        if len(new) == len(old) and all(a is b for a, b in zip(new, old)):
            self._entries = new
            return
        self.layoutAboutToBeChanged.emit()
        old_persist = self.persistentIndexList()
        pos_by_id = {id(e): i for i, e in enumerate(new)}
        self._entries = new
        new_persist = []
        for idx in old_persist:
            e = old[idx.row()] if 0 <= idx.row() < len(old) else None
            r = pos_by_id.get(id(e), -1) if e is not None else -1
            new_persist.append(self.index(r, idx.column()) if r >= 0
                               else QModelIndex())
        self.changePersistentIndexList(old_persist, new_persist)
        self._sep_hl_cache.clear()
        self.layoutChanged.emit()

    def _resort_if_key(self, *keys: str) -> None:
        """Rebuild the display when the active sort depends on data that just
        changed (avoids rebuild storms from unrelated async setters)."""
        if self._sort_key in keys:
            self._rebuild_display()

    def set_sizes(self, sizes: dict[str, str],
                  size_bytes: dict[str, int] | None = None) -> None:
        """Set formatted mod sizes (Size column) + raw bytes (Size sort).
        Repaints just that column — used when the user enables Size from the
        column menu after first load."""
        self._sizes = sizes or {}
        if size_bytes is not None:
            self._size_bytes = size_bytes or {}
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_SIZE),
                                  self.index(len(self._entries) - 1, COL_SIZE),
                                  [Qt.DisplayRole])
        self._resort_if_key("size")

    def set_flags(self, flags: dict[str, int]) -> None:
        self._flags = flags or {}
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_FLAGS),
                                  self.index(len(self._entries) - 1, COL_FLAGS),
                                  [FlagsRole, Qt.DisplayRole])
        self._resort_if_key("flags")

    def set_notes(self, notes: dict[str, str]) -> None:
        """Store per-mod note text (for the Note flag's hover tooltip)."""
        self._notes = notes or {}

    def note_for(self, name: str) -> str:
        return self._notes.get(name, "")

    def set_modified_mf(self, mods: set[str]) -> None:
        """Set which mods are modified in the Mod Files tab (overlays the
        FLAG_MODIFIED_MF eye icon in the Flags column)."""
        self._modified_mf = set(mods or ())
        self._emit_flags_changed()

    def set_prertx_mods(self, mods: set[str]) -> None:
        """Set which mods contain pre-RTX (natives/x64) files — the info icon
        (filemap-derived overlay)."""
        self._prertx_mods = set(mods or ())
        self._emit_flags_changed()

    def set_root_rule_mods(self, mods: set[str]) -> None:
        """Set which mods own files with a custom root-routing rule — the root
        icon (filemap-derived overlay)."""
        self._root_rule_mods = set(mods or ())
        self._emit_flags_changed()

    def _emit_flags_changed(self) -> None:
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_FLAGS),
                                  self.index(len(self._entries) - 1, COL_FLAGS),
                                  [FlagsRole, Qt.DisplayRole])
        self._resort_if_key("flags")

    def set_conflicts(self, conflicts: dict[str, int],
                      bsa_conflicts: dict[str, int] | None = None) -> None:
        self._conflicts = conflicts or {}
        if bsa_conflicts is not None:
            self._bsa_conflicts = bsa_conflicts or {}
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_NAME),
                                  self.index(len(self._entries) - 1, COL_CONFLICTS),
                                  [ConflictRole, BsaConflictRole, Qt.DisplayRole])
        self._resort_if_key("conflicts")

    def _separator_highlight(self, row: int, e) -> int:
        """A separator is tinted ONLY when collapsed AND one of its child mods is
        a highlight partner (Tk parity — an expanded block tints the child mod
        directly instead). anchor(2) > higher(1) > lower(-1).
        Cached per row — data() asks for this on every paint, and the block walk
        is O(block size). Invalidated on highlight/collapse/entry changes."""
        cached = self._sep_hl_cache.get(row)
        if cached is not None:
            return cached
        code = self._separator_highlight_compute(row, e)
        self._sep_hl_cache[row] = code
        return code

    def _separator_highlight_compute(self, row: int, e) -> int:
        # [Overwrite] is a boundary separator but DOES light up (green) when it
        # wins over the selection — same as Tk. Root Folder never highlights.
        if e.name == OVERWRITE_NAME:
            if e.name in self._hl_anchor:
                return 2
            if e.name in self._hl_higher:
                return 1
            if e.name in self._hl_lower:
                return -1
            return 0
        if e.name in _BOUNDARY_NAMES:
            return 0
        if e.display_name not in self._collapsed:
            return 0
        anchor = higher = lower = False
        for r in self.sep_block_rows(row):
            name = self._entries[r].name
            if name in self._hl_anchor:
                anchor = True
            elif name in self._hl_higher:
                higher = True
            elif name in self._hl_lower:
                lower = True
        if anchor:
            return 2
        if higher:
            return 1
        if lower:
            return -1
        return 0

    def set_highlights(self, higher=None, lower=None, anchor=None) -> None:
        """Update conflict/anchor highlight sets and repaint the whole list."""
        self._hl_higher = set(higher or ())
        self._hl_lower = set(lower or ())
        self._hl_anchor = set(anchor or ())
        self._sep_hl_cache.clear()
        if self._entries:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._entries) - 1, COL_PRIORITY),
                                  [HighlightRole])

    # ---- Qt model interface ----------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._entries)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            # TkStyleHeader paints the label itself (elided clear of the sort
            # triangle) — it suppresses the native text for the chrome pass.
            if getattr(self, "_suppress_header_text", False):
                return ""
            # The Name column hosts the column-menu button at its left edge;
            # pad the (left-aligned) label so it isn't drawn under the button.
            # Display-only — persistence keys off the canonical COLUMNS names.
            if section == COL_NAME:
                return "    " + COLUMNS[section]
            return COLUMNS[section]
        return None

    def _priority_for_row(self, row: int) -> int:
        """Priority number for a display row. Normally the natural descending
        number (top of natural order = largest); in reverse-priority mode the
        display is the exact inversion, so counting non-separators ABOVE the
        display row yields the same per-mod value with 0 at the top."""
        e = self._entries[row]
        if e.is_separator:
            return -1
        if self.reverse_mode_active:
            return sum(1 for x in self._entries[:row] if not x.is_separator)
        if self._entries is self._natural:
            below = sum(1 for x in self._entries[row:] if not x.is_separator)
            return below - 1
        # Non-priority sort: the number reflects the NATURAL position (Tk
        # parity — sorting by name doesn't renumber priorities).
        try:
            ni = next(i for i, x in enumerate(self._natural) if x is e)
        except StopIteration:
            return -1
        below = sum(1 for x in self._natural[ni:] if not x.is_separator)
        return below - 1

    def _effective_flags(self, name: str) -> int:
        """Meta flag bits + the Mod-Files / filemap-derived overlays."""
        from gui_qt.modlist_data import (
            FLAG_MODIFIED_MF, FLAG_PRERTX, FLAG_ROOT_RULE)
        bits = self._flags.get(name, 0)
        if name in self._modified_mf:
            bits |= FLAG_MODIFIED_MF
        if name in self._prertx_mods:
            bits |= FLAG_PRERTX
        if name in self._root_rule_mods:
            bits |= FLAG_ROOT_RULE
        return bits

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        e = self._entries[index.row()]
        col = index.column()

        if role == EntryRole:
            return e
        if role == ConflictRole:
            return 0 if e.is_separator else self._conflicts.get(e.name, 0)
        if role == BsaConflictRole:
            return 0 if e.is_separator else self._bsa_conflicts.get(e.name, 0)
        if role == HighlightRole:
            if e.is_separator:
                return self._separator_highlight(index.row(), e)
            if e.name in self._hl_anchor:
                return 2
            if e.name in self._hl_higher:
                return 1
            if e.name in self._hl_lower:
                return -1
            return 0
        if role == FlagsRole:
            return 0 if e.is_separator else self._effective_flags(e.name)
        if role == PriorityRole:
            return self._priority_for_row(index.row())

        if role == Qt.DisplayRole:
            if e.is_separator:
                return e.display_name if col == COL_NAME else ""
            if col == COL_NAME:
                return e.display_name
            if col == COL_CATEGORY:
                return self._categories.get(e.name, "")
            if col == COL_VERSION:
                return self._versions.get(e.name, "")
            if col == COL_INSTALLED:
                return self._installed.get(e.name, "")
            if col == COL_SIZE:
                return self._sizes.get(e.name, "")
            if col == COL_PRIORITY:
                p = self._priority_for_row(index.row())
                return str(p) if p >= 0 else ""
            return ""
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemIsDropEnabled
        e = self._entries[index.row()]
        # The reverse-mode float divider is a pure visual marker: enabled only
        # (not selectable, not draggable, not a drop target).
        if e.name == DIVIDER_NAME:
            return Qt.ItemIsEnabled
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        # Draggable unless pinned: boundary separators + locked MODS can't be
        # dragged (a regular separator reads as locked=True but IS draggable).
        pinned = e.name in _BOUNDARY_NAMES or (not e.is_separator and e.locked)
        if not pinned:
            f |= Qt.ItemIsDragEnabled
        if not e.is_separator:
            f |= Qt.ItemIsDropEnabled
        return f

    # ---- drop-validity (keep boundary separators pinned) ------------------
    def _movable_span(self) -> tuple[int, int]:
        """[lo, hi) row range mods may live in: below a leading locked
        separator (Overwrite) and above a trailing locked one (Root Folder)."""
        lo, hi = 0, len(self._entries)
        if self._entries and self._entries[0].is_separator and self._entries[0].locked:
            lo = 1
        if (self._entries and self._entries[-1].is_separator
                and self._entries[-1].locked):
            hi = len(self._entries) - 1
        return lo, hi

    def canDropMimeData(self, data, action, row, col, parent):
        if action != Qt.MoveAction or not data.hasFormat(_MIME):
            return False
        dest = row if row != -1 else (parent.row() if parent.isValid()
                                      else len(self._entries))
        lo, hi = self._movable_span()
        return lo <= dest <= hi

    # ---- toggling ---------------------------------------------------------
    def toggle(self, row: int) -> None:
        e = self._entries[row]
        if e.is_separator or e.locked:
            return
        e.enabled = not e.enabled
        idx = self.index(row, COL_NAME)
        self.dataChanged.emit(idx, idx, [EntryRole, Qt.DisplayRole])
        self.save()
        self.enabled_changed.emit([(e.name, e.enabled)])

    def set_rows_enabled(self, rows, enabled: bool) -> None:
        """Enable/disable the mods at *rows* (skips separators + locked), then
        save + emit enabled_changed ONCE for the whole batch."""
        changed: list[tuple[str, bool]] = []
        for r in rows:
            e = self._entries[r]
            if e.is_separator or e.locked or e.enabled == enabled:
                continue
            e.enabled = enabled
            changed.append((e.name, enabled))
            idx = self.index(r, COL_NAME)
            self.dataChanged.emit(idx, idx, [EntryRole, Qt.DisplayRole])
        if changed:
            self.save()
            self.enabled_changed.emit(changed)

    def entry(self, row: int) -> ModEntry:
        return self._entries[row]

    # ---- separators -------------------------------------------------------
    def set_separator_state(self, collapsed: set[str], locks: dict[str, bool]):
        self._collapsed = set(collapsed or set())
        self._sep_locks = dict(locks or {})
        self._sep_hl_cache.clear()

    def is_collapsed(self, sep_name: str) -> bool:
        return sep_name in self._collapsed

    def is_sep_locked(self, sep_name: str) -> bool:
        return bool(self._sep_locks.get(sep_name, False))

    def toggle_collapse(self, row: int) -> set[str]:
        e = self._entries[row]
        if not e.is_separator or e.name == DIVIDER_NAME:
            return self._collapsed
        name = e.display_name
        if name in self._collapsed:
            self._collapsed.discard(name)
        else:
            self._collapsed.add(name)
        self._sep_hl_cache.clear()
        return self._collapsed

    def toggle_sep_lock(self, row: int) -> dict[str, bool]:
        e = self._entries[row]
        if e.is_separator and e.name not in _PINNED_NAMES:
            name = e.display_name
            self._sep_locks[name] = not self._sep_locks.get(name, False)
        return self._sep_locks

    # ---- bulk separator / mod operations (footer buttons) -----------------
    def collapsible_separator_names(self) -> list[str]:
        """Display names of separators that can be collapsed (excludes the
        Overwrite / Root Folder boundaries + the reverse-mode divider)."""
        return [e.display_name for e in self._entries
                if e.is_separator and e.name not in _PINNED_NAMES]

    def any_collapsed(self) -> bool:
        names = self.collapsible_separator_names()
        return any(n in self._collapsed for n in names)

    def set_all_collapsed(self, collapsed: bool) -> set[str]:
        """Collapse or expand every (non-boundary) separator. Returns the new
        collapsed set so the view can persist it."""
        names = set(self.collapsible_separator_names())
        if collapsed:
            self._collapsed |= names
        else:
            self._collapsed -= names
        return self._collapsed

    def all_mods_enabled(self) -> bool:
        mods = [e for e in self._entries if not e.is_separator and not e.locked]
        return bool(mods) and all(e.enabled for e in mods)

    def has_disabled_mods(self) -> bool:
        return any(not e.is_separator and not e.locked and not e.enabled
                   for e in self._entries)

    def set_all_enabled(self, enabled: bool) -> None:
        """Enable/disable every toggleable mod, then save once."""
        changed: list[tuple[str, bool]] = []
        for r, e in enumerate(self._entries):
            if e.is_separator or e.locked:
                continue
            if e.enabled != enabled:
                e.enabled = enabled
                changed.append((e.name, enabled))
        if changed:
            self.dataChanged.emit(
                self.index(0, COL_NAME),
                self.index(len(self._entries) - 1, COL_NAME),
                [EntryRole, Qt.DisplayRole])
            self.save()
            self.enabled_changed.emit(changed)

    def hidden_rows(self) -> set[int]:
        """Rows to hide: mods that fall under a collapsed separator (up to the
        next separator). Separators themselves are never hidden.

        The Overwrite / Root Folder boundaries never collapse their block — they
        aren't user-collapsible (Tk excludes them from the toggle set), so a
        stale '[Overwrite]' in the persisted collapsed set must NOT hide the
        ungrouped mods that sit between Overwrite and the first real separator.
        """
        hidden: set[int] = set()
        collapsing = False
        for i, e in enumerate(self._entries):
            if e.is_separator:
                collapsing = (e.name not in _BOUNDARY_NAMES
                              and e.display_name in self._collapsed)
            elif collapsing:
                hidden.add(i)
        return hidden

    def sep_block_rows(self, sep_row: int) -> range:
        """Row range [sep_row+1, next-separator) — the mods a separator owns."""
        end = sep_row + 1
        while end < len(self._entries) and not self._entries[end].is_separator:
            end += 1
        return range(sep_row + 1, end)

    # ---- persistence ------------------------------------------------------
    def save(self) -> None:
        """Write the current entries back to modlist.txt (no-op if no path).
        The Overwrite / Root Folder boundary separators are UI-only and are
        stripped before writing. Fires on_saved() so the view can rebuild."""
        if self.modlist_path is None:
            return
        # Every structural edit (drag, remove, add-separator, set_priority…)
        # funnels through here — row→block mapping may have changed.
        self._sep_hl_cache.clear()
        from Utils.modlist import write_modlist
        # ALWAYS write the natural order — the display list may be a sorted /
        # inverted permutation (and contains the divider in reverse mode).
        body = [e for e in self._natural if e.name not in _PINNED_NAMES]
        try:
            write_modlist(self.modlist_path, body)
        except Exception as exc:
            print(f"[gui_qt] modlist save failed: {exc}", flush=True)
            self.save_failed.emit(f"Modlist save failed: {exc}")
            return
        if self.on_saved:
            self.on_saved()

    # ---- structural edits (context-menu actions) --------------------------
    def rename(self, row: int, new_name: str) -> None:
        e = self._entries[row]
        # Block only pinned boundaries + locked mods (separators read as locked
        # but are renamable).
        if e.name in _PINNED_NAMES or (not e.is_separator and e.locked):
            return
        # Separators keep their suffix so they stay separators on write-out.
        from Utils.modlist import _SEPARATOR_SUFFIX
        e.name = (new_name + _SEPARATOR_SUFFIX) if e.is_separator else new_name
        idx = self.index(row, COL_NAME)
        self.dataChanged.emit(idx, idx, [Qt.DisplayRole, EntryRole])
        self.save()

    def _natural_row_of(self, e: ModEntry) -> int:
        """Position of an entry object in the natural list (identity match)."""
        for i, x in enumerate(self._natural):
            if x is e:
                return i
        return -1

    def set_priority(self, row: int, priority: int) -> None:
        """Move a mod so its descending-priority number becomes *priority*.
        Re-positions within the NATURAL non-separator ordering (clamped)."""
        e = self._entries[row]
        if e.is_separator:
            return
        nat = self._natural
        nonsep = [i for i, x in enumerate(nat) if not x.is_separator]
        n = len(nonsep)
        target_from_top = max(0, min(n - 1, n - 1 - priority))
        dest_row = nonsep[target_from_top]
        if self._entries is nat:
            if dest_row != row:
                self.move_block([row],
                                dest_row if dest_row < row else dest_row + 1)
            return
        # Sorted display: splice the natural list, then re-derive.
        src = self._natural_row_of(e)
        if src < 0 or dest_row == src:
            return
        nat.pop(src)
        # Pre-removal target is dest_row (moving up: before it) or dest_row+1
        # (moving down: after it); the pop shifts the latter down by one, so
        # both cases land at dest_row post-pop.
        nat.insert(dest_row, e)
        self._rebuild_display()
        self.save()

    def add_separator(self, row: int, name: str, above: bool) -> None:
        from Utils.modlist import _SEPARATOR_SUFFIX
        from gui_qt.modlist_sort import insert_separator_display
        sep = ModEntry(name + _SEPARATOR_SUFFIX, True, False, True)
        ref = self._entries[row]
        if self.reverse_mode_active:
            # Inverted display reverses group/mod order, so a natural-order
            # insert mis-anchors — resolve in display space and uninvert
            # (Tk _add_separator_inverted).
            self._natural = insert_separator_display(self._entries, row,
                                                     above, sep)
            self._rebuild_display()
            self.save()
            return
        if self._entries is self._natural:
            at = row if above else row + 1
            self.beginInsertRows(QModelIndex(), at, at)
            self._entries.insert(at, sep)
            self.endInsertRows()
            self.save()
            return
        # Non-priority sort: insert next to the anchor entry in NATURAL order
        # (Tk operates on the natural list; the display re-sorts around it).
        ni = self._natural_row_of(ref)
        if ni < 0:
            return
        self._natural.insert(ni if above else ni + 1, sep)
        self._rebuild_display()
        self.save()

    def insert_mod(self, row: int, name: str, above: bool = False) -> None:
        """Insert an (enabled) mod entry named *name* relative to *row*. Used by
        'Create empty mod below' — the folder/meta.ini are created by the caller."""
        entry = ModEntry(name, True, False, False)
        if self._entries is self._natural:
            at = row if above else row + 1
            self.beginInsertRows(QModelIndex(), at, at)
            self._entries.insert(at, entry)
            self.endInsertRows()
            self.save()
            return
        ref = self._entries[row]
        ni = self._natural_row_of(ref)
        if ni < 0:
            return
        self._natural.insert(ni if above else ni + 1, entry)
        self._rebuild_display()
        self.save()

    def remove_row(self, row: int) -> None:
        e = self._entries[row]
        if e.name in _PINNED_NAMES or (not e.is_separator and e.locked):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._entries[row]
        if self._entries is not self._natural:
            ni = self._natural_row_of(e)
            if ni >= 0:
                del self._natural[ni]
        self.endRemoveRows()
        self.save()

    # ---- drag-reorder (beginMoveRows; selection/scroll preserved) ---------
    def supportedDropActions(self):
        return Qt.MoveAction

    def mimeTypes(self):
        return [_MIME]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes})
        md = QMimeData()
        md.setData(_MIME, QByteArray(",".join(map(str, rows)).encode()))
        return md

    def dropMimeData(self, data, action, row, col, parent):
        if action != Qt.MoveAction or not data.hasFormat(_MIME):
            return False
        src = [int(x) for x in bytes(data.data(_MIME)).decode().split(",")]
        dest = row if row != -1 else (parent.row() if parent.isValid()
                                      else len(self._entries))
        # A separator drag (lone line, or a LOCKED separator carrying its block)
        # must land at a block boundary, never inside another separator's mods.
        dragging_sep = len(src) == 1 and self._entries[src[0]].is_separator
        if dragging_sep and self.is_sep_locked(self._entries[src[0]].display_name):
            src = [src[0]] + list(self.sep_block_rows(src[0]))
        dest = self._resolve_drop_dest(dest, separator_drag=dragging_sep)
        return self.move_block(src, dest)

    def _resolve_drop_dest(self, dest: int, separator_drag: bool) -> int:
        """Tk drop rule: a separator (lone or carrying its block) lands just
        BEFORE the next separator/boundary below the drop point — i.e. at the
        END of whatever block it was dropped into — so it becomes a peer group
        and never splits another separator's mods. A plain mod drops as-is."""
        n = len(self._entries)
        dest = max(0, min(dest, n))
        if not separator_drag:
            return dest
        i = dest
        while i < n and not self._entries[i].is_separator:
            i += 1
        return i

    def move_block(self, src_rows: list[int], dest: int) -> bool:
        """Move a contiguous block of rows to *dest* using beginMoveRows so the
        view animates and keeps selection/scroll (unlike a full reset).

        Natural-order only: while a column sort is active the display is a
        permutation and row moves are meaningless here — the drag path clears
        a non-priority sort first, and reverse-priority drags go through
        move_block_display()."""
        if not src_rows or self._entries is not self._natural:
            return False
        src_rows = sorted(src_rows)
        first, last = src_rows[0], src_rows[-1]
        # Pinned rows never move: boundary separators (Overwrite/Root Folder)
        # and locked MODS. NB a regular separator reads back as locked=True
        # (the '-' prefix convention in read_modlist) yet is still movable — so
        # the lock guard applies to mods only, plus the boundary names.
        for r in src_rows:
            e = self._entries[r]
            if e.name in _PINNED_NAMES:
                return False
            if not e.is_separator and e.locked:
                return False
        # Keep within the movable span (below Overwrite, above Root Folder).
        lo, hi = self._movable_span()
        if first < lo or last >= hi or not (lo <= dest <= hi):
            return False
        # Mods may be dragged freely into or out of a locked separator's block
        # (locking now only means the separator carries its block when the
        # SEPARATOR itself is dragged — it no longer traps loose mods).
        # Qt's beginMoveRows requires dest outside the moved range.
        if first <= dest <= last + 1:
            return False
        if not self.beginMoveRows(QModelIndex(), first, last,
                                  QModelIndex(), dest):
            return False
        block = self._entries[first:last + 1]
        del self._entries[first:last + 1]
        insert_at = dest if dest < first else dest - len(block)
        self._entries[insert_at:insert_at] = block
        self.endMoveRows()
        self.save()
        return True

    def move_block_display(self, src_rows: list[int], slot: int,
                           hidden: set[int] | frozenset = frozenset()) -> bool:
        """Reverse-priority drag commit: move rows [first..last] of the DISPLAY
        list to drop *slot*, applying the Tk reverse-mode drop semantics
        (join-group #165 guard, top clamp, divider slot, full-block exemption),
        then re-derive the natural order via uninvert (Tk
        _uninvert_entries_order) and save."""
        from gui_qt.modlist_sort import resolve_reverse_drop
        if not src_rows or not self.reverse_mode_active:
            return False
        src_rows = sorted(src_rows)
        first, last = src_rows[0], src_rows[-1]
        for r in src_rows:
            e = self._entries[r]
            if e.name in _PINNED_NAMES:
                return False
            if not e.is_separator and e.locked:
                return False
        lo, hi = self._movable_span()
        if first < lo or last >= hi:
            return False
        # Full separator block = the separator plus exactly its own mods
        # moving as a unit (exempt from the join-group branch + top clamp).
        full_block = (self._entries[first].is_separator
                      and last in self.sep_block_rows(first)
                      and all(not self._entries[r].is_separator
                              for r in range(first + 1, last + 1)))
        ins = resolve_reverse_drop(self._entries, slot, set(src_rows),
                                   full_block, hidden=hidden)
        # Mods drag freely into/out of a locked separator's block (locking only
        # keeps the separator's block together when the SEPARATOR is dragged).
        ins = max(lo, min(ins, hi + 1))
        if first <= ins <= last + 1:
            return False
        if not self.beginMoveRows(QModelIndex(), first, last,
                                  QModelIndex(), ins):
            return False
        block = self._entries[first:last + 1]
        # The display list must become independent of _natural before the
        # splice (it IS a derived list in reverse mode, but guard anyway).
        if self._entries is self._natural:
            self._entries = list(self._natural)
        del self._entries[first:last + 1]
        at = ins if ins < first else ins - len(block)
        self._entries[at:at] = block
        self.endMoveRows()
        # Uninvert the mutated display into the new natural order, then
        # re-canonicalise the display (a drop below Overwrite belongs in the
        # float; everywhere else this is a no-op — invert∘uninvert identity).
        self._natural = uninvert_display(self._entries)
        self._sep_hl_cache.clear()
        self._rebuild_display()
        self.save()
        return True

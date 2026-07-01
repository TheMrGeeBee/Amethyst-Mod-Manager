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

# UI-only boundary separators: pinned + locked, never written to modlist.txt.
# Overwrite floats at the top, Root Folder at the bottom (Tk parity).
_BOUNDARY_NAMES = (OVERWRITE_NAME, ROOT_FOLDER_NAME)


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
        self._entries: list[ModEntry] = entries or []
        self._versions = versions or {}
        self._installed = installed or {}
        self._categories: dict[str, str] = {}
        # Formatted mod folder sizes ("12 MB"). Computed lazily — only when the
        # Size column is visible — so a default-hidden Size costs no disk walk.
        self._sizes: dict[str, str] = {}
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
        body = [e for e in entries if e.name not in _BOUNDARY_NAMES]
        top = ModEntry(OVERWRITE_NAME, True, True, True)
        bot = ModEntry(ROOT_FOLDER_NAME, True, True, True)
        return [top] + body + [bot]

    def set_entries(self, entries: list[ModEntry]) -> None:
        self.beginResetModel()
        self._entries = self._with_boundaries(entries)
        self._sep_hl_cache.clear()
        self.endResetModel()

    def set_sizes(self, sizes: dict[str, str]) -> None:
        """Set formatted mod sizes (Size column). Repaints just that column —
        used when the user enables Size from the column menu after first load."""
        self._sizes = sizes or {}
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_SIZE),
                                  self.index(len(self._entries) - 1, COL_SIZE),
                                  [Qt.DisplayRole])

    def set_flags(self, flags: dict[str, int]) -> None:
        self._flags = flags or {}
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_FLAGS),
                                  self.index(len(self._entries) - 1, COL_FLAGS),
                                  [FlagsRole, Qt.DisplayRole])

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

    def set_conflicts(self, conflicts: dict[str, int],
                      bsa_conflicts: dict[str, int] | None = None) -> None:
        self._conflicts = conflicts or {}
        if bsa_conflicts is not None:
            self._bsa_conflicts = bsa_conflicts or {}
        if self._entries:
            self.dataChanged.emit(self.index(0, COL_NAME),
                                  self.index(len(self._entries) - 1, COL_CONFLICTS),
                                  [ConflictRole, BsaConflictRole, Qt.DisplayRole])

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
            # The Name column hosts the column-menu button at its left edge;
            # pad the (left-aligned) label so it isn't drawn under the button.
            # Display-only — persistence keys off the canonical COLUMNS names.
            if section == COL_NAME:
                return "    " + COLUMNS[section]
            return COLUMNS[section]
        return None

    def _priority_for_row(self, row: int) -> int:
        """Descending priority number among non-separator rows (top = highest)."""
        # Count non-separator entries at-or-below this row.
        e = self._entries[row]
        if e.is_separator:
            return -1
        below = sum(1 for x in self._entries[row:] if not x.is_separator)
        return below - 1

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
            if e.is_separator:
                return 0
            from gui_qt.modlist_data import (
                FLAG_MODIFIED_MF, FLAG_PRERTX, FLAG_ROOT_RULE)
            bits = self._flags.get(e.name, 0)
            if e.name in self._modified_mf:
                bits |= FLAG_MODIFIED_MF
            if e.name in self._prertx_mods:
                bits |= FLAG_PRERTX
            if e.name in self._root_rule_mods:
                bits |= FLAG_ROOT_RULE
            return bits
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
        if not e.is_separator:
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
        if e.is_separator:
            name = e.display_name
            self._sep_locks[name] = not self._sep_locks.get(name, False)
        return self._sep_locks

    # ---- bulk separator / mod operations (footer buttons) -----------------
    def collapsible_separator_names(self) -> list[str]:
        """Display names of separators that can be collapsed (excludes the
        Overwrite / Root Folder boundaries)."""
        return [e.display_name for e in self._entries
                if e.is_separator and e.name not in _BOUNDARY_NAMES]

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

    def _locked_block_of(self, row: int) -> "range | None":
        """If *row* sits inside a locked separator's block, return that block's
        mod-row range, else None. (The separator row itself is not included.)

        *row* may be a real entry row or a drop-insert position. A position
        landing on a separator's own row is the boundary BEFORE that block —
        i.e. the END of the preceding block — not inside the locked one, so it
        scans from the entry just above it."""
        start = min(row, len(self._entries) - 1)
        if 0 <= start < len(self._entries) and self._entries[start].is_separator:
            start -= 1
        sep = None
        for i in range(start, -1, -1):
            if self._entries[i].is_separator:
                sep = i
                break
        if sep is None:
            return None
        if not self.is_sep_locked(self._entries[sep].display_name):
            return None
        return self.sep_block_rows(sep)

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
        body = [e for e in self._entries if e.name not in _BOUNDARY_NAMES]
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
        if e.name in _BOUNDARY_NAMES or (not e.is_separator and e.locked):
            return
        # Separators keep their suffix so they stay separators on write-out.
        from Utils.modlist import _SEPARATOR_SUFFIX
        e.name = (new_name + _SEPARATOR_SUFFIX) if e.is_separator else new_name
        idx = self.index(row, COL_NAME)
        self.dataChanged.emit(idx, idx, [Qt.DisplayRole, EntryRole])
        self.save()

    def set_priority(self, row: int, priority: int) -> None:
        """Move a mod so its descending-priority number becomes *priority*.
        Re-positions within the non-separator ordering (clamped)."""
        e = self._entries[row]
        if e.is_separator:
            return
        nonsep = [i for i, x in enumerate(self._entries) if not x.is_separator]
        n = len(nonsep)
        target_from_top = max(0, min(n - 1, n - 1 - priority))
        dest_row = nonsep[target_from_top]
        if dest_row != row:
            self.move_block([row], dest_row if dest_row < row else dest_row + 1)

    def add_separator(self, row: int, name: str, above: bool) -> None:
        from Utils.modlist import _SEPARATOR_SUFFIX
        at = row if above else row + 1
        sep = ModEntry(name + _SEPARATOR_SUFFIX, True, False, True)
        self.beginInsertRows(QModelIndex(), at, at)
        self._entries.insert(at, sep)
        self.endInsertRows()
        self.save()

    def insert_mod(self, row: int, name: str, above: bool = False) -> None:
        """Insert an (enabled) mod entry named *name* relative to *row*. Used by
        'Create empty mod below' — the folder/meta.ini are created by the caller."""
        at = row if above else row + 1
        entry = ModEntry(name, True, False, False)
        self.beginInsertRows(QModelIndex(), at, at)
        self._entries.insert(at, entry)
        self.endInsertRows()
        self.save()

    def remove_row(self, row: int) -> None:
        e = self._entries[row]
        if e.name in _BOUNDARY_NAMES or (not e.is_separator and e.locked):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._entries[row]
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
        view animates and keeps selection/scroll (unlike a full reset)."""
        if not src_rows:
            return False
        src_rows = sorted(src_rows)
        first, last = src_rows[0], src_rows[-1]
        # Pinned rows never move: boundary separators (Overwrite/Root Folder)
        # and locked MODS. NB a regular separator reads back as locked=True
        # (the '-' prefix convention in read_modlist) yet is still movable — so
        # the lock guard applies to mods only, plus the boundary names.
        for r in src_rows:
            e = self._entries[r]
            if e.name in _BOUNDARY_NAMES:
                return False
            if not e.is_separator and e.locked:
                return False
        # Keep within the movable span (below Overwrite, above Root Folder).
        lo, hi = self._movable_span()
        if first < lo or last >= hi or not (lo <= dest <= hi):
            return False
        # Block confinement applies only when moving MODS, not when relocating a
        # whole separator+block as a unit (a locked OR collapsed separator that
        # carries its mods — first row is the separator, the rest are its block).
        moving_sep_block = (
            self._entries[first].is_separator
            and last in self.sep_block_rows(first)
            and all(not self._entries[r].is_separator
                    for r in range(first + 1, last + 1)))
        if not moving_sep_block:
            src_block = self._locked_block_of(first)
            if src_block is not None:
                if (last not in src_block or dest < src_block.start
                        or dest > src_block.stop):
                    return False
            elif self._locked_block_of(dest) is not None:
                return False
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

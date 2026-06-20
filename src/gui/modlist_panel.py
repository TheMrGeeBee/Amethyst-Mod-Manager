"""
Mod list panel: canvas-based virtual list, toolbar, filters, Nexus update/endorsed.
Used by App. Imports theme, game_helpers, dialogs, install_mod.
"""

import json
import os
import shutil
import subprocess
import threading

from Utils.xdg import xdg_open, open_url
import tkinter as tk
import tkinter.font as tkfont
import tkinter.messagebox
import tkinter.ttk as ttk
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace

import customtkinter as ctk
from PIL import Image as PilImage, ImageTk

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_HOVER_ROW,
    BG_PANEL,
    BG_ROW,
    BG_ROW_ALT,
    BG_SELECT,
    BG_SEP,
    BG_OVERLAY_ERR,
    BG_ENTRY,
    BG_BTN_SAVE,
    BG_SELECT_BAR,
    BORDER,
    BORDER_FAINT,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_SEP,
    TEXT_WHITE,
    TONE_FLAG,
    TONE_BLUE_SOFT,
    TONE_GREEN,
    STATUS_ERR_BRIGHT,
    BG_DARK_GREEN,
    BG_DARK_BLUE,
    TEXT_TREE_FG,
    BTN_DANGER,
    BTN_DANGER_HOV,
    _ICONS_DIR,
    load_icon as _load_icon,
)
import gui.theme as _theme
from gui.theme import scaled
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.ctk_components import CTkAlert, CTkNotification, CTkPopupMenu, CTkProgressPopup
from gui.game_helpers import (
    _GAMES,
    _load_games,
    _profiles_for_game,
    _create_profile,
    _save_last_game,
    _load_last_game,
    _handle_missing_profile_root,
    _vanilla_plugins_for_game,
)
from gui.dialogs import (
    _RenameDialog,
    _SeparatorNameDialog,
    _ModNameDialog,
    _OverwritesDialog,
    _PriorityDialog,
    _DisablePluginsDialog,
    _ReplaceModDialog,
    ask_yes_no,
    show_error,
)
from gui.install_mod import install_mod_from_archive, _show_mod_notification
from gui.add_game_dialog import AddGameDialog, sync_modlist_with_mods_folder
from gui.modlist_filters_dialog import ModlistFiltersDialog
from gui.backup_restore_dialog import BackupRestoreDialog

from Utils.filemap import (
    build_filemap,
    read_mod_index,
    rebuild_mod_index,
    rescan_mods_in_index,
    remove_from_mod_index,
    rename_in_mod_index,
    fix_flat_staging_folders,
    CONFLICT_NONE,
    CONFLICT_WINS,
    CONFLICT_LOSES,
    CONFLICT_PARTIAL,
    CONFLICT_FULL,
    OVERWRITE_NAME,
    ROOT_FOLDER_NAME,
)
from Utils.bsa_filemap import (
    build_bsa_conflicts,
    rebuild_bsa_index,
    remove_from_bsa_index,
)
from Utils.deploy import deploy_root_folder, restore_root_folder, LinkMode, load_per_mod_strip_prefixes, undeploy_mod_files, restore_custom_deploy_backup_for_path
from Utils.modlist import (
    ModEntry,
    read_modlist,
    write_modlist,
    prepend_mod,
    ensure_mod_preserving_position,
)
from Utils.plugin_parser import check_missing_masters
from Utils.plugins import (
    read_plugins, write_plugins, PluginEntry,
    read_loadorder, write_loadorder,
)
from Utils.profile_backup import create_backup
from Utils.profile_state import (
    read_profile_state,
    read_collapsed_seps,
    read_separator_locks,
    read_separator_colors,
    read_separator_deploy_paths,
    read_root_folder_state,
    read_mod_strip_prefixes,
    read_disabled_plugins,
    read_excluded_mod_files,
    read_mod_notes,
    read_ignored_missing_requirements,
    write_collapsed_seps,
    write_separator_locks,
    write_separator_colors,
    write_separator_deploy_paths,
    write_root_folder_state,
    write_mod_strip_prefixes,
    write_disabled_plugins,
    write_excluded_mod_files,
    write_mod_notes,
    write_ignored_missing_requirements,
)
from Nexus.nexus_api import NexusAPI, NexusAPIError, NexusModRequirement
from gui.collections_dialog import CollectionsDialog
from gui.workshop_dialog import WorkshopDialog
from gui.nexus_browser_overlay import NexusBrowserOverlay
from gui.changelog_overlay import ChangelogOverlay
from Nexus.nexus_meta import ensure_installed_stamp, read_meta, write_meta
from Utils.config_paths import get_download_cache_dir, list_all_cache_dirs
from Utils.ui_config import load_column_widths, load_column_order, load_normalize_folder_case, load_sort_state, save_sort_state, load_column_hidden, load_show_summary_tooltips, load_hide_bsa_conflicts


from gui.text_utils import truncate_text as _truncate_text_for_width, clear_truncate_cache as _clear_truncate_cache
from gui.tk_tooltip import TkTooltip


# Name of the transient, non-interactive "ungrouped float" boundary row that is
# inserted into _entries during a reverse-mode (priority-ascending) drag.  It
# separates the high-priority mods that belong to no user separator (floated to
# the bottom, just above Overwrite) from the last real user group, so dragging
# across it is unambiguous.  Never saved to modlist.txt — stripped on uninvert.
BOUNDARY_NAME = "__Ungrouped_Boundary__"

# Sentinel "entry index" used in _visible_indices for the static (non-drag)
# ungrouped-float divider.  It is NOT a valid index into _entries — _redraw
# resolves it to self._static_boundary and every other vis[row] consumer treats
# it as non-interactive (skips selection / hover / drag).
BOUNDARY_ROW = -2


def _copy_fomod_choice(src_profile_dir: Path, dst_profile_dir: Path, mod_name: str) -> None:
    """Copy a mod's saved installer choice JSON (FOMOD or BAIN) from one profile
    to another, if present."""
    for sub in ("fomod", "bain"):
        src = src_profile_dir / sub / f"{mod_name}.json"
        if not src.is_file():
            continue
        dst_dir = dst_profile_dir / sub
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst_dir / f"{mod_name}.json"))


def _dir_size_bytes(path: Path) -> int:
    """Recursively sum file sizes under path (bytes). Safe to run in a thread."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        total += _dir_size_bytes(Path(entry.path))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _format_size(num_bytes: int) -> str:
    """Format a byte count as a short KB/MB/GB string."""
    if num_bytes <= 0:
        return ""
    kb = num_bytes / 1024
    if kb < 1024:
        return f"{kb:.0f} KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


def _scan_meta_flags_impl(entries: list, mods_dir: Path, compute_sizes: bool = False) -> dict:
    """Pure scan over meta.ini; returns dict of results. Safe to run in thread.

    Folder sizes are only walked when compute_sizes is set (Size column visible),
    since walking every mod tree is expensive and fires on many GUI events."""
    update_mods: set[str] = set()
    missing_reqs: set[str] = set()
    missing_reqs_detail: dict[str, list[str]] = {}
    endorsed_mods: set[str] = set()
    install_dates: dict[str, str] = {}
    install_datetimes: dict[str, datetime] = {}
    category_names: dict[str, str] = {}
    mod_versions: dict[str, str] = {}
    mod_sizes: dict[str, str] = {}
    mod_size_bytes: dict[str, int] = {}
    fomod_mods: set[str] = set()
    bain_mods: set[str] = set()
    root_folder_mods: set[str] = set()
    collection_bundled_mods: set[str] = set()
    collection_patched_mods: set[str] = set()
    xedit_modified_mods: dict[str, list[str]] = {}
    bundle_mods: set[str] = set()  # RE/Fluffy single-mod bundles (have a [Bundle] spec)
    from Utils.re_bundle import read_bundle_spec as _read_bundle_spec
    today = datetime.now().date()
    for entry in entries:
        if entry.is_separator:
            continue
        mod_dir = mods_dir / entry.name
        if compute_sizes:
            size_bytes = _dir_size_bytes(mod_dir)
            if size_bytes > 0:
                mod_size_bytes[entry.name] = size_bytes
                mod_sizes[entry.name] = _format_size(size_bytes)
        meta_path = mod_dir / "meta.ini"
        if not meta_path.is_file():
            continue
        try:
            meta = read_meta(meta_path)
            if not meta.installed and ensure_installed_stamp(meta_path):
                meta = read_meta(meta_path)
            if meta.has_update and not meta.ignore_update:
                update_mods.add(entry.name)
            if meta.missing_requirements:
                missing_reqs.add(entry.name)
                names = []
                for pair in meta.missing_requirements.split(";"):
                    parts = pair.split(":", 1)
                    if len(parts) == 2:
                        names.append(parts[1])
                    elif parts[0]:
                        names.append(parts[0])
                missing_reqs_detail[entry.name] = names
            if meta.endorsed:
                endorsed_mods.add(entry.name)
            if meta.installed:
                dt = datetime.fromisoformat(meta.installed)
                if dt.date() == today:
                    install_dates[entry.name] = dt.strftime("%-I:%M %p")
                else:
                    install_dates[entry.name] = dt.strftime("%-m/%-d/%y")
                install_datetimes[entry.name] = dt
            if meta.category_name:
                category_names[entry.name] = meta.category_name
            if meta.version:
                mod_versions[entry.name] = meta.version
            if meta.is_fomod:
                fomod_mods.add(entry.name)
            if meta.is_bain:
                bain_mods.add(entry.name)
            if meta.root_folder:
                root_folder_mods.add(entry.name)
            if meta.from_collection_bundled:
                collection_bundled_mods.add(entry.name)
            if meta.from_collection_patched:
                collection_patched_mods.add(entry.name)
            if meta.xedit_modified_plugins:
                plugins = [p.strip() for p in meta.xedit_modified_plugins.split(";")
                           if p.strip()]
                if plugins:
                    xedit_modified_mods[entry.name] = plugins
            if _read_bundle_spec(meta_path) is not None:
                bundle_mods.add(entry.name)
        except Exception:
            pass
    return {
        "update_mods": update_mods,
        "missing_reqs": missing_reqs,
        "missing_reqs_detail": missing_reqs_detail,
        "endorsed_mods": endorsed_mods,
        "install_dates": install_dates,
        "install_datetimes": install_datetimes,
        "category_names": category_names,
        "mod_versions": mod_versions,
        "mod_sizes": mod_sizes,
        "mod_size_bytes": mod_size_bytes,
        "fomod_mods": fomod_mods,
        "bain_mods": bain_mods,
        "root_folder_mods": root_folder_mods,
        "collection_bundled_mods": collection_bundled_mods,
        "collection_patched_mods": collection_patched_mods,
        "xedit_modified_mods": xedit_modified_mods,
        "bundle_mods": bundle_mods,
    }


# ---------------------------------------------------------------------------
# ModListPanel
# ---------------------------------------------------------------------------
from gui.modlist_filter_panel import ModListFilterPanelMixin
from gui.modlist_download_bar import ModListDownloadBarMixin
from gui.modlist_header import ModListHeaderColumnsMixin
from gui.modlist_nexus_actions import ModListNexusActionsMixin


class ModListPanel(ModListFilterPanelMixin, ModListDownloadBarMixin,
                   ModListHeaderColumnsMixin, ModListNexusActionsMixin,
                   ctk.CTkFrame):
    """
    Left panel: column header, canvas-based mod list, toolbar.

    Rows are drawn as canvas items rather than individual CTk widgets.
    One tk.Checkbutton per visible row is embedded via canvas.create_window
    (not place), so it composites properly with the canvas and avoids opaque
    Checkbutton backgrounds.  All other columns are drawn as canvas text items.
    This gives smooth scrolling and instant load for large mod lists.
    """

    ROW_H   = scaled(30)
    HEADERS = ["", "Mod Name", "Flags", "Conflicts", "Installed", "Priority"]
    # x-start of each logical column (checkbox, name, flags, conflicts, installed, priority)
    # Computed dynamically in _layout_columns(); defaults here.
    _COL_X  = [4, 32, 0, 0, 0, 0, 0, 0]   # patched in _layout_columns

    def __init__(self, parent, log_fn=None, call_threadsafe_fn=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._log = log_fn or (lambda msg: None)
        self._call_threadsafe = call_threadsafe_fn

        self._game = None
        self.__profile_state: dict = {}   # cached profile_state.json for current profile load
        self._entries:  list[ModEntry] = []
        self._sel_idx:  int = -1          # anchor of the current selection
        self._sel_set:  set[int] = set()  # all selected entry indices
        self._hover_idx: int = -1         # entry index under the mouse cursor
        self._highlighted_mod: str | None = None  # mod highlighted by plugin panel selection
        self._modlist_path: Path | None = None
        self._staging_root: Path | None = None
        self._filemap_path: Path | None = None
        self._strip_prefixes:    set[str] = set()
        self._mod_strip_prefixes: dict[str, list[str]] = {}  # mod name -> top-level folders to ignore
        self._install_extensions: set[str] = set()
        self._root_deploy_folders: set[str] = set()
        self._staging_requires_subdir: bool = False
        self._staging_wrap_signals: tuple[set[str], set[str]] = ({"manifest.json"}, set())
        self._staging_already_structured: set[str] = set()
        self._normalize_folder_case: bool = True
        self._filemap_casing: str = "upper"
        self._filemap_exclude_dirs: frozenset[str] = frozenset({"fomod"})
        self._root_folder_enabled: bool = True
        self._conflict_map:  dict[str, int]      = {}  # mod_name → CONFLICT_* constant

        # Conflict icons (canvas-compatible PhotoImage)
        self._icon_plus: ImageTk.PhotoImage | None = None
        self._icon_minus: ImageTk.PhotoImage | None = None
        self._icon_cross: ImageTk.PhotoImage | None = None
        self._icon_conflict_mixed: ImageTk.PhotoImage | None = None
        self._icon_bsa_winner: ImageTk.PhotoImage | None = None
        self._icon_bsa_loser: ImageTk.PhotoImage | None = None
        self._icon_bsa_mixed: ImageTk.PhotoImage | None = None
        self._icon_bsa_redundant: ImageTk.PhotoImage | None = None
        _icon_sz = scaled(18)
        _lock_icon_sz = scaled(14)
        _conflict_icon_sz = scaled(24)

        def _load_conflict(filename: str) -> ImageTk.PhotoImage | None:
            p = _ICONS_DIR / filename
            if not p.is_file():
                return None
            return ImageTk.PhotoImage(
                PilImage.open(p).convert("RGBA").resize((_conflict_icon_sz, _conflict_icon_sz), PilImage.LANCZOS))

        self._icon_plus  = _load_conflict("conflict-winner.png") or _load_conflict("plus.png")
        self._icon_minus = _load_conflict("conflict-loser.png")  or _load_conflict("minus.png")
        self._icon_cross = _load_conflict("conflict-redundant.png") or _load_conflict("cross.png")
        self._icon_conflict_mixed = _load_conflict("conflict-mixed.png")
        self._icon_bsa_winner    = _load_conflict("archive-conflict-winner.png")
        self._icon_bsa_loser     = _load_conflict("archive-conflict-loser.png")
        self._icon_bsa_mixed     = _load_conflict("archive-conflict-mixed.png")
        self._icon_bsa_redundant = _load_conflict("archive-conflict-redundant.png")

        # Update-available icon
        self._icon_update: ImageTk.PhotoImage | None = None
        _update_path = _ICONS_DIR / "update.png"
        if _update_path.is_file():
            self._icon_update = ImageTk.PhotoImage(
                PilImage.open(_update_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Missing-requirements warning icon
        self._icon_warning: ImageTk.PhotoImage | None = None
        _warning_path = _ICONS_DIR / "warning.png"
        if _warning_path.is_file():
            self._icon_warning = ImageTk.PhotoImage(
                PilImage.open(_warning_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Pre-RTX mod info icon
        self._icon_info: ImageTk.PhotoImage | None = None
        _info_path = _ICONS_DIR / "info.png"
        if _info_path.is_file():
            self._icon_info = ImageTk.PhotoImage(
                PilImage.open(_info_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Endorsed mod tick icon
        self._icon_endorsed: ImageTk.PhotoImage | None = None
        _tick_path = _ICONS_DIR / "endorsed.png"
        if _tick_path.is_file():
            self._icon_endorsed = ImageTk.PhotoImage(
                PilImage.open(_tick_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Disabled files icon
        self._icon_disabled_files: ImageTk.PhotoImage | None = None
        _eye2_path = _ICONS_DIR / "eye2_white.png"
        if _eye2_path.is_file():
            self._icon_disabled_files = ImageTk.PhotoImage(
                PilImage.open(_eye2_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        self._icon_root_folder: ImageTk.PhotoImage | None = None
        _root_path = _ICONS_DIR / "root.png"
        if _root_path.is_file():
            self._icon_root_folder = ImageTk.PhotoImage(
                PilImage.open(_root_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Mod-note flag icon — shown when a mod has a note saved in profile_state
        self._icon_note: ImageTk.PhotoImage | None = None
        _note_path = _ICONS_DIR / "note.png"
        if _note_path.is_file():
            self._icon_note = ImageTk.PhotoImage(
                PilImage.open(_note_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Bundle settings flag — shown on RE/Fluffy single-mod bundles; clicking
        # it opens the Bundle Options dialog.
        self._icon_settings: ImageTk.PhotoImage | None = None
        _settings_path = _ICONS_DIR / "settings.png"
        if _settings_path.is_file():
            self._icon_settings = ImageTk.PhotoImage(
                PilImage.open(_settings_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # xEdit-modified plugin flag — shown when a mod contains a plugin that
        # was edited in xEdit and moved back to staging on restore.
        self._icon_xedit: ImageTk.PhotoImage | None = None
        _brush_path = _ICONS_DIR / "brush.png"
        if _brush_path.is_file():
            self._icon_xedit = ImageTk.PhotoImage(
                PilImage.open(_brush_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Separator collapse/expand arrows (right = collapsed, arrow = expanded)
        self._icon_sep_right: ImageTk.PhotoImage | None = None
        self._icon_sep_arrow: ImageTk.PhotoImage | None = None
        _right_path = _ICONS_DIR / "right.png"
        _arrow_path = _ICONS_DIR / "arrow.png"
        if _right_path.is_file():
            self._icon_sep_right = ImageTk.PhotoImage(
                PilImage.open(_right_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))
        if _arrow_path.is_file():
            self._icon_sep_arrow = ImageTk.PhotoImage(
                PilImage.open(_arrow_path).convert("RGBA").resize((_icon_sz, _icon_sz), PilImage.LANCZOS))

        # Separator lock icon
        self._icon_lock: ImageTk.PhotoImage | None = None
        _lock_path = _ICONS_DIR / "lock.png"
        if _lock_path.is_file():
            self._icon_lock = ImageTk.PhotoImage(
                PilImage.open(_lock_path).convert("RGBA").resize((_lock_icon_sz, _lock_icon_sz), PilImage.LANCZOS))

        # Set of mod names that have a Nexus update available
        self._update_mods: set[str] = set()

        # Lazy cache of mod_name → description (Nexus summary), loaded from meta.ini
        # on first hover. Cleared whenever update info is recomputed (since the
        # update checker is what backfills descriptions).
        self._description_cache: dict[str, str] = {}
        # (entry idx, [(icon center x, tooltip text)]) for the last hovered flags row
        self._flag_tip_cache: tuple[int, list[tuple[int, str]]] | None = None
        self._show_summary_tooltips: bool = load_show_summary_tooltips()

        # Set of mod names that have missing Nexus requirements
        self._missing_reqs: set[str] = set()
        # Map mod name → list of missing requirement names (for tooltips / context menu)
        self._missing_reqs_detail: dict[str, list[str]] = {}
        # Mod names for which the user chose "Ignore requirements" (flag hidden, per profile)
        self._ignored_missing_reqs: set[str] = set()

        # Tooltip state (missing requirements hover)
        self._tooltip = TkTooltip(
            self,
            bg=BG_OVERLAY_ERR,
            fg=STATUS_ERR_BRIGHT,
            font=(_theme.FONT_FAMILY, _theme.FS10),
        )

        # Set of mod names the user has endorsed on Nexus
        self._endorsed_mods: set[str] = set()

        # Set of mod names that contain pre-RTX (natives/x64) files
        self._prertx_mods: set[str] = set()

        # Map mod name → install date display string
        self._install_dates: dict[str, str] = {}
        self._category_names: dict[str, str] = {}
        self._mod_versions: dict[str, str] = {}
        self._mod_sizes: dict[str, str] = {}
        self._mod_size_bytes: dict[str, int] = {}
        self._fomod_mods: set[str] = set()
        self._bain_mods: set[str] = set()
        # Set of mod names flagged for root-level (engine) deployment
        self._root_folder_mods: set[str] = set()
        # Set of mod names that own at least one file matched by a custom
        # routing rule with dest="" (i.e. files that deploy to the game root).
        # Auto-detected from the mod index + game's custom_routing_rules.
        self._root_rule_mods: set[str] = set()
        # Sets of mod names tagged by collection install — populated from
        # meta.ini's fromCollectionBundled / fromCollectionPatched flags.
        self._collection_bundled_mods: set[str] = set()
        self._collection_patched_mods: set[str] = set()
        # RE/Fluffy single-mod bundles (carry a [Bundle] spec in meta.ini) — get a
        # clickable settings flag that opens the Bundle Options dialog.
        self._bundle_mods: set[str] = set()
        # Map mod name → list of plugin names edited in xEdit (from meta.ini's
        # xeditModifiedPlugins). Drives the brush flag in the Flags column.
        self._xedit_modified_mods: dict[str, list[str]] = {}
        # Map mod name → install datetime for sorting (parallel to _install_dates)
        self._install_datetimes: dict[str, datetime] = {}

        self._overrides:     dict[str, set[str]] = {}  # mod beats these mods
        self._overridden_by: dict[str, set[str]] = {}  # these mods beat this mod
        # Loose-only conflict state — kept un-merged so the BSA-only recompute
        # path (triggered on plugin reorder) can re-fold loose↔BSA relationships
        # without redoing the full filemap scan.
        self._conflict_map_base:  dict[str, int]      = {}
        self._overrides_base:     dict[str, set[str]] = {}
        self._overridden_by_base: dict[str, set[str]] = {}
        # BSA-vs-BSA conflicts (separate pipeline, Bethesda games only)
        self._bsa_conflict_map:  dict[str, int]      = {}
        self._bsa_overrides:     dict[str, set[str]] = {}
        self._bsa_overridden_by: dict[str, set[str]] = {}
        # When True, skip BSA conflict parsing entirely and hide the flags.
        self._hide_bsa_conflicts: bool = load_hide_bsa_conflicts()
        self._on_filemap_rebuilt: callable | None = None  # called after each filemap rebuild
        self._on_mod_selected_cb: callable | None = None  # called when a mod is selected
        self._filemap_pending: bool = False   # True while a background rebuild is running
        self._filemap_dirty:   bool = False   # True if another rebuild was requested while one was running
        self._filemap_after_id: str | None = None  # after() handle for debounce timer
        self._filemap_rescan_index: bool = False  # True if next rebuild should regenerate modindex.bin first
        self._redraw_after_id: str | None = None  # after_idle handle for scroll-debounce
        self._canvas_resize_after_id: str | None = None  # after() handle for resize-debounce
        self._marker_strip_after_id: str | None = None   # after() handle for marker-strip debounce

        # Drag state
        self._drag_idx:      int = -1      # entry index being dragged (updated in real-time)
        self._drag_origin_idx: int = -1    # original index when drag began
        self._drag_start_y:  int = 0
        self._drag_moved:    bool = False
        self._drag_is_block: bool = False   # True when dragging a separator+its mods
        self._drag_block:    list  = []     # snapshot of (entry, var) at mousedown
        self._drag_sel_indices: list[int] = []  # actual entry indices for sparse multi-select drag
        self._drag_slot:     int  = -1     # last computed insertion slot (in vis-without-drag space)
        self._drag_start_slot: int = 0     # slot when drag began (for delta-based movement)

        self._drag_entries_snapshot: list | None = None  # snapshot for column-sort drag
        self._drag_vars_snapshot: list | None = None
        self._drag_reordered_snapshot: list | None = None  # reordered state for inverted drag
        self._drag_reordered_vars: list | None = None
        self._drag_saved_sort_column: str | None = None
        self._drag_saved_sort_ascending: bool | None = None
        # Reverse-mode "ungrouped float": during a reverse-mode drag a transient,
        # non-interactive boundary row (BOUNDARY_NAME) is inserted into _entries
        # between the last user group and the high-priority mods that belong to no
        # user separator, so those mods can sit at the bottom (just above
        # Overwrite) while staying distinguishable from the last group's members.
        # The boundary is stripped before saving.  See _activate_drag /
        # _uninvert_entries_order.
        self._drag_has_boundary: bool = False
        # In the STATIC (non-drag) reverse-priority view the same divider is shown
        # as a render-only synthetic row so the user can see where the last user
        # separator's mods end and the ungrouped float begins.  It is injected
        # into _visible_indices as the sentinel index BOUNDARY_ROW and resolved to
        # this standalone ModEntry in _redraw; it is never part of _entries, so it
        # never affects counts, saving, or the natural order.
        self._static_boundary = ModEntry(
            name=BOUNDARY_NAME, enabled=True, locked=True, is_separator=True,
        )
        self._static_boundary_visible: bool = False

        self._drag_pending:  bool = False  # waiting for click-vs-drag disambiguation
        self._drag_after_id: str | None = None  # after() id for drag-start timer
        # Deferred-activation for clicking a highlighted mod inside a multi-
        # selection: we must NOT start the drag (which physically inverts
        # _entries in reverse mode) until the pointer actually moves, otherwise a
        # plain click leaves the list re-sorted.  Holds (idx, cy) until the first
        # real motion promotes it to a live drag, or release treats it as a click.
        self._pending_multidrag: tuple[int, int] | None = None
        # True when the pending drag is a locked separator's whole block — the
        # promotion in _on_mouse_drag rebuilds the block from _sep_block_range
        # and activates with is_block=True.  Cleared whenever _pending_multidrag
        # is cleared.
        self._pending_sep_block: bool = False
        self._drag_scroll_after: str | None = None  # after() id for auto-scroll repeat
        self._drag_last_event_y: int = 0  # last widget-space Y from mouse drag

        # Locked-separator drag confinement: when a mod inside a locked block is
        # dragged, _drag_lock_sep_name names that separator so each drag tick can
        # re-resolve the block range in the (possibly mutated) _entries list.
        # _drag_lock_bypass=True (Shift held at press) disables the confinement.
        self._drag_lock_sep_name: str | None = None
        self._drag_lock_bypass: bool = False

        # Separator lock state: sep_name → bool (True = locked, block drag disabled)
        self._sep_locks: dict[str, bool] = {}

        # Separator custom background colors: sep_name → hex color string
        self._sep_colors: dict[str, str] = {}

        # Separator custom deployment directories: sep_name → path string (empty = default)
        self._sep_deploy_paths: dict[str, dict] = {}  # {sep_name: {"path": str, "raw": bool}}

        # Separator settings overlay widget (in-panel frame placed over the list)
        self._sep_settings_overlay: tk.Frame | None = None

        # Collapsed separators: set of sep names whose mods are hidden
        self._collapsed_seps: set[str] = set()

        # Legacy bundle-group map (always empty now — bundles are plain mods).
        self._bundle_groups: dict[str, list[int]] = {}

        # Search/filter
        self._filter_text: str = ""
        # Tri-state filter flags: 0 = off, 1 = include (show only matches),
        # 2 = exclude (hide matches). Truthy == active either way.
        self._filter_show_disabled: int = 0
        self._filter_show_enabled: int = 0
        self._filter_hide_separators: int = 0
        self._filter_conflict_winning: int = 0
        self._filter_conflict_losing: int = 0
        self._filter_conflict_partial: int = 0
        self._filter_conflict_full: int = 0
        self._filter_missing_reqs: int = 0
        self._filter_has_disabled_plugins: int = 0
        self._filter_has_plugins: int = 0
        self._filter_has_disabled_files: int = 0
        self._filter_has_updates: int = 0
        self._filter_fomod_only: int = 0
        self._filter_bain_only: int = 0
        self._filter_has_bsa: int = 0
        self._filter_has_pbr: int = 0
        self._filter_categories: frozenset[str] = frozenset()         # include-only categories
        self._filter_categories_exclude: frozenset[str] = frozenset() # categories to hide
        self._filter_filetypes: frozenset[str] = frozenset()
        self._filter_filetypes_exclude: frozenset[str] = frozenset()
        self._filter_has_notes: int = 0
        self._disabled_plugins_map: dict[str, list[str]] = {}  # mod_name → [plugin, ...]
        self._excluded_mod_files_map: dict[str, list[str]] = {}  # mod_name → [rel_key, ...]
        self._mod_notes_map: dict[str, str] = {}  # mod_name → note text (profile_state.json)
        self._visible_indices: list[int] = []  # entry indices matching current filter
        self._vis_dirty: bool = True           # True when _visible_indices needs recomputing
        self._priorities: dict[int, int] = {}  # entry index → priority number (cached)
        self._sep_block_cache: dict[int, range] = {}  # sep_idx → range (cached)
        self._mod_to_sep_idx: dict[str, int] | None = None  # mod_name → owning sep idx; None = stale

        # Column sorting (visual only — never touches modlist.txt)
        # _sort_column: None or one of "name", "installed", "flags", "conflicts", "priority"
        self._sort_column, self._sort_ascending = load_sort_state()

        # Column resize overrides: col index (1–6) → width in px
        # When set, _layout_columns uses this instead of auto-calculated width.
        self._col_w_override: dict[int, int] = load_column_widths()
        self._col_drag_col: int | None = None   # which col boundary is being dragged
        self._col_drag_start_x: int = 0
        self._col_drag_start_w: int = 0

        # Column reorder: list of data col indices (2-5) in left→right display order
        self._col_order: list[int] = load_column_order()
        # Hidden columns: set of data col indices (2..7). Col 1 (name) is never hideable.
        self._col_hidden: set[int] = load_column_hidden()
        # _col_pos: data col index → _COL_X/W slot index (computed in _layout_columns)
        self._col_pos: dict[int, int] = {2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
        # Header label drag-to-reorder state
        self._hdr_drag_col: int | None = None   # data col index being dragged
        self._hdr_drag_start_x: int = 0
        self._hdr_drag_moved: bool = False
        self._hdr_drag_ghost: tk.Label | None = None  # floating ghost label

        # Per-entry logical state (parallel to _entries, aligned by index)
        self._check_vars:    list[tk.BooleanVar | None] = []

        # Lock canvas items for separator rows: sep_name → canvas item id
        # Pure canvas rect + checkmark — scroll in sync automatically.
        self._lock_cb_rects: dict[str, int] = {}
        self._lock_cb_marks: dict[str, int] = {}

        # Virtual-list pool — pre-allocated canvas items + widgets for visible rows.
        # Only _pool_size slots are ever created; they are reconfigured on every draw.
        self._pool_size: int = 60
        self._pool_data_idx: list[int] = []          # slot → entry index (-1 = unused)
        self._pool_last_state: list = []             # slot → last rendered state key (None = dirty)
        self._pool_bg: list[int] = []                # rectangle canvas item ids
        self._pool_name: list[int] = []              # text canvas item ids (mod name / sep label)
        self._pool_flag_icon: list[int] = []         # image canvas item ids (flags column, slot 1)
        self._pool_flag_icon2: list[int] = []        # image canvas item ids (flags column, slot 2)
        self._pool_flag_icon3: list[int] = []        # image canvas item ids (flags column, slot 3)
        self._pool_flag_icon4: list[int] = []        # image canvas item ids (flags column, slot 4)
        self._pool_flag_icon5: list[int] = []        # image canvas item ids (flags column, slot 5)
        self._pool_flag_star: list[int] = []         # text canvas item ids (lock star in flags column)
        self._pool_conflict_icon1: list[int] = []    # image canvas item ids (conflict col left)
        self._pool_conflict_icon2: list[int] = []    # image canvas item ids (conflict col right)
        self._pool_bsa_dot1: list[int] = []          # image canvas item ids (BSA conflict icon, primary)
        self._pool_bsa_dot2: list[int] = []          # image canvas item ids (BSA conflict icon, secondary, unused)
        self._pool_bsa_sep:  list[int] = []          # line canvas item ids (small separator between loose icons and BSA dots)
        self._pool_category_text: list[int] = []     # text canvas item ids (category)
        self._pool_install_text: list[int] = []      # text canvas item ids (install date)
        self._pool_priority_text: list[int] = []     # text canvas item ids (priority)
        self._pool_version_text: list[int] = []      # text canvas item ids (version)
        self._pool_size_text: list[int] = []         # text canvas item ids (size)
        self._pool_sep_icon: list[int] = []          # image canvas item ids (collapse arrow)
        self._pool_sep_line_l: list[int] = []        # line canvas item ids (separator left line)
        self._pool_sep_line_r: list[int] = []        # line canvas item ids (separator right line)
        self._pool_sep_badge: list[int] = []         # text canvas item ids (custom deploy badge)
        self._pool_check_vars: list[tk.BooleanVar] = []
        self._pool_cb_rect: list[int] = []   # canvas rectangle ids for checkbox outline
        self._pool_cb_mark: list[int] = []   # canvas text ids for checkmark
        # Marker-strip lookup maps, rebuilt only when _visible_indices is replaced
        self._marker_maps_vis: list[int] | None = None
        self._marker_ei_to_row: dict[int, int] = {}
        self._marker_name_to_row: dict[str, int] = {}
        # Pool canvas_w cached for pool creation after canvas exists
        self._canvas_w: int = 600
        self._context_menu: CTkPopupMenu | None = None

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=0, minsize=0)  # filter side panel
        self.grid_columnconfigure(1, weight=1)              # main content

        self._build_new_profile_bar()
        self._build_header()
        self._build_canvas()
        self._build_toolbar()
        self._build_search_bar()
        self._build_download_bar()
        self._build_filter_side_panel()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_game(self, game, profile: str = "default") -> None:
        # Hide any open new-profile bar when switching game/profile
        if hasattr(self, "_new_profile_bar"):
            self.hide_new_profile_bar()
        _collections_was_open = getattr(self, "_collections_panel", None) is not None
        if game is None:
            self._game = None
            self._modlist_path = None
            self._ignored_missing_reqs = set()
            self._reload()
            self._close_collections()
            if hasattr(self, "_restore_backup_btn"):
                self._restore_backup_btn.configure(state="disabled")
            return
        self._game = game
        profile_dir = game.get_profile_root() / "profiles" / profile
        self._modlist_path = profile_dir / "modlist.txt"
        self._staging_root = game.get_effective_mod_staging_path()
        self._filemap_path = self._staging_root.parent / "filemap.txt"
        self._strip_prefixes    = game.mod_folder_strip_prefixes | getattr(game, "mod_folder_strip_prefixes_post", set())
        self._install_extensions = getattr(game, "mod_install_extensions", set())
        self._root_deploy_folders = getattr(game, "mod_root_deploy_folders", set())
        self._staging_requires_subdir = getattr(game, "mod_staging_requires_subdir", False)
        self._staging_wrap_signals = getattr(game, "mod_staging_wrap_signals", ({"manifest.json"}, set()))
        self._staging_already_structured = getattr(game, "mod_staging_already_structured_markers", set())
        self._normalize_folder_case = getattr(game, "normalize_folder_case", True) and load_normalize_folder_case()
        self._filemap_casing = str(getattr(game, "filemap_casing", "upper"))
        self._conflict_ignore_filenames = getattr(game, "conflict_ignore_filenames", set())
        self._excluded_loose_filenames = getattr(game, "excluded_loose_filenames", set())
        self._allowed_top_level_folders = (
            set(getattr(game, "mod_required_top_level_folders", set()))
            if getattr(game, "filemap_exclude_unknown_top_level", False)
            else set()
        )
        self._filemap_exclude_dirs = getattr(game, "filemap_exclude_dirs", frozenset({"fomod"}))
        # Load profile_state.json once; individual loaders pull from it
        self.__profile_state = read_profile_state(profile_dir)
        self._ignored_missing_reqs = read_ignored_missing_requirements(profile_dir, self.__profile_state)
        self._reload()
        if hasattr(self, "_restore_backup_btn"):
            self._restore_backup_btn.configure(state="normal")
        # If the collections panel was open, re-open it for the new game
        if _collections_was_open:
            self._on_collections()

    def reload_after_install(self):
        self._reload()

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_new_profile_bar(self):
        """Inline bar (row 0) shown when the user clicks '+' to create a profile."""
        bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        bar.grid(row=0, column=1, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_remove()  # hidden by default
        self._new_profile_bar = bar

        ctk.CTkLabel(
            bar, text="New profile:", font=_theme.FONT_NORMAL,
            text_color=TEXT_MAIN,
        ).pack(side="left", padx=(8, 4), pady=6)

        self._new_profile_var = tk.StringVar()
        self._new_profile_entry = ctk.CTkEntry(
            bar, textvariable=self._new_profile_var, font=_theme.FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER,
            width=180, height=26,
        )
        self._new_profile_entry.pack(side="left", padx=(0, 8), pady=6)
        self._new_profile_entry.bind("<Return>", lambda _e: self._on_new_profile_create())
        self._new_profile_entry.bind("<Escape>", lambda _e: self.hide_new_profile_bar())

        self._new_profile_specific_mods_var = tk.BooleanVar(value=False)
        _specific_cb = ctk.CTkCheckBox(
            bar,
            text="Use Profile Specific Mods",
            variable=self._new_profile_specific_mods_var,
            font=_theme.FONT_NORMAL,
            text_color=TEXT_MAIN,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            border_color=BORDER,
            checkmark_color="white",
            width=22, height=22,
        )
        _specific_cb.pack(side="left", padx=(0, 12), pady=6)
        self._tooltip.attach(
            _specific_cb,
            "Profiles with this setting use their own mods folders",
            offset_x=scaled(12), offset_y=scaled(12),
        )

        ctk.CTkButton(
            bar, text="Create", width=72, height=26, font=_theme.FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_new_profile_create,
        ).pack(side="left", padx=(0, 4), pady=6)

        ctk.CTkButton(
            bar, text="Cancel", width=72, height=26, font=_theme.FONT_NORMAL,
            fg_color=BG_HOVER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self.hide_new_profile_bar,
        ).pack(side="left", padx=(0, 8), pady=6)

    # ------------------------------------------------------------------
    # New-profile bar public API
    # ------------------------------------------------------------------

    def show_new_profile_bar(self, on_create_fn):
        """Reveal the inline new-profile bar and focus the name entry.

        *on_create_fn(name: str, profile_specific_mods: bool)* is called when
        the user confirms; it can do validation and profile creation.
        """
        self._new_profile_create_fn = on_create_fn
        self._new_profile_var.set("")
        self._new_profile_specific_mods_var.set(False)
        self._new_profile_bar.grid()
        self._new_profile_entry.focus_set()

    def hide_new_profile_bar(self):
        """Hide the inline new-profile bar."""
        self._new_profile_bar.grid_remove()
        self._new_profile_create_fn = None

    def _on_new_profile_create(self):
        name = self._new_profile_var.get().strip()
        if not name:
            return
        fn = getattr(self, "_new_profile_create_fn", None)
        self.hide_new_profile_bar()
        if fn:
            fn(name, self._new_profile_specific_mods_var.get())

    def _build_header(self):
        # Use design size 28: CTk applies its own widget scaling, so scaled() would double-scale
        self._header = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=28)
        self._header.grid(row=1, column=1, sticky="ew")
        self._header.grid_propagate(False)
        # Header labels placed after canvas is built (we need its width)
        self._header_labels: list[ctk.CTkLabel] = []
        # Grey divider lines between columns — drag events bound directly to these
        self._header_dividers: list[tk.Frame] = []
        # Column visibility menu button (created lazily in _update_header)
        self._col_menu_btn: tk.Label | None = None
        self._col_menu_popup: CTkPopupMenu | None = None

    def _build_canvas(self):
        frame = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0)
        frame.grid(row=2, column=1, sticky="nsew")
        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(frame, bg=BG_DEEP, bd=0, highlightthickness=0,
                                 yscrollincrement=1, takefocus=0)
        # Custom combined scrollbar + marker strip: single canvas that paints
        # the trough, tick markers, and a stippled thumb (so ticks bleed
        # through). Replaces native tk.Scrollbar + separate marker canvas.
        self._SCROLL_W = 16
        self._marker_strip = tk.Canvas(frame, bg=BG_DEEP, bd=0, highlightthickness=0,
                                       width=self._SCROLL_W, takefocus=0)
        # Alias kept for any external references; _vsb is no longer a tk.Scrollbar.
        self._vsb = self._marker_strip
        self._canvas.configure(yscrollcommand=self._scroll_set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._marker_strip.grid(row=0, column=1, sticky="ns")
        # Scroll state
        self._scroll_first = 0.0
        self._scroll_last = 1.0
        self._thumb_drag_offset: float | None = None  # fractional offset within thumb when drag starts
        self._marker_strip.bind("<Configure>",   self._on_marker_strip_resize)
        self._marker_strip.bind("<ButtonPress-1>",   self._on_scrollbar_press)
        self._marker_strip.bind("<B1-Motion>",       self._on_scrollbar_drag)
        self._marker_strip.bind("<ButtonRelease-1>", self._on_scrollbar_release)
        self._marker_strip.bind("<Button-4>",        self._on_scroll_up)
        self._marker_strip.bind("<Button-5>",        self._on_scroll_down)
        self._marker_strip.bind("<MouseWheel>",      self._on_mousewheel)

        self._canvas_w = 600   # updated on first <Configure>
        self._layout_columns(600)  # init before first _redraw (avoids IndexError on _COL_X[6])
        self._canvas.bind("<Configure>",      self._on_canvas_resize)
        self._canvas.bind("<Button-4>",       self._on_scroll_up)
        self._canvas.bind("<Button-5>",       self._on_scroll_down)
        self._canvas.bind("<MouseWheel>",     self._on_mousewheel)
        self._canvas.bind("<ButtonPress-1>",         self._on_mouse_press)
        self._canvas.bind("<Control-ButtonPress-1>", self._on_mouse_press)
        self._canvas.bind("<B1-Motion>",             self._on_mouse_drag)
        self._canvas.bind("<ButtonRelease-1>",       self._on_mouse_release)
        self._canvas.bind("<ButtonRelease-3>", self._on_right_click)
        self._canvas.bind("<ButtonRelease-2>", self._on_middle_click)
        self._canvas.bind("<Motion>",         self._on_mouse_motion)
        self._canvas.bind("<Leave>",          self._on_mouse_leave)

        self._create_pool()

    def _build_toolbar(self):
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        bar.grid(row=3, column=1, sticky="ew")
        bar.grid_propagate(False)
        self._toolbar_bar = bar

        # Expand/Collapse all separators toggle
        self._expand_collapse_all_btn = ctk.CTkButton(
            bar, text="Expand all", width=90, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._toggle_all_separators
        )
        self._expand_collapse_all_btn.pack(side="left", padx=4, pady=5)
        self._update_expand_collapse_all_btn()

        # Enable/Disable all mods toggle
        self._enable_disable_all_btn = ctk.CTkButton(
            bar, text="Enable all", width=90, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._toggle_all_mods_enabled
        )
        self._enable_disable_all_btn.pack(side="left", padx=4, pady=5)
        self._update_enable_disable_all_btn()

        # Check for Nexus mod updates button
        self._update_btn = ctk.CTkButton(
            bar, text="Check Updates", width=110, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._on_check_updates
        )
        self._update_btn.pack(side="left", padx=4, pady=5)
        self._help_tooltip = TkTooltip(
            self, bg="#1a1a2e", fg="#d6e4ff", font=_theme.TK_FONT_SMALL,
        )
        self._help_tooltip.attach(
            self._update_btn,
            "Checks every mod against Nexus and reports:\n"
            "  • Newer versions available for installed mods\n"
            "  • Mods with missing required dependencies\n"
            "It also refreshes each mod's metadata — Category, Version,\n"
            "size, endorsement status and install date shown in the columns.",
        )

        self._filter_btn = ctk.CTkButton(
            bar, text="Filters", width=64, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._on_open_filters
        )
        self._filter_btn.pack(side="left", padx=4, pady=5)

        self._restore_backup_btn = ctk.CTkButton(
            bar, text="Restore backup", width=110, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._on_restore_backup,
            state="disabled",
        )
        self._restore_backup_btn.pack(side="left", padx=4, pady=5)

        # Refresh button (icon only)
        refresh_icon = _load_icon("refresh.png", size=(16, 16))
        ctk.CTkButton(
            bar, text="Refresh Modlist" if refresh_icon else "↺", image=refresh_icon,
            width=30, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._on_refresh_clicked
        ).pack(side="left", padx=4, pady=5)

        ctk.CTkButton(
            bar, text="Generate Separators", width=140, height=26,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._generate_separators
        ).pack(side="left", padx=4, pady=5)

        self._status_bar = None  # set via set_status_bar() after construction

    def _build_search_bar(self):
        bar = tk.Frame(self, bg=BG_DEEP, bd=0, highlightthickness=0, height=scaled(32))
        bar.grid(row=4, column=1, sticky="ew")
        bar.grid_propagate(False)

        tk.Label(bar, text="🔍", bg=BG_DEEP, fg=TEXT_DIM,
                 font=(_theme.FONT_FAMILY, _theme.FS11)).pack(side="left", padx=(8, 2), pady=4)

        self._search_entry = tk.Entry(
            bar,
            bg=BG_PANEL, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=(_theme.FONT_FAMILY, _theme.FS11),
            bd=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
        )
        self._search_entry.pack(side="left", fill="x", expand=True, padx=(2, 2), pady=4)

        self._search_clear_btn = ctk.CTkButton(
            bar, text="✕", width=32, height=24,
            fg_color=BTN_DANGER, hover_color=BTN_DANGER_HOV, text_color=TEXT_WHITE,
            font=_theme.FONT_HEADER, cursor="hand2",
            command=self._on_search_clear,
        )
        self._search_clear_btn.pack(side="left", padx=(0, 8), pady=4)
        self._search_clear_btn.pack_forget()  # hidden until there is text

        # KeyRelease fires after the character is committed to the widget
        self._search_entry.bind("<KeyRelease>", self._on_search_change)
        self._search_entry.bind("<Escape>", self._on_search_clear)
        self._search_entry.bind("<Control-a>", lambda e: (
            self._search_entry.select_range(0, "end"),
            self._search_entry.icursor("end"),
            "break"
        )[-1])

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    def _layout_columns(self, canvas_w: int):
        """Compute column x positions given the current canvas width.

        All columns fill the available space. Dragging a boundary grows one
        column while shrinking its neighbour — total width stays constant.
        """
        scale = min(1.4, max(0.5, canvas_w / scaled(700)))
        gap = scaled(10)
        scroll_gap = scaled(14)
        # Extra right-side pad for the column-visibility menu button
        menu_btn_pad = scaled(22)
        x0, x1 = scaled(4), scaled(32)
        hidden = self._col_hidden
        # Number of visible cols among 2..8 (name col 1 is always visible)
        visible_data_cols = [dc for dc in self._col_order if dc not in hidden]
        n_visible = len(visible_data_cols)
        # gaps: between name and col2, between subsequent visible cols, and before scrollbar
        avail = canvas_w - x1 - gap * (n_visible + 1) - scroll_gap - menu_btn_pad

        # Default proportional widths keyed by data-col index (1=name, 2=cat, 3=flags, 4=conf, 5=inst, 6=prio, 7=version, 8=size)
        data_defaults = {
            1: max(scaled(80), avail - scaled(int((130 + 56 + 95 + 100 + 72 + 90 + 80) * scale))),
            2: scaled(int(130 * scale)),
            3: scaled(int(56 * scale)),
            4: scaled(int(95 * scale)),
            5: scaled(int(100 * scale)),
            6: scaled(int(72 * scale)),
            7: scaled(int(90 * scale)),
            8: scaled(int(80 * scale)),
        }
        data_mins = {1: scaled(120), 2: scaled(95), 3: scaled(70), 4: scaled(95), 5: scaled(95), 6: scaled(80), 7: scaled(80), 8: scaled(75)}

        ov = self._col_w_override
        # Width per visible data col (name col 1 always included)
        visible_dcs = [1] + visible_data_cols
        data_widths = {dc: max(data_mins[dc], ov.get(dc, data_defaults[dc])) for dc in visible_dcs}

        # Scale all widths proportionally to fit available space exactly
        total = sum(data_widths[dc] for dc in visible_dcs)
        if total != avail and avail > 0:
            factor = avail / total
            data_widths = {dc: max(data_mins[dc], int(data_widths[dc] * factor)) for dc in visible_dcs}
            remainder = avail - sum(data_widths.values())
            data_widths[1] += remainder

        # Build slot-ordered widths: slot 0=checkbox, 1=name, then visible cols in order.
        # Hidden cols get slot indices too, but with width=0 and x off-screen, so draw
        # code that references them via _col_pos sees invisible geometry.
        slot_data = [0, 1] + visible_data_cols  # slot → data col
        # widths[0] = name (slot 1), widths[1..n_visible] = visible cols (slots 2..n_visible+1),
        # remaining entries are 0 for dead/hidden slots.
        widths = []
        for i in range(8):
            slot = i + 1  # _COL_W slot index
            if slot < len(slot_data):
                widths.append(data_widths.get(slot_data[slot], 0))
            else:
                widths.append(0)

        # Build _col_pos: data col → slot index in _COL_X/_COL_W
        self._col_pos = {1: 1}
        for k, dc in enumerate(visible_data_cols):
            self._col_pos[dc] = k + 2  # slots 2..(n_visible+1)

        # Build x positions for visible slots
        xs = [x0, x1]
        for i in range(n_visible):
            xs.append(xs[-1] + widths[i] + gap)
        # Pad remaining slots (for hidden cols) with a far off-screen sentinel
        off_x = -99999
        while len(xs) < 9:
            xs.append(off_x)

        # Hidden col slots: assign them slot indices past the visible ones, off-screen.
        for k, dc in enumerate(self._col_order):
            if dc in hidden:
                slot = n_visible + 2  # shared off-screen slot (width 0, x off_x)
                if slot >= 9:
                    slot = 8
                self._col_pos[dc] = slot

        self._COL_X = xs
        self._COL_W = [scaled(28)] + widths
        self._canvas_w = canvas_w
        self._name_col_right = x1 + widths[0] - scaled(4)
        # Right edge of the last visible column (for placing the menu button).
        self._last_col_right = xs[n_visible + 1] if n_visible > 0 else (x1 + widths[0])

    # Static sort key names by data-col index
    _DATA_COL_SORT_KEYS = {1: "name", 2: "category", 3: "flags", 4: "conflicts", 5: "installed", 6: "priority", 7: "version", 8: "size"}
    # Static titles by data-col index
    _DATA_COL_TITLES = {0: "", 1: "Mod Name", 2: "Category", 3: "Flags", 4: "Conflicts", 5: "Installed", 6: "Priority", 7: "Version", 8: "Size"}

    def _update_header(self, canvas_w: int):
        try:
            self._header.configure(width=canvas_w)
        except RecursionError:
            return

        # Build slot-ordered metadata using only visible cols.
        visible = [dc for dc in self._col_order if dc not in self._col_hidden]
        slot_data_cols = [0, 1] + visible + [0] * (7 - len(visible))
        titles  = [self._DATA_COL_TITLES.get(slot_data_cols[i], "") for i in range(9)]
        x_pos   = self._COL_X
        anchors = ["center"] * 9
        widths  = self._COL_W
        for i, (title, x, anc, w) in enumerate(zip(titles, x_pos, anchors, widths)):
            dc = slot_data_cols[i]
            # Slots past the last visible col have no real data col → hide any existing label.
            is_dead_slot = (i >= 2 + len(visible))
            if is_dead_slot:
                if i < len(self._header_labels):
                    try:
                        self._header_labels[i].place_forget()
                    except Exception:
                        pass
                continue
            sort_key = self._DATA_COL_SORT_KEYS.get(dc)
            is_movable = dc in (2, 3, 4, 5, 6, 7, 8)
            display = title
            if sort_key and sort_key == self._sort_column:
                arrow = " ▲" if self._sort_ascending else " ▼"
                display = title + arrow
            if i < len(self._header_labels):
                lbl = self._header_labels[i]
                lbl.configure(
                    text=display,
                    fg=ACCENT if sort_key == self._sort_column else TEXT_SEP,
                    cursor="fleur" if is_movable else ("hand2" if sort_key else ""),
                )
                # Rebind events so they reflect the current dc/sort_key for this slot
                lbl.unbind("<Button-1>")
                lbl.unbind("<ButtonPress-1>")
                lbl.unbind("<B1-Motion>")
                lbl.unbind("<ButtonRelease-1>")
                if sort_key and not is_movable:
                    lbl.bind("<Button-1>", lambda e, k=sort_key: self._on_header_click(k))
                if is_movable:
                    lbl.bind("<ButtonPress-1>",  lambda e, d=dc, k=sort_key: self._on_hdr_drag_start(e, d, k))
                    lbl.bind("<B1-Motion>",       self._on_hdr_drag_motion)
                    lbl.bind("<ButtonRelease-1>", self._on_hdr_drag_end)
                lbl.place(x=x, y=0, height=scaled(28), width=w)
            else:
                lbl = tk.Label(
                    self._header, text=display, anchor=anc,
                    font=(_theme.FONT_FAMILY, _theme.FS11, "bold"),
                    fg=ACCENT if sort_key == self._sort_column else TEXT_SEP,
                    bg=BG_HEADER, bd=0,
                    cursor="fleur" if is_movable else ("hand2" if sort_key else ""),
                )
                if sort_key and not is_movable:
                    lbl.bind("<Button-1>", lambda e, k=sort_key: self._on_header_click(k))
                if is_movable:
                    lbl.bind("<ButtonPress-1>",  lambda e, d=dc, k=sort_key: self._on_hdr_drag_start(e, d, k))
                    lbl.bind("<B1-Motion>",       self._on_hdr_drag_motion)
                    lbl.bind("<ButtonRelease-1>", self._on_hdr_drag_end)
                lbl.place(x=x, y=0, height=scaled(28), width=w)
                self._header_labels.append(lbl)

        # Place divider grab handles at each resizable boundary (x2..x6).
        # Wider hit area (8px) with a visible 2px line centered inside.
        # Events are bound directly to dividers so header labels can't intercept.
        boundaries = self._COL_X[2:]  # skip x0 (checkbox) and x1 (name start)
        n_visible_local = len(visible)
        for i, bx in enumerate(boundaries):
            col_idx = i + 1  # _COL_W index of the left column at this boundary
            # Only place dividers for boundaries that sit between visible cols.
            is_visible_boundary = (i < n_visible_local)
            if i < len(self._header_dividers):
                div = self._header_dividers[i]
            else:
                div = tk.Frame(self._header, bg=BG_HEADER, cursor="sb_h_double_arrow",
                               highlightthickness=0, bd=0)
                line = tk.Frame(div, bg=BORDER_FAINT, width=2)
                line.place(relx=0.5, y=4, anchor="n", width=2, height=scaled(20))
                # Bind drag events directly to the divider
                div.bind("<ButtonPress-1>", lambda e, c=col_idx: self._on_divider_drag_start(e, c))
                div.bind("<B1-Motion>", self._on_header_col_drag_motion)
                div.bind("<ButtonRelease-1>", self._on_header_col_drag_end)
                div.bind("<Double-Button-1>", lambda e, c=col_idx: self._on_divider_drag_reset(e, c))
                # Also bind to the inner line so clicks on the visible part work
                line.bind("<ButtonPress-1>", lambda e, c=col_idx: self._on_divider_drag_start(e, c))
                line.bind("<B1-Motion>", self._on_header_col_drag_motion)
                line.bind("<ButtonRelease-1>", self._on_header_col_drag_end)
                line.bind("<Double-Button-1>", lambda e, c=col_idx: self._on_divider_drag_reset(e, c))
                line.configure(cursor="sb_h_double_arrow")
                self._header_dividers.append(div)
            if is_visible_boundary:
                div.place(x=bx - 4, y=0, width=8, height=scaled(28))
                div.lift()  # raise above labels
            else:
                try:
                    div.place_forget()
                except Exception:
                    pass

        # Column visibility menu button (far right of header)
        if not hasattr(self, "_col_menu_btn") or self._col_menu_btn is None:
            btn = tk.Label(
                self._header, text="⋮",
                font=(_theme.FONT_FAMILY, _theme.FS12, "bold"),
                fg=TEXT_SEP, bg=BG_HEADER, bd=0, cursor="hand2",
            )
            btn.bind("<Button-1>", lambda e: self._show_column_menu())
            btn.bind("<Enter>", lambda e: btn.configure(fg=ACCENT))
            btn.bind("<Leave>", lambda e: btn.configure(fg=TEXT_SEP))
            self._col_menu_btn = btn
        btn_w = scaled(20)
        btn_x = max(0, canvas_w - btn_w - scaled(14))
        self._col_menu_btn.place(x=btn_x, y=0, width=btn_w, height=scaled(28))
        self._col_menu_btn.lift()

    # ------------------------------------------------------------------
    # Load / reload
    # ------------------------------------------------------------------

    # (attr, read_fn, write_fn, default_factory)
    _STATE_IO_SPECS = (
        ("_sep_locks",          read_separator_locks,         write_separator_locks,         dict),
        ("_sep_colors",         read_separator_colors,        write_separator_colors,        dict),
        ("_sep_deploy_paths",   read_separator_deploy_paths,  write_separator_deploy_paths,  dict),
        ("_mod_strip_prefixes", read_mod_strip_prefixes,      write_mod_strip_prefixes,      dict),
        ("_root_folder_enabled", read_root_folder_state,      write_root_folder_state,       lambda: True),
        ("_collapsed_seps",     read_collapsed_seps,          write_collapsed_seps,          set),
    )

    def _load_state(self, attr: str) -> None:
        spec = next(s for s in self._STATE_IO_SPECS if s[0] == attr)
        _, read_fn, _, default_factory = spec
        if self._modlist_path is None:
            setattr(self, attr, default_factory())
            return
        setattr(self, attr, read_fn(self._modlist_path.parent, self.__profile_state))

    def _save_state(self, attr: str) -> None:
        if self._modlist_path is None:
            return
        spec = next(s for s in self._STATE_IO_SPECS if s[0] == attr)
        _, _, write_fn, _ = spec
        write_fn(self._modlist_path.parent, getattr(self, attr))

    def _load_sep_locks(self) -> None:          self._load_state("_sep_locks")
    def _save_sep_locks(self) -> None:          self._save_state("_sep_locks")
    def _load_sep_colors(self) -> None:         self._load_state("_sep_colors")
    def _save_sep_colors(self) -> None:         self._save_state("_sep_colors")
    def _load_sep_deploy_paths(self) -> None:   self._load_state("_sep_deploy_paths")
    def _save_sep_deploy_paths(self) -> None:   self._save_state("_sep_deploy_paths")
    def _load_mod_strip_prefixes(self) -> None: self._load_state("_mod_strip_prefixes")
    def _load_root_folder_state(self) -> None:  self._load_state("_root_folder_enabled")
    def _save_root_folder_state(self) -> None:  self._save_state("_root_folder_enabled")
    def _load_collapsed(self) -> None:          self._load_state("_collapsed_seps")
    def _save_collapsed(self) -> None:          self._save_state("_collapsed_seps")

    def _save_mod_strip_prefixes(self) -> None:
        self._save_state("_mod_strip_prefixes")
        if self._modlist_path is not None:
            self.__profile_state.pop("mod_strip_prefixes", None)

    def _compute_bundle_groups(self) -> None:
        """No-op kept for call sites.

        RE/Fluffy bundles now install as a single normal mod (option selection
        is materialised inside the mod folder via Utils/re_bundle.py), so the
        mod list no longer has bundle "variants" or radio groups.  ``_bundle_groups``
        stays empty so the legacy bundle-aware code paths treat every row as an
        ordinary mod.
        """
        self._bundle_groups = {}

    def _bundle_name_of(self, idx: int) -> "str | None":
        """Legacy hook — bundles are plain mods now, so always None."""
        return None

    def _variant_name_of(self, idx: int) -> "str | None":
        """Legacy hook — bundles are plain mods now, so always None."""
        return None

    # ------------------------------------------------------------------
    # RE/Fluffy single-mod bundles (option selection)
    # ------------------------------------------------------------------
    def _bundle_spec_path(self, idx: int) -> "Path | None":
        """Return the meta.ini path for entry *idx* if it's a RE/Fluffy bundle
        mod (carries a ``[Bundle]`` spec), else None."""
        if not (0 <= idx < len(self._entries)) or self._staging_root is None:
            return None
        entry = self._entries[idx]
        if entry.is_separator:
            return None
        meta = self._staging_root / entry.name / "meta.ini"
        try:
            from Utils.re_bundle import read_bundle_spec
            return meta if read_bundle_spec(meta) is not None else None
        except Exception:
            return None

    def _open_bundle_options(self, idx: int) -> None:
        """Open the Bundle Options selector for the bundle mod at *idx* — an
        in-app overlay over the plugin panel (same pattern as separator
        settings).  On Save the new selection is materialised, the mod is
        re-indexed, and the filemap rebuilt."""
        meta = self._bundle_spec_path(idx)
        if meta is None or self._staging_root is None:
            return
        from Utils.re_bundle import read_bundle_spec
        spec = read_bundle_spec(meta)
        if spec is None:
            return
        mod_name = self._entries[idx].name

        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_bundle_options_panel", None)
        if show_fn is None:
            return
        def _on_done(panel):
            if getattr(panel, "result", None) is not None:
                self._apply_bundle_selection(mod_name, meta, panel.result)
        from Utils.re_bundle import BUNDLE_LIB_DIR
        lib_dir = self._staging_root / mod_name / BUNDLE_LIB_DIR
        show_fn(mod_name, spec, _on_done, lib_dir=lib_dir)

    def _apply_bundle_selection(self, mod_name, meta_path, new_spec) -> None:
        """Persist *new_spec*, re-materialise the bundle's selection, re-index
        just that mod, and rebuild the filemap."""
        if self._staging_root is None:
            return
        from Utils.re_bundle import write_bundle_spec, materialize_selection
        write_bundle_spec(meta_path, new_spec)
        materialize_selection(self._staging_root / mod_name, new_spec)
        # Re-index just this mod (scanner skips .mm_bundle/), then rebuild filemap.
        try:
            rescan_mods_in_index(
                self._staging_root.parent / "modindex.bin",
                self._staging_root, [mod_name],
                strip_prefixes=getattr(self._game, "mod_folder_strip_prefixes", set()) or None,
                allowed_extensions=getattr(self._game, "install_extensions", None) or None,
                normalize_folder_case=getattr(self._game, "normalize_folder_case", True),
                exclude_dirs=self._filemap_exclude_dirs or None,
                root_folder_mods=set(self._root_folder_mods) if self._root_folder_mods else None,
            )
        except Exception as e:
            self._log(f"Bundle options: reindex failed: {e}")
        self._rebuild_filemap()
        self._redraw()
        _show_mod_notification(self.winfo_toplevel(), f"Updated bundle: {mod_name}")

    def _is_bundle_separator(self, idx: int) -> bool:
        """True if the entry at *idx* is a separator that owns a bundle block."""
        e = self._entries[idx]
        if not e.is_separator:
            return False
        display = e.display_name  # strips _separator suffix
        return display in self._bundle_groups

    def _clamp_outside_bundle_blocks(self, insert_at: int) -> int:
        """If *insert_at* falls strictly inside a bundle separator block,
        push it just before that block so non-bundle mods can never land
        inside a bundle group."""
        # Walk backwards to find the separator that owns this position.
        for si in range(insert_at - 1, -1, -1):
            e = self._entries[si]
            if e.is_separator:
                if self._is_bundle_separator(si):
                    blk = self._sep_block_range(si)
                    if insert_at < blk.stop:
                        # insert_at is inside the bundle block — move before it
                        return si
                break  # inside a normal separator block — fine
        return insert_at

    def _clamp_insert_at(self, insert_at: int, is_block: bool) -> int:
        """Clamp insert_at so items never cross OW or Root Folder boundaries.

        Works regardless of whether _entries is in natural or inverted order.
        After removing the dragged items, OW and Root are at fixed positions.
        Nothing should be inserted before whichever is first or after whichever
        is last.
        """
        n = len(self._entries)
        # Find OW and Root positions in the current (post-removal) _entries.
        # If the dragged item IS a pinned separator it won't be in _entries,
        # so we also check the drag block for pinned names.
        ow_idx = -1
        rf_idx = -1
        for i, e in enumerate(self._entries):
            if e.name == OVERWRITE_NAME:
                ow_idx = i
            elif e.name == ROOT_FOLDER_NAME:
                rf_idx = i

        # If neither pinned separator is found, no clamping needed.
        if ow_idx < 0 and rf_idx < 0:
            return max(0, min(insert_at, n))

        # When only one pinned separator exists, clamp relative to it.
        if ow_idx < 0 or rf_idx < 0:
            pinned = ow_idx if ow_idx >= 0 else rf_idx
            # Items go after OW or before Root
            if self._entries[pinned].name == OVERWRITE_NAME:
                lo = pinned + 1
                return max(lo, min(insert_at, n))
            else:
                hi = pinned
                return max(0, min(insert_at, hi))

        # Both pinned separators present.
        first_pinned = min(ow_idx, rf_idx)
        last_pinned = max(ow_idx, rf_idx)

        lo = first_pinned + 1
        hi = last_pinned

        return max(lo, min(insert_at, hi))

    def _reload(self):
        self._sel_idx = -1
        self._sel_set = set()
        self._drag_idx = -1
        # Preserve the active column sort across reloads.
        # Remove stale lock canvas items before rebuilding
        c = getattr(self, "_canvas", None)
        if c is not None:
            for item_id in self._lock_cb_rects.values():
                c.delete(item_id)
            for item_id in self._lock_cb_marks.values():
                c.delete(item_id)
        self._lock_cb_rects.clear()
        self._lock_cb_marks.clear()
        if self._modlist_path is None:
            self._entries = []
        else:
            # Sync any mods in the mods folder not yet in modlist.txt
            mods_dir = self._staging_root
            sync_modlist_with_mods_folder(self._modlist_path, mods_dir)
            self._load_root_folder_state()
            self._load_mod_strip_prefixes()
            self._load_sep_deploy_paths()
            self._entries = read_modlist(self._modlist_path)
            # Prepend synthetic Overwrite row — always first (highest priority),
            # never saved to modlist.txt.
            self._entries.insert(0, ModEntry(
                name=OVERWRITE_NAME, enabled=True, locked=True, is_separator=True
            ))
            # Append synthetic Root_Folder row at the bottom (lowest priority)
            # if the folder exists.
            root_folder_dir = (
                self._game.get_effective_root_folder_path()
                if self._game is not None
                else self._modlist_path.parent.parent.parent / "Root_Folder"
            )
            if root_folder_dir.is_dir():
                self._entries.append(ModEntry(
                    name=ROOT_FOLDER_NAME,
                    enabled=self._root_folder_enabled,
                    locked=True, is_separator=True
                ))
        if self._modlist_path is not None:
            self.__profile_state = read_profile_state(self._modlist_path.parent)
            self._disabled_plugins_map = read_disabled_plugins(self._modlist_path.parent, self.__profile_state)
            _exc = read_excluded_mod_files(self._modlist_path.parent, self.__profile_state)
            self._excluded_mod_files_map = _exc or {}
            self._mod_notes_map = read_mod_notes(self._modlist_path.parent, self.__profile_state)
        self._load_sep_locks()
        self._load_sep_colors()
        self._load_collapsed()
        self._update_expand_collapse_all_btn()
        self._update_enable_disable_all_btn()
        # Defer meta scan to background so the window appears sooner
        self._scan_meta_flags_async()
        self._rebuild_check_widgets()  # also calls _compute_bundle_groups()
        # Refresh always rescans all mod folders to rebuild the index from scratch.
        self._filemap_rescan_index = True
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _on_refresh_clicked(self):
        """Refresh button handler: reload and show a notification."""
        self._reload()
        try:
            _show_mod_notification(self.winfo_toplevel(), "Modlist Refreshed", state="info")
        except Exception:
            pass

    def _scan_meta_flags(self):
        """Single pass over meta.ini: update, missing_reqs, endorsed, install_dates (sync)."""
        results = _scan_meta_flags_impl(self._entries, self._staging_root,
                                        compute_sizes=8 not in self._col_hidden)
        self._apply_meta_results(results)

    def _scan_meta_flags_async(self):
        """Run meta scan in background so the window appears sooner; apply results when done.

        Note: we no longer clear the dictionaries up-front.  The previous scan's
        data stays visible until the new results arrive, avoiding a flash where
        Category / Flags / Installed columns go blank during the async gap.
        """
        if self._modlist_path is None or not self._staging_root.is_dir():
            self._update_mods.clear()
            self._missing_reqs.clear()
            self._missing_reqs_detail.clear()
            self._endorsed_mods.clear()
            self._install_dates.clear()
            self._install_datetimes.clear()
            self._category_names.clear()
            self._mod_versions.clear()
            self._mod_sizes.clear()
            self._mod_size_bytes.clear()
            self._fomod_mods.clear()
            self._bain_mods.clear()
            self._xedit_modified_mods.clear()
            self._vis_dirty = True
            self._mod_to_sep_idx = None
            return
        if not self._call_threadsafe:
            self._scan_meta_flags()  # Fallback: sync if no thread-safe callback
            return
        entries = list(self._entries)
        mods_dir = self._staging_root
        modlist_path = self._modlist_path
        call_threadsafe = self._call_threadsafe
        compute_sizes = 8 not in self._col_hidden

        def _worker():
            results = _scan_meta_flags_impl(entries, mods_dir, compute_sizes)
            results["_modlist_path"] = modlist_path
            call_threadsafe(lambda: self._apply_meta_results(results))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_meta_results(self, results: dict):
        """Merge scan results into instance state and redraw (main thread only)."""
        # Staleness check only applies to async scans (which add _modlist_path).
        # Sync scans (e.g. after Check Updates) don't include it; avoid rejecting them.
        if "_modlist_path" in results and results["_modlist_path"] != self._modlist_path:
            return  # Stale: user switched game before scan finished
        self._update_mods = results["update_mods"]
        self._description_cache.clear()
        self._missing_reqs = results["missing_reqs"]
        self._missing_reqs_detail = results["missing_reqs_detail"]
        self._endorsed_mods = results["endorsed_mods"]
        self._install_dates = results["install_dates"]
        self._install_datetimes = results["install_datetimes"]
        self._category_names = results.get("category_names", {})
        self._mod_versions = results.get("mod_versions", {})
        self._mod_sizes = results.get("mod_sizes", {})
        self._mod_size_bytes = results.get("mod_size_bytes", {})
        self._fomod_mods = results.get("fomod_mods", set())
        self._bain_mods = results.get("bain_mods", set())
        self._root_folder_mods = results.get("root_folder_mods", set())
        self._root_rule_mods = self._compute_root_rule_mods()
        self._collection_bundled_mods = results.get("collection_bundled_mods", set())
        self._collection_patched_mods = results.get("collection_patched_mods", set())
        self._xedit_modified_mods = results.get("xedit_modified_mods", {})
        self._bundle_mods = results.get("bundle_mods", set())
        if self._filter_panel_open:
            self._refresh_filter_category_list()
        self._vis_dirty = True
        self._mod_to_sep_idx = None
        self._redraw()
        self._update_info()

    def _compute_root_rule_mods(self) -> set[str]:
        """Mods owning files matched by a custom routing rule with dest=""."""
        game = self._game
        if game is None:
            return set()
        try:
            rules = list(getattr(game, "custom_routing_rules", None) or [])
        except Exception:
            return set()
        if not any(r.dest == "" and not r.to_prefix for r in rules):
            return set()
        try:
            from Utils.filemap import read_mod_index
            from Utils.deploy_custom_rules import mods_matching_root_rules
        except Exception:
            return set()
        index_path = self._staging_root.parent / "modindex.bin"
        index = read_mod_index(index_path)
        if not index:
            return set()
        mod_files = {
            name: list(normal.values()) + list(root.values())
            for name, (normal, root) in index.items()
        }
        return mods_matching_root_rules(mod_files, rules)

    def _scan_update_flags(self):
        """Scan meta.ini for update flags. Uses async to avoid blocking UI."""
        self._scan_meta_flags_async()

    def _scan_missing_reqs_flags(self):
        """Scan meta.ini for missing requirements. Uses async to avoid blocking UI."""
        self._scan_meta_flags_async()

    def _scan_endorsed_flags(self):
        """Scan meta.ini for endorsed mods. Uses async to avoid blocking UI."""
        self._scan_meta_flags_async()

    def _scan_install_dates(self):
        """Scan meta.ini for install dates. Uses async to avoid blocking UI."""
        self._scan_meta_flags_async()

    def _save_ignored_missing_reqs(self) -> None:
        """Persist _ignored_missing_reqs to profile_state.json."""
        if self._modlist_path is None:
            return
        write_ignored_missing_requirements(self._modlist_path.parent, self._ignored_missing_reqs)

    def _rebuild_check_widgets(self):
        """Rebuild per-entry BooleanVars (logical state only — visual pool is separate)."""
        self._check_vars.clear()
        self._invalidate_derived_caches()

        for entry in self._entries:
            if entry.is_separator:
                # Placeholder keeps indices aligned with self._entries
                self._check_vars.append(None)
                continue
            var = tk.BooleanVar(value=entry.enabled)
            self._check_vars.append(var)

    def _invalidate_derived_caches(self) -> None:
        """Mark cached data derived from _entries as stale.

        Call whenever _entries is mutated (add/remove/reorder) or filter/sort
        state changes so that _redraw() recomputes priorities and visible indices.
        """
        self._vis_dirty = True
        self._mod_to_sep_idx = None
        self._priorities = {}
        self._sep_block_cache: dict[int, range] = {}
        self._compute_bundle_groups()

    # ------------------------------------------------------------------
    # Virtual-list pool
    # ------------------------------------------------------------------

    def _create_pool(self) -> None:
        """Pre-allocate a fixed set of canvas items and checkbutton widgets.

        Called once after the canvas is created.  On every _redraw() call the
        pool items are reconfigured (coords/text/fill/state) rather than deleted
        and recreated.  Items outside the visible viewport are set to
        state='hidden' so Tkinter does not render them.
        """
        for s in range(self._pool_size):
            self._create_pool_slot(s)

    def _ensure_pool_capacity(self, needed: int) -> None:
        """Grow the slot pool when the viewport holds more rows than slots."""
        if needed <= self._pool_size:
            return
        for s in range(self._pool_size, needed):
            self._create_pool_slot(s)
        self._pool_size = needed
        # Lazily-created lock checkboxes must stay above the new row backgrounds.
        self._canvas.tag_raise("lock_cb")

    def _create_pool_slot(self, s: int) -> None:
        c = self._canvas
        self._pool_data_idx.append(-1)
        self._pool_last_state.append(None)

        # Background rectangle
        bg_id = c.create_rectangle(0, -200, 0, -200, fill="", outline="", state="hidden")
        # Mod name / separator label text
        name_id = c.create_text(0, -200, text="", anchor="w", fill="",
                                font=(_theme.FONT_FAMILY, _theme.FS11), state="hidden")
        # Flags column icons (up to 3 icons + lock star text)
        flag_id = c.create_image(0, -200, anchor="center", state="hidden")
        flag2_id = c.create_image(0, -200, anchor="center", state="hidden")
        flag3_id = c.create_image(0, -200, anchor="center", state="hidden")
        flag4_id = c.create_image(0, -200, anchor="center", state="hidden")
        flag5_id = c.create_image(0, -200, anchor="center", state="hidden")
        flag_star_id = c.create_text(0, -200, text="★", anchor="center", fill=TONE_FLAG,
                                     font=(_theme.FONT_FAMILY, _theme.FS11), state="hidden")
        # Conflict icons (left slot and right slot)
        conf1_id = c.create_image(0, -200, anchor="center", state="hidden")
        conf2_id = c.create_image(0, -200, anchor="center", state="hidden")
        # BSA conflict icons (images — archive-level conflict indicators)
        bsa_dot1_id = c.create_image(0, -200, anchor="center", state="hidden")
        bsa_dot2_id = c.create_image(0, -200, anchor="center", state="hidden")
        # Small vertical separator between loose-file icons and BSA dots
        bsa_sep_id = c.create_line(0, -200, 0, -200, fill=BORDER, width=1, state="hidden")
        # Category text
        cat_id = c.create_text(0, -200, text="", anchor="center", fill="",
                              font=(_theme.FONT_FAMILY, _theme.FS10), state="hidden")
        # Install date text
        inst_id = c.create_text(0, -200, text="", anchor="center", fill="",
                                font=(_theme.FONT_FAMILY, _theme.FS10), state="hidden")
        # Priority text
        prio_id = c.create_text(0, -200, text="", anchor="center", fill="",
                                font=(_theme.FONT_FAMILY, _theme.FS10), state="hidden")
        # Version text
        ver_id = c.create_text(0, -200, text="", anchor="center", fill="",
                               font=(_theme.FONT_FAMILY, _theme.FS10), state="hidden")
        # Size text
        size_id = c.create_text(0, -200, text="", anchor="center", fill="",
                                font=(_theme.FONT_FAMILY, _theme.FS10), state="hidden")
        # Separator collapse icon
        sep_icon_id = c.create_image(0, -200, anchor="center", state="hidden")
        # Separator decorative lines (left and right of label).
        # Lowered to sit just above the row background so flag/conflict
        # icons stack on top of them when columns overlap.
        sep_line_l = c.create_line(0, -200, 0, -200, fill="", width=1, state="hidden")
        sep_line_r = c.create_line(0, -200, 0, -200, fill="", width=1, state="hidden")
        c.tag_raise(sep_line_l, bg_id)
        c.tag_raise(sep_line_r, bg_id)
        c.tag_lower(sep_line_l, flag_id)
        c.tag_lower(sep_line_r, flag_id)
        # Custom deploy path badge (shown right of label on separators with override)
        sep_badge_id = c.create_text(0, -200, text="", anchor="w", fill="",
                                     font=(_theme.FONT_FAMILY, _theme.FS9), state="hidden")

        self._pool_bg.append(bg_id)
        self._pool_name.append(name_id)
        self._pool_flag_icon.append(flag_id)
        self._pool_flag_icon2.append(flag2_id)
        self._pool_flag_icon3.append(flag3_id)
        self._pool_flag_icon4.append(flag4_id)
        self._pool_flag_icon5.append(flag5_id)
        self._pool_flag_star.append(flag_star_id)
        self._pool_conflict_icon1.append(conf1_id)
        self._pool_conflict_icon2.append(conf2_id)
        self._pool_bsa_dot1.append(bsa_dot1_id)
        self._pool_bsa_dot2.append(bsa_dot2_id)
        self._pool_bsa_sep.append(bsa_sep_id)
        self._pool_category_text.append(cat_id)
        self._pool_install_text.append(inst_id)
        self._pool_priority_text.append(prio_id)
        self._pool_version_text.append(ver_id)
        self._pool_size_text.append(size_id)
        self._pool_sep_icon.append(sep_icon_id)
        self._pool_sep_line_l.append(sep_line_l)
        self._pool_sep_line_r.append(sep_line_r)
        self._pool_sep_badge.append(sep_badge_id)

        # Canvas-drawn checkbox (no tk.Checkbutton) — avoids opaque widget background
        # on Linux. Rect + checkmark text, click handled via tag_bind.
        var = tk.BooleanVar(value=False)
        self._pool_check_vars.append(var)
        cb_tag = f"pool_cb_{s}"
        rect_id = c.create_rectangle(
            0, -200, 0, -200, outline=BORDER, width=1, state="hidden",
            tags=(cb_tag, "pool_cb"),
        )
        mark_id = c.create_text(
            0, -200, text="✓", anchor="center", fill=ACCENT,
            font=(_theme.FONT_FAMILY, _theme.FS12, "bold"), state="hidden",
            tags=(cb_tag, "pool_cb"),
        )
        self._pool_cb_rect.append(rect_id)
        self._pool_cb_mark.append(mark_id)
        def _cb_press(e, slot=s):
            # Canvas tag_bind fires before bindtag "all", so the global
            # defocus handler in gui.App won't run here. Defocus manually
            # so clicking a row checkbox blurs the search entry.
            self._defocus_text_inputs()
            return "break"
        def _cb_release(e, slot=s):
            self._on_pool_check_toggle(slot)
            return "break"
        def _cb_enter(e, slot=s):
            c.config(cursor="hand2")
        def _cb_leave(e, slot=s):
            c.config(cursor="")
        c.tag_bind(cb_tag, "<ButtonPress-1>", _cb_press)
        c.tag_bind(cb_tag, "<ButtonRelease-1>", _cb_release)
        c.tag_bind(cb_tag, "<Enter>", _cb_enter)
        c.tag_bind(cb_tag, "<Leave>", _cb_leave)

    def _on_pool_check_toggle(self, slot: int) -> None:
        """A pooled enable-checkbox was clicked — map back to the entry and toggle."""
        # _on_mouse_press already handled this click via the checkbox hit-test — skip.
        if getattr(self, "_checkbox_click_handled", False):
            self._checkbox_click_handled = False
            return
        entry_idx = self._pool_data_idx[slot] if slot < len(self._pool_data_idx) else -1
        if entry_idx < 0:
            return
        # Bundle variants are toggleable even when locked=True (locked only prevents drag/rename).
        _entry = self._entries[entry_idx] if entry_idx < len(self._entries) else None
        if _entry and _entry.locked and self._bundle_name_of(entry_idx) is None:
            return
        # Toggle: flip current value, sync to logical var, persist
        checked = not self._pool_check_vars[slot].get()
        self._pool_check_vars[slot].set(checked)
        if entry_idx < len(self._check_vars) and self._check_vars[entry_idx] is not None:
            self._check_vars[entry_idx].set(checked)
        self._on_toggle(entry_idx)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _compute_conflict_highlights(
        self, sel_entry,
    ) -> tuple[set[int], set[int], set[str], set[str], set[str], set[str]]:
        """Build the six highlight sets _redraw needs from current selection.

        Returns (sep_higher, sep_lower, mod_higher, mod_lower, sel_higher, sel_lower):
        - sep_higher / sep_lower: separator row indices to tint green / red
          because a collapsed separator's block conflicts with the selection.
        - mod_higher / mod_lower: mod names to tint when a *separator* is selected
          and the conflicting mod sits in an expanded block.
        - sel_higher / sel_lower: mod names to tint when one or more *mods* are
          selected and the conflicting mod sits in an expanded block.
        """
        sep_higher: set[int] = set()
        sep_lower: set[int] = set()
        mod_higher: set[str] = set()
        mod_lower: set[str] = set()
        sel_higher: set[str] = set()
        sel_lower: set[str] = set()

        entries = self._entries
        n = len(entries)
        collapsed = self._collapsed_seps
        overrides = self._overrides
        overridden_by = self._overridden_by
        bsa_overrides = self._bsa_overrides
        bsa_overridden_by = self._bsa_overridden_by

        def _ow_idx() -> int:
            return next((j for j, e in enumerate(entries) if e.name == OVERWRITE_NAME), -1)

        if sel_entry and not sel_entry.is_separator:
            # Mods are selected — partition each conflict mod into "collapsed
            # separator → tint the separator" vs "expanded → tint the mod".
            for sel_i in self._sel_set:
                if sel_i < 0 or sel_i >= n:
                    continue
                e = entries[sel_i]
                if e.is_separator:
                    continue
                sel_name = e.name
                higher_set = overrides.get(sel_name, set()) | bsa_overrides.get(sel_name, set())
                lower_set = overridden_by.get(sel_name, set()) | bsa_overridden_by.get(sel_name, set())
                for cm in higher_set:
                    si = self._sep_idx_for_mod(cm)
                    if si >= 0 and entries[si].name in collapsed:
                        sep_higher.add(si)
                    else:
                        sel_higher.add(cm)
                for cm in lower_set:
                    si = self._sep_idx_for_mod(cm)
                    if si >= 0 and entries[si].name in collapsed:
                        sep_lower.add(si)
                    else:
                        sel_lower.add(cm)
        elif (sel_entry and sel_entry.is_separator
              and sel_entry.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)):
            # A normal separator is selected — highlight everything its block conflicts with.
            i = self._sel_idx + 1
            while i < n and not entries[i].is_separator:
                mod_name = entries[i].name
                higher_set = overrides.get(mod_name, set()) | bsa_overrides.get(mod_name, set())
                lower_set = overridden_by.get(mod_name, set()) | bsa_overridden_by.get(mod_name, set())
                for cm in higher_set:
                    si = self._sep_idx_for_mod(cm)
                    if si >= 0:
                        if entries[si].name in collapsed:
                            sep_higher.add(si)
                        else:
                            mod_higher.add(cm)
                    elif cm == OVERWRITE_NAME:
                        ow_idx = _ow_idx()
                        if ow_idx >= 0:
                            sep_higher.add(ow_idx)
                for cm in lower_set:
                    si = self._sep_idx_for_mod(cm)
                    if si >= 0:
                        if entries[si].name in collapsed:
                            sep_lower.add(si)
                        else:
                            mod_lower.add(cm)
                    elif cm == OVERWRITE_NAME:
                        ow_idx = _ow_idx()
                        if ow_idx >= 0:
                            sep_lower.add(ow_idx)
                i += 1
        elif sel_entry and sel_entry.name == OVERWRITE_NAME:
            ow_higher = (overrides.get(OVERWRITE_NAME, set())
                         | bsa_overrides.get(OVERWRITE_NAME, set()))
            for cm in ow_higher:
                si = self._sep_idx_for_mod(cm)
                if si >= 0 and entries[si].name in collapsed:
                    sep_higher.add(si)

        # Special case: when Overwrite overrides any selected mod, light up the
        # Overwrite row green so the user can see Overwrite is winning over them.
        if sel_entry and not sel_entry.is_separator:
            for sel_i in self._sel_set:
                if sel_i < 0 or sel_i >= n:
                    continue
                e = entries[sel_i]
                if e.is_separator:
                    continue
                ow_losers = (overridden_by.get(e.name, set())
                             | bsa_overridden_by.get(e.name, set()))
                if OVERWRITE_NAME in ow_losers:
                    ow_idx = _ow_idx()
                    if ow_idx >= 0:
                        sep_higher.add(ow_idx)
                    break

        return sep_higher, sep_lower, mod_higher, mod_lower, sel_higher, sel_lower

    def _sep_row_colors(self, i: int, entry,
                        conflict_sep_higher: set[int], conflict_sep_lower: set[int],
                        highlighted_sep_idx: int) -> tuple[str, str, str]:
        """Resolve a separator row's colours → (base_bg, row_bg, txt_col).

        Selected rows are forced to BG_SELECT; the active drag anchor (i ==
        _drag_idx) is still forced highlighted even mid-drag.
        """
        is_overwrite   = (entry.name == OVERWRITE_NAME)
        is_root_folder = (entry.name == ROOT_FOLDER_NAME)
        is_synthetic   = is_overwrite or is_root_folder
        is_sel_row = (i in self._sel_set) or (i == self._drag_idx)

        if is_overwrite:
            base_bg = BG_DARK_GREEN
            txt_col = TEXT_TREE_FG
        elif is_root_folder:
            base_bg = BG_DARK_BLUE if entry.enabled else _theme.BG_SEP
            txt_col = TONE_BLUE_SOFT if entry.enabled else TEXT_DIM
        else:
            custom_color = self._sep_colors.get(entry.name)
            base_bg = custom_color if custom_color else _theme.BG_SEP
            txt_col = _theme.contrasting_text_color(base_bg)

        _is_hover_sep = (i == self._hover_idx and self._drag_idx < 0)
        if is_sel_row:
            row_bg = BG_SELECT
        elif (not is_synthetic or is_overwrite) and i in conflict_sep_higher:
            txt_col = _theme.contrasting_text_color(_theme.conflict_higher)
            row_bg = _theme.hover_tint(_theme.conflict_higher) if _is_hover_sep else _theme.conflict_higher
        elif (not is_synthetic or is_overwrite) and i in conflict_sep_lower:
            txt_col = _theme.contrasting_text_color(_theme.conflict_lower)
            row_bg = _theme.hover_tint(_theme.conflict_lower) if _is_hover_sep else _theme.conflict_lower
        elif not is_synthetic and i == highlighted_sep_idx:
            txt_col = _theme.contrasting_text_color(_theme.plugin_separator)
            row_bg = _theme.hover_tint(_theme.plugin_separator) if _is_hover_sep else _theme.plugin_separator
        elif _is_hover_sep:
            row_bg = _theme.hover_tint(base_bg)
        else:
            row_bg = base_bg
        return base_bg, row_bg, txt_col

    def _mod_row_colors(self, i: int, row: int, entry, sel_entry,
                        conflict_mod_higher: set[str], conflict_mod_lower: set[str],
                        conflict_sel_higher: set[str], conflict_sel_lower: set[str],
                        ) -> tuple[str, str]:
        """Resolve a mod row's colours → (base_row_bg, bg).

        base_row_bg is the colour the row would have without hover (used for
        text-contrast so hover tinting doesn't flip text colour); bg adds the
        hover tint on top.
        """
        is_sel = (i in self._sel_set) or (i == self._drag_idx)
        _is_hover = (i == self._hover_idx and self._drag_idx < 0)
        if is_sel:
            base_row_bg = BG_SELECT
        elif entry.name == self._highlighted_mod:
            base_row_bg = _theme.plugin_mod
        elif sel_entry and (not sel_entry.is_separator
                            or sel_entry.name == OVERWRITE_NAME):
            if entry.name in conflict_sel_higher:
                base_row_bg = _theme.conflict_higher
            elif entry.name in conflict_sel_lower:
                base_row_bg = _theme.conflict_lower
            else:
                base_row_bg = BG_ROW if row % 2 == 0 else BG_ROW_ALT
        elif entry.name in conflict_mod_higher:
            base_row_bg = _theme.conflict_higher
        elif entry.name in conflict_mod_lower:
            base_row_bg = _theme.conflict_lower
        else:
            base_row_bg = BG_ROW if row % 2 == 0 else BG_ROW_ALT

        if _is_hover and not is_sel:
            # Coloured rows: tint base. Plain rows: use the flat hover colour.
            if base_row_bg in (BG_ROW, BG_ROW_ALT):
                bg = BG_HOVER_ROW
            else:
                bg = _theme.hover_tint(base_row_bg)
        else:
            bg = base_row_bg
        return base_row_bg, bg

    def _highlighted_sep_index(self) -> int:
        """Index of the collapsed separator holding _highlighted_mod, or -1."""
        if not self._highlighted_mod:
            return -1
        _hi_sep_idx = self._sep_idx_for_mod(self._highlighted_mod)
        if (0 <= _hi_sep_idx < len(self._entries)
                and self._entries[_hi_sep_idx].name in self._collapsed_seps):
            return _hi_sep_idx
        return -1

    def _repaint_hover_rows(self, *indices: int) -> None:
        """Repaint only the hover-dependent fills of the slots showing *indices*.

        Hover changes just the row background (and the fills painted with it)
        of at most two rows, so the full _redraw() — every pool slot plus the
        marker strip — is skipped on mouse moves.
        """
        c = self._canvas
        sel_entry = (self._entries[self._sel_idx]
                     if 0 <= self._sel_idx < len(self._entries) else None)
        highlights = None
        highlighted_sep_idx = -1
        first_row = max(0, int(c.canvasy(0)) // self.ROW_H)
        for i in indices:
            if i < 0 or i >= len(self._entries):
                continue
            try:
                s = self._pool_data_idx.index(i)
            except ValueError:
                continue
            if highlights is None:
                highlights = self._compute_conflict_highlights(sel_entry)
                highlighted_sep_idx = self._highlighted_sep_index()
            (sep_higher, sep_lower, mod_higher, mod_lower,
             sel_higher, sel_lower) = highlights
            entry = self._entries[i]
            if entry.is_separator:
                _, row_bg, _ = self._sep_row_colors(
                    i, entry, sep_higher, sep_lower, highlighted_sep_idx)
                c.itemconfigure(self._pool_bg[s], fill=row_bg)
                # Unlocked separator lock checkboxes are filled with the row bg.
                if (entry.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
                        and not self._sep_locks.get(entry.name, False)):
                    rect_id = self._lock_cb_rects.get(entry.name)
                    if rect_id is not None:
                        c.itemconfigure(rect_id, fill=row_bg)
            else:
                _, bg = self._mod_row_colors(
                    i, first_row + s, entry, sel_entry,
                    mod_higher, mod_lower, sel_higher, sel_lower)
                c.itemconfigure(self._pool_bg[s], fill=bg)
                # Checkbox hit-rect / unchecked box is filled with the row bg.
                if i < len(self._check_vars) and self._check_vars[i] is not None:
                    if (self._bundle_name_of(i) is not None
                            or not self._check_vars[i].get()):
                        c.itemconfigure(self._pool_cb_rect[s], fill=bg)
            # Direct poke — force a full repaint of this slot on the next _redraw.
            self._pool_last_state[s] = None

    # Pool item attribute names that get hidden together when a slot leaves
    # the viewport. Listed once so _hide_pool_slot stays a one-line loop.
    _POOL_HIDE_ITEMS = (
        "_pool_bg", "_pool_name",
        "_pool_flag_icon", "_pool_flag_icon2", "_pool_flag_icon3",
        "_pool_flag_icon4", "_pool_flag_icon5", "_pool_flag_star",
        "_pool_conflict_icon1", "_pool_conflict_icon2",
        "_pool_bsa_dot1", "_pool_bsa_dot2", "_pool_bsa_sep",
        "_pool_category_text", "_pool_install_text",
        "_pool_priority_text", "_pool_version_text", "_pool_size_text",
        "_pool_sep_icon", "_pool_sep_line_l", "_pool_sep_line_r",
        "_pool_sep_badge",
        "_pool_cb_rect", "_pool_cb_mark",
    )

    def _aggregate_block_conflicts(self, sep_idx: int) -> tuple[int, int]:
        """Return (loose_aggregate, bsa_aggregate) CONFLICT_* constants for the
        block under separator `sep_idx`. Aggregate goes WINS / LOSES /
        PARTIAL / NONE — never FULL, since FULL describes a single mod's
        per-file resolution and doesn't compose across mods."""
        wins = loses = bsa_wins = bsa_loses = False
        for bi in self._sep_block_range(sep_idx):
            be = self._entries[bi]
            if be.is_separator:
                continue
            bc = self._conflict_map.get(be.name, CONFLICT_NONE)
            if bc in (CONFLICT_WINS, CONFLICT_PARTIAL, CONFLICT_FULL):
                wins = True
            if bc in (CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL):
                loses = True
            bbc = self._bsa_conflict_map.get(be.name, CONFLICT_NONE)
            if bbc in (CONFLICT_WINS, CONFLICT_PARTIAL, CONFLICT_FULL):
                bsa_wins = True
            if bbc in (CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL):
                bsa_loses = True

        def _reduce(w: bool, l: bool) -> int:
            if w and l: return CONFLICT_PARTIAL
            if w:       return CONFLICT_WINS
            if l:       return CONFLICT_LOSES
            return CONFLICT_NONE

        return _reduce(wins, loses), _reduce(bsa_wins, bsa_loses)

    def _render_conflict_cell(self, s: int, conflict: int, bsa_conflict: int,
                              conf_x: int, conf_w: int, y_mid: int) -> None:
        """Render the loose / BSA conflict icons for pool slot `s`.

        When both groups are present they're split around the column centre
        with a thin vertical separator line drawn between them.
        """
        c = self._canvas
        cx_center = conf_x + conf_w // 2
        has_loose = conflict != CONFLICT_NONE
        has_bsa = bsa_conflict != CONFLICT_NONE

        if has_loose and has_bsa:
            sep_x = cx_center
            gap = scaled(6)         # visible gap between icon edge and separator
            icon_half = scaled(12)  # half-width of a conflict icon
            loose_cx = sep_x - gap - icon_half
            bsa_cx = sep_x + gap + icon_half
        else:
            loose_cx = cx_center
            bsa_cx = cx_center

        loose_icon = None
        if conflict == CONFLICT_WINS:
            loose_icon = self._icon_plus
        elif conflict == CONFLICT_LOSES:
            loose_icon = self._icon_minus
        elif conflict == CONFLICT_PARTIAL:
            loose_icon = self._icon_conflict_mixed
        elif conflict == CONFLICT_FULL:
            loose_icon = self._icon_cross
        if loose_icon is not None:
            c.coords(self._pool_conflict_icon1[s], loose_cx, y_mid)
            c.itemconfigure(self._pool_conflict_icon1[s],
                            image=loose_icon, state="normal")
        else:
            c.itemconfigure(self._pool_conflict_icon1[s], state="hidden")
        c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")

        bsa_icon = None
        if has_bsa:
            if bsa_conflict == CONFLICT_WINS:
                bsa_icon = self._icon_bsa_winner
            elif bsa_conflict == CONFLICT_LOSES:
                bsa_icon = self._icon_bsa_loser
            elif bsa_conflict == CONFLICT_PARTIAL:
                bsa_icon = self._icon_bsa_mixed
            elif bsa_conflict == CONFLICT_FULL:
                bsa_icon = self._icon_bsa_redundant
        if bsa_icon is not None:
            c.coords(self._pool_bsa_dot1[s], bsa_cx, y_mid)
            c.itemconfigure(self._pool_bsa_dot1[s],
                            image=bsa_icon, state="normal")
        else:
            c.itemconfigure(self._pool_bsa_dot1[s], state="hidden")
        c.itemconfigure(self._pool_bsa_dot2[s], state="hidden")

        if has_loose and has_bsa:
            sh = scaled(7)
            c.coords(self._pool_bsa_sep[s],
                     sep_x, y_mid - sh, sep_x, y_mid + sh)
            c.itemconfigure(self._pool_bsa_sep[s], fill=BORDER, state="normal")
            c.tag_raise(self._pool_bsa_sep[s])
        else:
            c.itemconfigure(self._pool_bsa_sep[s], state="hidden")

    def _hide_pool_slot(self, s: int) -> None:
        """Hide every canvas item belonging to pool slot `s` and mark it free."""
        c = self._canvas
        for attr in self._POOL_HIDE_ITEMS:
            c.itemconfigure(getattr(self, attr)[s], state="hidden")
        self._pool_data_idx[s] = -1

    def _render_flag_strip(self, s: int, flags: list,
                           flag_x: int, flag_w: int, y_mid: int) -> None:
        """Render a centred row of flag icons into pool slot `s`.

        `flags` is an ordered list of either ("img", image) or ("star",) tuples.
        The pool exposes 5 image slots + 1 star slot; image flags past slot 5
        are silently dropped (callers are expected to cap before calling). Used
        by both regular mod rows and collapsed-separator aggregate rows.
        """
        c = self._canvas
        SPACING = scaled(22)
        img_slots = (
            self._pool_flag_icon[s],
            self._pool_flag_icon2[s],
            self._pool_flag_icon3[s],
            self._pool_flag_icon4[s],
            self._pool_flag_icon5[s],
        )
        star_slot = self._pool_flag_star[s]
        n = len(flags)
        if n > 0:
            group_w = (n - 1) * SPACING
            fx_start = flag_x + flag_w // 2 - group_w // 2
        else:
            fx_start = flag_x + flag_w // 2
        img_idx = 0
        star_placed = False
        for fi, flag in enumerate(flags):
            fx = fx_start + fi * SPACING
            if flag[0] == "star":
                c.coords(star_slot, fx, y_mid)
                c.itemconfigure(star_slot, state="normal")
                star_placed = True
            elif img_idx < len(img_slots):
                slot_id = img_slots[img_idx]
                c.coords(slot_id, fx, y_mid)
                c.itemconfigure(slot_id, image=flag[1], state="normal")
                img_idx += 1
        for si in range(img_idx, len(img_slots)):
            c.itemconfigure(img_slots[si], state="hidden")
        if not star_placed:
            c.itemconfigure(star_slot, state="hidden")

    def _redraw(self):
        """Pool-based redraw: reconfigure pre-allocated canvas items for the visible viewport.

        No delete("all") — items outside the viewport are hidden via state="hidden".
        This is the same technique used by the plugin panel for smooth scrolling.
        """
        self._redraw_after_id = None
        self._flag_tip_cache = None  # row data may have changed — rebuild on next hover
        c = self._canvas
        cw = self._canvas_w

        # Pre-compute column x/w for each data col (respects reorder).
        # Cache _col_pos.get() once per column — was called twice per column per redraw.
        _col_pos = self._col_pos
        _COL_X = self._COL_X
        _COL_W = self._COL_W
        _cp2 = _col_pos.get(2, 2); _CAT_X  = _COL_X[_cp2]; _CAT_W  = _COL_W[_cp2]
        _cp3 = _col_pos.get(3, 3); _FLAG_X = _COL_X[_cp3]; _FLAG_W = _COL_W[_cp3]
        _cp4 = _col_pos.get(4, 4); _CONF_X = _COL_X[_cp4]; _CONF_W = _COL_W[_cp4]
        _cp5 = _col_pos.get(5, 5); _INST_X = _COL_X[_cp5]; _INST_W = _COL_W[_cp5]
        _cp6 = _col_pos.get(6, 6); _PRIO_X = _COL_X[_cp6]; _PRIO_W = _COL_W[_cp6]
        _cp7 = _col_pos.get(7, 7); _VER_X  = _COL_X[_cp7]; _VER_W  = _COL_W[_cp7]
        _cp8 = _col_pos.get(8, 8); _SIZE_X = _COL_X[_cp8]; _SIZE_W = _COL_W[_cp8]

        # Hoist Path.home() and frequently-used scaled() constants out of the per-row loop.
        _home = Path.home()
        _SCALED_4 = scaled(4)
        _SCALED_7 = scaled(7)
        _SCALED_8 = scaled(8)
        _SCALED_28 = scaled(28)

        # Pre-compute font tuples (avoid re-creating inside the inner loop)
        _FONT_NAME = (_theme.FONT_FAMILY, _theme.FS11)
        _FONT_SEP_BOLD = (_theme.FONT_FAMILY, _theme.FS10, "bold")
        _FONT_SMALL = (_theme.FONT_FAMILY, _theme.FS10)
        _FONT_TINY = (_theme.FONT_FAMILY, _theme.FS9)
        _FONT_CHECK = (_theme.FONT_FAMILY, int(_theme.FS13 * 1.25), "bold")
        _FONT_RADIO = (_theme.FONT_FAMILY, int(_theme.FS13 * 1.25), "bold")
        _FONT_STAR = (_theme.FONT_FAMILY, _theme.FS11)

        canvas_top = int(c.canvasy(0))
        canvas_h = c.winfo_height()

        # Tall viewports (4K monitors, low UI scale) can hold more rows than
        # the default pool — grow it so rows past the last slot still render.
        self._ensure_pool_capacity(canvas_h // self.ROW_H + 3)

        # Recompute priorities cache only when _entries has changed.
        # Must happen before _compute_visible_indices so priority-column sort works.
        self._ensure_priorities()

        # Recompute visible indices only when something structural changed.
        if self._vis_dirty:
            self._visible_indices = self._compute_visible_indices()
            self._vis_dirty = False

        priorities = self._priorities

        vis = self._visible_indices

        n = len(vis)
        total_h = n * self.ROW_H
        row_h = self.ROW_H

        # If the viewport now sits past the end of the (possibly shrunken) content
        # — e.g. after collapse-all or a filter that hides rows — clamp the scroll
        # position so rows remain visible instead of going blank until the user scrolls.
        max_top = max(0, total_h - canvas_h)
        if canvas_top > max_top:
            c.configure(scrollregion=(0, 0, self._canvas_w, max(total_h, canvas_h)))
            c.yview_moveto(max_top / max(total_h, canvas_h, 1))
            canvas_top = max_top

        # Viewport slice: only reconfigure pool slots for visible rows.
        first_row = max(0, canvas_top // row_h)
        last_row  = min(n, (canvas_top + canvas_h) // row_h + 2)
        vis_count = last_row - first_row

        sel_entry = (self._entries[self._sel_idx]
                     if 0 <= self._sel_idx < len(self._entries) else None)

        (conflict_sep_higher, conflict_sep_lower,
         conflict_mod_higher, conflict_mod_lower,
         conflict_sel_higher, conflict_sel_lower) = self._compute_conflict_highlights(sel_entry)

        highlighted_sep_idx: int = self._highlighted_sep_index()

        # Track which lock canvas items were repositioned this frame
        _visited_lock_keys: set[str] = set()

        # Per-slot render-state keys: a mod-row slot whose key is unchanged
        # skips all its canvas calls (same technique as plugin_panel._predraw).
        # Separator rows always re-render — their collapsed-block aggregates
        # depend on too many inputs to key cheaply, and few are visible at once.
        _pool_last_state = self._pool_last_state
        _layout_key = (cw, _COL_X[0], _COL_X[1], _COL_W[1],
                       _CAT_X, _CAT_W, _FLAG_X, _FLAG_W, _CONF_X, _CONF_W,
                       _INST_X, _INST_W, _PRIO_X, _PRIO_W, _VER_X, _VER_W,
                       _SIZE_X, _SIZE_W)

        # Reconfigure pool slots
        for s in range(self._pool_size):
            row = first_row + s
            if s < vis_count and row < n:
                i = vis[row]
                # BOUNDARY_ROW is a render-only sentinel (static divider); it is
                # not a valid _entries index, so resolve it to the standalone
                # boundary entry.  The drag divider is a real entry with the same
                # name, so both fall into the same render branch below.
                entry = self._static_boundary if i == BOUNDARY_ROW else self._entries[i]
                y_top = row * row_h
                y_bot = y_top + row_h
                y_mid = y_top + row_h // 2

                self._pool_data_idx[s] = i

                if entry.name == BOUNDARY_NAME:
                    # Transient ungrouped-float boundary: render a thin centered
                    # divider line, nothing else.  Always repaint (cheap, rare).
                    self._hide_pool_slot(s)
                    self._pool_data_idx[s] = i
                    _line_col = _theme.hover_tint(BG_DEEP, 50)
                    c.coords(self._pool_sep_line_l[s],
                             _SCALED_8, y_mid, cw - _SCALED_8, y_mid)
                    c.itemconfigure(self._pool_sep_line_l[s],
                                    fill=_line_col, state="normal", dash=(3, 3))
                    _pool_last_state[s] = None
                    continue

                if entry.is_separator:
                    is_overwrite   = (entry.name == OVERWRITE_NAME)
                    is_root_folder = (entry.name == ROOT_FOLDER_NAME)
                    is_synthetic   = is_overwrite or is_root_folder
                    _pool_last_state[s] = None

                    base_bg, row_bg, txt_col = self._sep_row_colors(
                        i, entry, conflict_sep_higher, conflict_sep_lower,
                        highlighted_sep_idx)

                    # Background rectangle
                    c.coords(self._pool_bg[s], 0, y_top, cw, y_bot)
                    c.itemconfigure(self._pool_bg[s], fill=row_bg, outline="", state="normal")

                    # Separator label (pool name item, centred, bold)
                    if is_overwrite:
                        label = "Overwrite"
                    elif is_root_folder:
                        label = "Root Folder"
                    else:
                        label = entry.display_name

                    mid_x     = _COL_X[1] + _COL_W[1] // 2
                    lock_w    = _SCALED_28 if not is_synthetic else 0
                    _badge_info = self._sep_deploy_paths.get(entry.name, {}) if not is_synthetic else {}
                    # Determine type once; extract path/raw fields without repeated isinstance checks.
                    if isinstance(_badge_info, dict):
                        _deploy_path = _badge_info.get("path", "")
                        _is_raw_deploy = _badge_info.get("raw", False)
                    elif _badge_info:
                        _deploy_path = str(_badge_info)
                        _is_raw_deploy = False
                    else:
                        _deploy_path = ""
                        _is_raw_deploy = False
                    has_badge = bool(_deploy_path or _is_raw_deploy)
                    left_edge = scaled(32) if is_root_folder else (scaled(20) if not is_synthetic else _SCALED_8)
                    label_len = len(label)

                    if has_badge:
                        # Left-aligned layout: label + path badge flowing from left edge
                        label_x = left_edge + _SCALED_4
                        c.coords(self._pool_name[s], label_x, y_mid)
                        c.itemconfigure(self._pool_name[s], text=label, anchor="w",
                                        fill=txt_col, font=_FONT_SEP_BOLD, state="normal")
                        # Approximate bold label width at FS10 (~7px per char)
                        badge_x = label_x + label_len * _SCALED_7 + _SCALED_8
                        if _deploy_path:
                            try:
                                _dp = Path(_deploy_path)
                                badge_path = "~/" + str(_dp.relative_to(_home)) if _dp.is_relative_to(_home) else _deploy_path
                            except Exception:
                                badge_path = _deploy_path
                            badge_text = f"⇒ {badge_path}  [raw]" if _is_raw_deploy else f"⇒ {badge_path}"
                        else:
                            badge_text = "[raw deploy]"
                        c.coords(self._pool_sep_badge[s], badge_x, y_mid)
                        c.itemconfigure(self._pool_sep_badge[s], text=badge_text,
                                        fill=TONE_BLUE_SOFT, state="normal")
                        c.itemconfigure(self._pool_sep_line_l[s], state="hidden")
                        c.itemconfigure(self._pool_sep_line_r[s], state="hidden")
                    else:
                        # Centered label with flanking decorative lines (default)
                        c.coords(self._pool_name[s], mid_x, y_mid)
                        c.itemconfigure(self._pool_name[s], text=label, anchor="center",
                                        fill=txt_col, font=_FONT_SEP_BOLD, state="normal")
                        c.itemconfigure(self._pool_sep_badge[s], state="hidden")
                        text_pad     = scaled(6)
                        label_hw     = label_len * _SCALED_4 + text_pad
                        right_edge   = cw - lock_w - _SCALED_8
                        sep_line_col = _theme.hover_tint(base_bg, 35)
                        c.coords(self._pool_sep_line_l[s],
                                 left_edge, y_mid, mid_x - label_hw, y_mid)
                        c.itemconfigure(self._pool_sep_line_l[s], fill=sep_line_col, state="normal")
                        c.coords(self._pool_sep_line_r[s],
                                 mid_x + label_hw, y_mid, right_edge, y_mid)
                        c.itemconfigure(self._pool_sep_line_r[s], fill=sep_line_col, state="normal")

                    # Collapse icon (real separators only)
                    if not is_synthetic:
                        if entry.name in self._collapsed_seps:
                            icon = self._icon_sep_right
                            fallback = "▶"
                        else:
                            icon = self._icon_sep_arrow
                            fallback = "▼"
                        if icon:
                            c.coords(self._pool_sep_icon[s], scaled(10), y_mid)
                            c.itemconfigure(self._pool_sep_icon[s],
                                            image=icon, state="normal")
                        else:
                            # No image — use a text item; reuse pool_name won't work here,
                            # so just hide the image slot and draw nothing extra.
                            c.itemconfigure(self._pool_sep_icon[s], state="hidden")
                    else:
                        c.itemconfigure(self._pool_sep_icon[s], state="hidden")

                    # Overwrite row conflict icons in conflict column — always wins only
                    _sep_is_collapsed = (not is_synthetic
                                         and entry.name in self._collapsed_seps)
                    if is_overwrite and self._overrides.get(OVERWRITE_NAME):
                        cx = _CONF_X + _CONF_W // 2
                        c.itemconfigure(self._pool_conflict_icon1[s], state="hidden")
                        if self._icon_plus:
                            c.coords(self._pool_conflict_icon2[s], cx, y_mid)
                            c.itemconfigure(self._pool_conflict_icon2[s],
                                            image=self._icon_plus, state="normal")
                        else:
                            c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_dot1[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_dot2[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_sep[s], state="hidden")
                        # Show "Modified in Mod Files tab" flag for Overwrite if applicable
                        if (self._mod_is_modified_in_mf(OVERWRITE_NAME)
                                and self._icon_disabled_files):
                            c.coords(self._pool_flag_icon[s],
                                     _FLAG_X + _FLAG_W // 2, y_mid)
                            c.itemconfigure(self._pool_flag_icon[s],
                                            image=self._icon_disabled_files,
                                            state="normal")
                        else:
                            c.itemconfigure(self._pool_flag_icon[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon2[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon3[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon4[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon5[s], state="hidden")
                        c.itemconfigure(self._pool_flag_star[s], state="hidden")
                    elif is_overwrite:
                        # No conflict overrides — still show modified flag if present.
                        c.itemconfigure(self._pool_conflict_icon1[s], state="hidden")
                        c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_dot1[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_dot2[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_sep[s], state="hidden")
                        if (self._mod_is_modified_in_mf(OVERWRITE_NAME)
                                and self._icon_disabled_files):
                            c.coords(self._pool_flag_icon[s],
                                     _FLAG_X + _FLAG_W // 2, y_mid)
                            c.itemconfigure(self._pool_flag_icon[s],
                                            image=self._icon_disabled_files,
                                            state="normal")
                        else:
                            c.itemconfigure(self._pool_flag_icon[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon2[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon3[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon4[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon5[s], state="hidden")
                        c.itemconfigure(self._pool_flag_star[s], state="hidden")
                    elif _sep_is_collapsed:
                        # Aggregate the block's per-mod conflict state and render it.
                        _blk = self._sep_block_range(i)
                        _agg_conflict, _agg_bsa = self._aggregate_block_conflicts(i)
                        self._render_conflict_cell(
                            s, _agg_conflict, _agg_bsa, _CONF_X, _CONF_W, y_mid,
                        )

                        # Aggregate flags from mods in the block (deduplicated by type)
                        _agg_flags: list = []
                        _seen_flag = set()  # track which flag types we've added
                        _any_locked = False
                        for _bi in _blk:
                            _be = self._entries[_bi]
                            if _be.is_separator:
                                continue
                            if (_be.name in self._mod_notes_map and self._icon_note
                                    and "note" not in _seen_flag):
                                _agg_flags.append(("img", self._icon_note))
                                _seen_flag.add("note")
                            _has_missing = (_be.name in self._missing_reqs
                                           and _be.name not in self._ignored_missing_reqs)
                            if _has_missing and self._icon_warning and "warning" not in _seen_flag:
                                _agg_flags.append(("img", self._icon_warning))
                                _seen_flag.add("warning")
                            if _be.locked:
                                _any_locked = True
                            if _be.name in self._update_mods and self._icon_update and "update" not in _seen_flag:
                                _agg_flags.append(("img", self._icon_update))
                                _seen_flag.add("update")
                            if _be.name in self._endorsed_mods and self._icon_endorsed and "endorsed" not in _seen_flag:
                                _agg_flags.append(("img", self._icon_endorsed))
                                _seen_flag.add("endorsed")
                            if _be.name in self._prertx_mods and self._icon_info and "info" not in _seen_flag:
                                _agg_flags.append(("img", self._icon_info))
                                _seen_flag.add("info")
                            elif ((_be.name in self._collection_bundled_mods
                                   or _be.name in self._collection_patched_mods)
                                  and self._icon_info and "info" not in _seen_flag):
                                _agg_flags.append(("img", self._icon_info))
                                _seen_flag.add("info")
                            if self._mod_is_modified_in_mf(_be.name) and self._icon_disabled_files and "disabled" not in _seen_flag:
                                _agg_flags.append(("img", self._icon_disabled_files))
                                _seen_flag.add("disabled")
                            if (_be.name in self._xedit_modified_mods
                                    and self._icon_xedit and "xedit" not in _seen_flag):
                                _agg_flags.append(("img", self._icon_xedit))
                                _seen_flag.add("xedit")
                            if ((_be.name in self._root_folder_mods
                                 or _be.name in self._root_rule_mods)
                                and self._icon_root_folder and "root" not in _seen_flag):
                                _agg_flags.append(("img", self._icon_root_folder))
                                _seen_flag.add("root")
                        # Only 5 image slots exist in the pool — cap before the
                        # star insert so later img flags can't push earlier ones
                        # off the end of the renderer.
                        if len(_agg_flags) > 5:
                            _agg_flags = _agg_flags[:5]
                        # Insert star after warning if any locked mod exists
                        if _any_locked and "star" not in _seen_flag:
                            _ins = 1 if (_agg_flags and _agg_flags[0][0] == "img"
                                         and "warning" in _seen_flag) else 0
                            _agg_flags.insert(_ins, ("star",))
                            _seen_flag.add("star")

                        # Render aggregated flags (same layout as mod rows)
                        self._render_flag_strip(s, _agg_flags, _FLAG_X, _FLAG_W, y_mid)
                    else:
                        c.itemconfigure(self._pool_conflict_icon1[s], state="hidden")
                        c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_dot1[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_dot2[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_sep[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon2[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon3[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon4[s], state="hidden")
                        c.itemconfigure(self._pool_flag_icon5[s], state="hidden")
                        c.itemconfigure(self._pool_flag_star[s], state="hidden")

                    # Hide other mod-only items on separators
                    c.itemconfigure(self._pool_category_text[s], state="hidden")
                    c.itemconfigure(self._pool_install_text[s], state="hidden")
                    c.itemconfigure(self._pool_priority_text[s], state="hidden")
                    c.itemconfigure(self._pool_version_text[s], state="hidden")
                    c.itemconfigure(self._pool_size_text[s], state="hidden")

                    # Pool check widget — hidden for separators
                    c.itemconfigure(self._pool_cb_rect[s], state="hidden")
                    c.itemconfigure(self._pool_cb_mark[s], state="hidden")

                    # Canvas-drawn lock / root-folder enable checkbox (no tk widget —
                    # canvas items scroll in sync automatically).
                    if is_root_folder:
                        rf_key = ROOT_FOLDER_NAME
                        _visited_lock_keys.add(rf_key)
                        checked_rf = entry.enabled
                        cb_cx = self._COL_X[0] + scaled(12)
                        cb_size = scaled(18)
                        x1, y1 = cb_cx - cb_size // 2, y_mid - cb_size // 2
                        x2, y2 = cb_cx + cb_size // 2, y_mid + cb_size // 2
                        if rf_key not in self._lock_cb_rects:
                            lk_tag = "lock_cb_root"
                            rect_id = c.create_rectangle(
                                x1, y1, x2, y2,
                                outline=BORDER, width=1,
                                fill=BG_DEEP if checked_rf else base_bg,
                                tags=(lk_tag, "lock_cb"),
                            )
                            mark_id = c.create_text(
                                cb_cx, y_mid, text="✓", anchor="center",
                                fill=ACCENT, font=_FONT_CHECK,
                                state="normal" if checked_rf else "hidden",
                                tags=(lk_tag, "lock_cb"),
                            )
                            self._lock_cb_rects[rf_key] = rect_id
                            self._lock_cb_marks[rf_key] = mark_id
                            c.tag_bind(lk_tag, "<ButtonRelease-1>",
                                       lambda e: self._on_root_folder_toggle())
                            c.tag_bind(lk_tag, "<Enter>",
                                       lambda e: c.config(cursor="hand2"))
                            c.tag_bind(lk_tag, "<Leave>",
                                       lambda e: c.config(cursor=""))
                        else:
                            rect_id = self._lock_cb_rects[rf_key]
                            mark_id = self._lock_cb_marks[rf_key]
                            c.coords(rect_id, x1, y1, x2, y2)
                            c.itemconfigure(rect_id, fill=BG_DEEP if checked_rf else base_bg)
                            c.coords(mark_id, cb_cx, y_mid)
                            c.itemconfigure(mark_id,
                                            state="normal" if checked_rf else "hidden")
                    elif not is_synthetic:
                        sname = entry.name
                        _visited_lock_keys.add(sname)
                        locked_state = self._sep_locks.get(sname, False)
                        lk_x = cw - lock_w - scaled(8) + lock_w // 2
                        cb_size2 = scaled(14)
                        x1, y1 = lk_x - cb_size2 // 2, y_mid - cb_size2 // 2
                        x2, y2 = lk_x + cb_size2 // 2, y_mid + cb_size2 // 2
                        if sname not in self._lock_cb_rects:
                            lk_tag2 = f"lock_cb_{len(self._lock_cb_rects)}"
                            rect2_id = c.create_rectangle(
                                x1, y1, x2, y2,
                                outline=BORDER, width=1,
                                fill=BG_DEEP if locked_state else row_bg,
                                tags=(lk_tag2, "lock_cb"),
                            )
                            if self._icon_lock:
                                mark2_id = c.create_image(
                                    lk_x, y_mid, anchor="center",
                                    image=self._icon_lock,
                                    state="normal" if locked_state else "hidden",
                                    tags=(lk_tag2, "lock_cb"),
                                )
                            else:
                                mark2_id = c.create_text(
                                    lk_x, y_mid, text="🔒", anchor="center",
                                    fill=TEXT_SEP, font=_FONT_TINY,
                                    state="normal" if locked_state else "hidden",
                                    tags=(lk_tag2, "lock_cb"),
                                )
                            self._lock_cb_rects[sname] = rect2_id
                            self._lock_cb_marks[sname] = mark2_id
                            c.tag_bind(lk_tag2, "<ButtonRelease-1>",
                                       lambda e, r=rect2_id: self._on_sep_lock_click(r))
                            c.tag_bind(lk_tag2, "<Enter>",
                                       lambda e: c.config(cursor="hand2"))
                            c.tag_bind(lk_tag2, "<Leave>",
                                       lambda e: c.config(cursor=""))
                        else:
                            rect2_id = self._lock_cb_rects[sname]
                            mark2_id = self._lock_cb_marks[sname]
                            c.coords(rect2_id, x1, y1, x2, y2)
                            c.itemconfigure(rect2_id,
                                            fill=BG_DEEP if locked_state else row_bg)
                            c.coords(mark2_id, lk_x, y_mid)
                            c.itemconfigure(mark2_id,
                                            state="normal" if locked_state else "hidden")

                else:
                    # --- Regular mod row ---
                    base_row_bg, bg = self._mod_row_colors(
                        i, row, entry, sel_entry,
                        conflict_mod_higher, conflict_mod_lower,
                        conflict_sel_higher, conflict_sel_lower)

                    # Gather every input this row renders from (cheap dict/set
                    # lookups) so an unchanged slot can skip its canvas calls.
                    _bname_valid = self._bundle_name_of(i)
                    is_bundle_variant = _bname_valid is not None
                    _display_label = (self._variant_name_of(i)
                                      if is_bundle_variant else entry.name)
                    cat_text = self._category_names.get(entry.name, "")

                    # Flags column — collect ordered list of icons/items to show side by side.
                    # Each flag is a tuple: ("img", image_obj) or ("star",).
                    # Note flag goes first so it's leftmost and never elided by the 5-slot cap.
                    _flags: list = []
                    if entry.name in self._mod_notes_map and self._icon_note:
                        _flags.append(("img", self._icon_note))
                    if entry.name in self._bundle_mods and self._icon_settings:
                        _flags.append(("img", self._icon_settings))
                    has_missing = (entry.name in self._missing_reqs
                                   and entry.name not in self._ignored_missing_reqs)
                    if has_missing and self._icon_warning:
                        _flags.append(("img", self._icon_warning))
                    if entry.locked:
                        _flags.append(("star",))
                    if entry.name in self._update_mods and self._icon_update:
                        _flags.append(("img", self._icon_update))
                    if entry.name in self._endorsed_mods and self._icon_endorsed:
                        _flags.append(("img", self._icon_endorsed))
                    if entry.name in self._prertx_mods and self._icon_info:
                        _flags.append(("img", self._icon_info))
                    elif (entry.name in self._collection_bundled_mods
                          or entry.name in self._collection_patched_mods) and self._icon_info:
                        # Reuse the info icon for collection-tagged mods; tooltip
                        # text below distinguishes the two cases. Only one info
                        # icon per mod (mutually exclusive with prertx render).
                        _flags.append(("img", self._icon_info))
                    if self._mod_is_modified_in_mf(entry.name) and self._icon_disabled_files:
                        _flags.append(("img", self._icon_disabled_files))
                    if entry.name in self._xedit_modified_mods and self._icon_xedit:
                        _flags.append(("img", self._icon_xedit))
                    if ((entry.name in self._root_folder_mods
                         or entry.name in self._root_rule_mods)
                        and self._icon_root_folder):
                        _flags.append(("img", self._icon_root_folder))

                    conflict = self._conflict_map.get(entry.name, CONFLICT_NONE)
                    bsa_conflict = self._bsa_conflict_map.get(entry.name, CONFLICT_NONE)
                    install_text = self._install_dates.get(entry.name, "")
                    prio_text = str(priorities.get(i, ""))
                    version_text = self._mod_versions.get(entry.name, "")
                    size_text = self._mod_sizes.get(entry.name, "")
                    _has_cb = i < len(self._check_vars) and self._check_vars[i] is not None
                    checked = self._check_vars[i].get() if _has_cb else None

                    # Skip all canvas calls when nothing this slot renders changed
                    # (same technique as plugin_panel._predraw).
                    state_key = (
                        "m", i, row, _layout_key, entry.name, entry.enabled,
                        entry.locked, bg, base_row_bg, _display_label, cat_text,
                        tuple(_flags), conflict, bsa_conflict, install_text,
                        prio_text, version_text, size_text, checked,
                    )
                    if _pool_last_state[s] == state_key:
                        continue

                    # Background
                    c.coords(self._pool_bg[s], 0, y_top, cw, y_bot)
                    c.itemconfigure(self._pool_bg[s], fill=bg, outline="", state="normal")

                    # Name text (truncate if it would overlap the category column)
                    _theme_bgs = (_theme.conflict_higher, _theme.conflict_lower,
                                  _theme.plugin_mod, _theme.plugin_separator)
                    if base_row_bg in _theme_bgs:
                        name_color = _theme.contrasting_text_color(base_row_bg)
                    elif not entry.enabled:
                        name_color = TEXT_DIM
                    else:
                        name_color = TEXT_MAIN
                    name_font = _FONT_NAME
                    name_width = self._COL_W[1] - _SCALED_4
                    display_name = _truncate_text_for_width(c, _display_label, name_font, name_width)
                    c.coords(self._pool_name[s], self._COL_X[1], y_mid)
                    c.itemconfigure(self._pool_name[s], text=display_name, anchor="w",
                                    fill=name_color, font=name_font, state="normal")

                    # Hide separator-only items
                    c.itemconfigure(self._pool_sep_icon[s], state="hidden")
                    c.itemconfigure(self._pool_sep_line_l[s], state="hidden")
                    c.itemconfigure(self._pool_sep_line_r[s], state="hidden")
                    c.itemconfigure(self._pool_sep_badge[s], state="hidden")

                    # Category text (truncate if it would overlap the flags column)
                    if cat_text:
                        cat_font = _FONT_SMALL
                        cat_width = _CAT_W - scaled(4)
                        display_cat = _truncate_text_for_width(c, cat_text, cat_font, cat_width)
                        cat_cx = _CAT_X + _CAT_W // 2
                        c.coords(self._pool_category_text[s], cat_cx, y_mid)
                        c.itemconfigure(self._pool_category_text[s],
                                        text=display_cat, anchor="center",
                                        fill=TEXT_DIM, font=cat_font, state="normal")
                    else:
                        c.itemconfigure(self._pool_category_text[s], state="hidden")

                    self._render_flag_strip(s, _flags, _FLAG_X, _FLAG_W, y_mid)

                    # Conflict indicators: loose-file icons (left) + BSA dots (right of loose,
                    # separated by a thin vertical line when both are present).
                    if entry.locked:
                        c.itemconfigure(self._pool_conflict_icon1[s], state="hidden")
                        c.itemconfigure(self._pool_conflict_icon2[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_dot1[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_dot2[s], state="hidden")
                        c.itemconfigure(self._pool_bsa_sep[s], state="hidden")
                    else:
                        self._render_conflict_cell(
                            s, conflict, bsa_conflict, _CONF_X, _CONF_W, y_mid,
                        )

                    # Install date text
                    if install_text:
                        inst_cx = _INST_X + _INST_W // 2
                        c.coords(self._pool_install_text[s], inst_cx, y_mid)
                        c.itemconfigure(self._pool_install_text[s],
                                        text=install_text, anchor="center",
                                        fill=TEXT_DIM, font=_FONT_SMALL, state="normal")
                    else:
                        c.itemconfigure(self._pool_install_text[s], state="hidden")

                    # Priority text
                    prio_cx = _PRIO_X + _PRIO_W // 2
                    c.coords(self._pool_priority_text[s], prio_cx, y_mid)
                    c.itemconfigure(self._pool_priority_text[s],
                                    text=prio_text, anchor="center",
                                    fill=TEXT_DIM, font=_FONT_SMALL, state="normal")

                    # Version text
                    if version_text:
                        ver_font = _FONT_SMALL
                        ver_width = _VER_W - scaled(4)
                        display_ver = _truncate_text_for_width(c, version_text, ver_font, ver_width)
                        ver_cx = _VER_X + _VER_W // 2
                        c.coords(self._pool_version_text[s], ver_cx, y_mid)
                        c.itemconfigure(self._pool_version_text[s],
                                        text=display_ver, anchor="center",
                                        fill=TEXT_DIM, font=ver_font, state="normal")
                    else:
                        c.itemconfigure(self._pool_version_text[s], state="hidden")

                    # Size text
                    if size_text:
                        size_cx = _SIZE_X + _SIZE_W // 2
                        c.coords(self._pool_size_text[s], size_cx, y_mid)
                        c.itemconfigure(self._pool_size_text[s],
                                        text=size_text, anchor="center",
                                        fill=TEXT_DIM, font=_FONT_SMALL, state="normal")
                    else:
                        c.itemconfigure(self._pool_size_text[s], state="hidden")

                    # Enable/disable control (canvas-drawn)
                    # Bundle variants → radio circle (● / ○); normal mods → checkbox
                    if _has_cb:
                        self._pool_check_vars[s].set(checked)
                        cb_cx = self._COL_X[0] + scaled(12)
                        if is_bundle_variant:
                            # Invisible hit-area rect filled with the row bg colour so it
                            # still receives mouse events (fill="" / outline="" rects do not).
                            cb_size = scaled(18)
                            x1, y1 = cb_cx - cb_size // 2, y_mid - cb_size // 2
                            x2, y2 = cb_cx + cb_size // 2, y_mid + cb_size // 2
                            c.coords(self._pool_cb_rect[s], x1, y1, x2, y2)
                            c.itemconfigure(self._pool_cb_rect[s],
                                            fill=bg, outline="", state="normal")
                            c.coords(self._pool_cb_mark[s], cb_cx, y_mid)
                            c.itemconfigure(self._pool_cb_mark[s],
                                            text="●" if checked else "○",
                                            fill=ACCENT if checked else TEXT_DIM,
                                            font=_FONT_RADIO,
                                            state="normal")
                        else:
                            cb_size = scaled(18)
                            x1, y1 = cb_cx - cb_size // 2, y_mid - cb_size // 2
                            x2, y2 = cb_cx + cb_size // 2, y_mid + cb_size // 2
                            c.coords(self._pool_cb_rect[s], x1, y1, x2, y2)
                            fill = BG_DEEP if checked else bg
                            c.itemconfigure(self._pool_cb_rect[s],
                                            fill=fill, outline=BORDER, state="normal")
                            c.coords(self._pool_cb_mark[s], cb_cx, y_mid)
                            c.itemconfigure(self._pool_cb_mark[s],
                                            text="✓",
                                            fill=ACCENT,
                                            font=_FONT_CHECK,
                                            state="normal" if checked else "hidden")
                    else:
                        c.itemconfigure(self._pool_cb_rect[s], state="hidden")
                        c.itemconfigure(self._pool_cb_mark[s], state="hidden")

                    _pool_last_state[s] = state_key

            else:
                if _pool_last_state[s] != ("h",):
                    self._hide_pool_slot(s)
                    _pool_last_state[s] = ("h",)

        # Park lock canvas items for separators not in the current viewport.
        for key, rect_id in self._lock_cb_rects.items():
            if key not in _visited_lock_keys:
                c.coords(rect_id, 0, -200, 0, -200)
                c.coords(self._lock_cb_marks[key], 0, -200)

        # The drag overlay uses its own tagged items drawn on top
        c.configure(scrollregion=(0, 0, cw, max(total_h, canvas_h)))

        self._draw_marker_strip()

    def _draw_drag_overlay(self):
        """Draw a drag ghost under the cursor + a blue insertion line at the target slot."""
        self._canvas.delete("drag_overlay")
        if self._drag_idx < 0 or not self._entries:
            return

        cw = self._canvas_w
        gh = self.ROW_H

        # Build the list of entries to show in the ghost.
        # For collapsed separators, only show the separator itself (mods stay hidden).
        if self._drag_is_block and self._drag_block:
            sep_entry = self._drag_block[0][0]
            if sep_entry.is_separator and sep_entry.name in self._collapsed_seps:
                ghost_entries = [sep_entry]
            else:
                ghost_entries = [item[0] for item in self._drag_block]
        else:
            ghost_entries = [self._entries[self._drag_idx]]

        # Draw the ghost centered on the cursor (in widget-space, not canvas-space)
        canvas_top = int(self._canvas.canvasy(0))
        # _drag_cursor_y is widget-space; convert to canvas-space for drawing
        cursor_canvas_y = self._drag_cursor_y + canvas_top
        ghost_top = cursor_canvas_y - gh // 2

        for offset, entry in enumerate(ghost_entries):
            gy_top = ghost_top + offset * gh
            gy_mid = gy_top + gh // 2
            is_sep = entry.is_separator
            bg = _theme.BG_SEP if is_sep else BG_SELECT
            outline = ACCENT if offset == 0 else BORDER
            self._canvas.create_rectangle(
                2, gy_top, cw - 2, gy_top + gh,
                fill=bg, outline=outline, width=1, tags="drag_overlay",
            )
            self._canvas.create_text(
                self._COL_X[1], gy_mid,
                text=entry.display_name, anchor="w",
                fill=TEXT_SEP if is_sep else TEXT_MAIN,
                font=(_theme.FONT_FAMILY, _theme.FS10, "bold") if is_sep else (_theme.FONT_FAMILY, _theme.FS11),
                tags="drag_overlay",
            )

        # Blue insertion line showing where the item will land when released.
        # _drag_slot is an index into the vis-without-drag list.
        slot = self._drag_slot
        blk_size = len(self._drag_block) if self._drag_is_block else 1
        vis = self._visible_indices
        if self._drag_sel_indices:
            drag_set = set(self._drag_sel_indices)
        else:
            drag_set = set(range(self._drag_idx, self._drag_idx + blk_size))
        vis_without_drag = [i for i in vis if i not in drag_set]

        if slot >= len(vis_without_drag):
            # Inserting after the last rendered row
            line_y = len(vis_without_drag) * gh
        else:
            # slot is the position in vis_without_drag, which is also the rendered
            # row index (the drag entries are hidden/ghosted, so non-drag rows are
            # packed contiguously at 0..n-1). This is correct regardless of sort order.
            line_y = slot * gh

        self._canvas.create_line(
            0, line_y, cw, line_y,
            fill=ACCENT, width=2, tags="drag_overlay",
        )

    def _defocus_text_inputs(self):
        """Blur any focused Entry/Text by moving focus to the toplevel.
        Used by handlers that return "break" and so bypass the global
        defocus hook in gui.App.
        """
        try:
            focused = self.focus_get()
            if focused is None:
                return
            if focused.winfo_class() in ("Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"):
                self.winfo_toplevel().focus_set()
        except Exception:
            pass

    def _on_search_change(self, _event=None):
        # Ignore key events that fire after focus has left the search entry
        if self.focus_get() is not self._search_entry:
            return
        self._filter_text = self._search_entry.get().lower()
        if self._filter_text:
            self._search_clear_btn.pack(side="left", padx=(0, 8), pady=4)
        else:
            self._search_clear_btn.pack_forget()
        self._sel_idx = -1
        self._invalidate_derived_caches()
        self._redraw()

    def _on_search_clear(self, _event=None):
        self._search_entry.delete(0, "end")
        self._search_clear_btn.pack_forget()
        self._filter_text = ""
        self._sel_idx = -1
        self._invalidate_derived_caches()
        self._redraw()

    def _ensure_priorities(self) -> None:
        """Populate the entry-index → priority-number cache when empty.

        Must run before any priority-column sort (in _apply_column_sort) so the
        sort key has real numbers; otherwise priorities.get(i, 0) returns 0 for
        every mod and the "sort" is a no-op that leaves each group in its raw
        _entries order.  _compute_visible_indices calls this directly so callers
        outside _redraw (e.g. the multi-select collapse in _on_mouse_release)
        don't cache a mis-sorted visible list — that was the reverse-priority
        "list flips after dragging a locked separator" bug.
        """
        if self._priorities:
            return
        mod_count = sum(1 for e in self._entries if not e.is_separator)
        # During a reverse-mode (priority-ascending) drag, _entries is
        # physically inverted (lowest priority at the top — see _activate_drag).
        # The priority *number* of a mod is intrinsic to its real position, not
        # the temporary physical order, so number from the bottom up in that
        # case to keep the displayed numbers stable while dragging.
        _inverted_physical = (
            self._drag_saved_sort_column == "priority"
            and self._drag_saved_sort_ascending
        )
        if _inverted_physical:
            p = 0
            for idx, entry in enumerate(self._entries):
                if not entry.is_separator:
                    self._priorities[idx] = p
                    p += 1
        else:
            p = mod_count - 1
            for idx, entry in enumerate(self._entries):
                if not entry.is_separator:
                    self._priorities[idx] = p
                    p -= 1

    def _compute_visible_indices(self) -> list[int]:
        """Return entry indices that match the current filter, collapsed state, and column sort."""
        self._ensure_priorities()
        # Step 1: basic visibility (filter or collapse)
        if self._filter_text:
            ft = self._filter_text
            # Include mods that match AND separators that own at least one matching mod.
            matched_mods = {i for i, e in enumerate(self._entries)
                            if not e.is_separator and ft in e.name.lower()}
            base = []
            for i, e in enumerate(self._entries):
                if e.is_separator:
                    blk = self._sep_block_range(i)
                    if any(j in matched_mods for j in blk if j != i):
                        base.append(i)
                elif i in matched_mods:
                    base.append(i)
        elif not self._collapsed_seps:
            base = list(range(len(self._entries)))
        else:
            base = []
            skip = False
            _skip_bundle: str | None = None  # bundle_name to skip when collapsing a bundle sep
            for i, entry in enumerate(self._entries):
                if entry.is_separator:
                    skip = False
                    _skip_bundle = None
                    base.append(i)
                    if entry.name in self._collapsed_seps:
                        if self._is_bundle_separator(i):
                            _skip_bundle = entry.display_name
                        else:
                            skip = True
                elif skip:
                    # Keep the dragged entry visible even inside a collapsed block
                    if self._drag_idx >= 0 and i == self._drag_idx:
                        base.append(i)
                elif _skip_bundle is not None and self._bundle_name_of(i) == _skip_bundle:
                    pass  # collapsed bundle separator — hide only its variants
                else:
                    base.append(i)

        # Step 2: hide separators filter (keep synthetic Overwrite/Root Folder rows).
        # Tri-state: include = hide separators; exclude = SHOW separators (no-op,
        # since separators are visible by default anyway).
        if self._filter_hide_separators == 1:
            base = [i for i in base
                    if not self._entries[i].is_separator
                    or self._entries[i].name in (OVERWRITE_NAME, ROOT_FOLDER_NAME)]

        # Step 3: enabled/disabled filter (tri-state per side).
        # Include "show only enabled" ≡ exclude "show only disabled" and vice versa,
        # so we evaluate each side independently rather than picking a winner.
        if self._filter_show_disabled == 1:
            base = self._apply_filter(
                base, lambda e: not e.enabled, self._sep_block_has_disabled,
            )
        elif self._filter_show_disabled == 2:
            base = self._exclude_filter(base, lambda e: not e.enabled)
        if self._filter_show_enabled == 1:
            base = self._apply_filter(
                base, lambda e: e.enabled, self._sep_block_has_enabled,
            )
        elif self._filter_show_enabled == 2:
            base = self._exclude_filter(base, lambda e: e.enabled)

        # Step 4: conflict-type filter — handle include + exclude sets separately.
        include_conflicts: set = set()
        exclude_conflicts: set = set()
        for state, kind in (
            (self._filter_conflict_winning, CONFLICT_WINS),
            (self._filter_conflict_losing,  CONFLICT_LOSES),
            (self._filter_conflict_partial, CONFLICT_PARTIAL),
            (self._filter_conflict_full,    CONFLICT_FULL),
        ):
            if state == 1:
                include_conflicts.add(kind)
            elif state == 2:
                exclude_conflicts.add(kind)
        cmap, bmap = self._conflict_map, self._bsa_conflict_map
        if include_conflicts:
            base = self._apply_filter(
                base,
                lambda e: (cmap.get(e.name, CONFLICT_NONE) in include_conflicts
                           or bmap.get(e.name, CONFLICT_NONE) in include_conflicts),
                lambda i: self._sep_block_has_conflict_in(i, include_conflicts),
            )
        if exclude_conflicts:
            base = self._exclude_filter(
                base,
                lambda e: (cmap.get(e.name, CONFLICT_NONE) in exclude_conflicts
                           or bmap.get(e.name, CONFLICT_NONE) in exclude_conflicts),
            )

        # Steps 4b–4e3: simple flag-driven filters with optional precomputed mods set.
        # Each attr is now tri-state. Include uses the original predicate; exclude
        # inverts the mod predicate and leaves separators alone.
        for attr, mods_factory, mod_pred_fn, sep_pred_fn in self._SIMPLE_FILTER_SPECS:
            state = getattr(self, attr)
            if not state:
                continue
            mods_set = mods_factory(self) if mods_factory else None
            if mods_set is not None and not mods_set:
                # No mods match. Include → show nothing; Exclude → no-op.
                if state == 1:
                    base = []
                continue
            mod_pred = mod_pred_fn(self, mods_set)
            if state == 1:
                base = self._apply_filter(base, mod_pred, sep_pred_fn(self, mods_set))
            else:
                base = self._exclude_filter(base, mod_pred)

        # Step 4f: category filter — include and exclude sets are independent.
        if self._filter_categories:
            allowed_cats = self._filter_categories
            cats = self._category_names
            base = self._apply_filter(
                base,
                lambda e: (cats.get(e.name, "") or "") in allowed_cats,
                lambda i: self._sep_block_has_category(i, allowed_cats),
            )
        if self._filter_categories_exclude:
            blocked_cats = self._filter_categories_exclude
            cats = self._category_names
            base = self._exclude_filter(
                base,
                lambda e: (cats.get(e.name, "") or "") in blocked_cats,
            )

        # Step 4g: filetype filter — include and exclude sets are independent.
        if self._filter_filetypes:
            mods_set = self._get_mods_with_filetypes(self._filter_filetypes)
            if not mods_set:
                base = []
            else:
                base = self._apply_filter(
                    base,
                    lambda e: e.name in mods_set,
                    lambda i: self._sep_block_has_filetypes(i, mods_set),
                )
        if self._filter_filetypes_exclude:
            ex_mods = self._get_mods_with_filetypes(self._filter_filetypes_exclude)
            if ex_mods:
                base = self._exclude_filter(base, lambda e: e.name in ex_mods)

        # Step 5: apply column sort (visual only)
        if self._sort_column is not None:
            base = self._apply_column_sort(base)
        return base

    def _apply_filter(self, base: list[int], mod_pred, sep_pred) -> list[int]:
        """Canonical filter step: keep mods passing `mod_pred(entry)` and
        separators whose block satisfies `sep_pred(idx)`."""
        result = []
        for i in base:
            entry = self._entries[i]
            if entry.is_separator:
                if sep_pred(i):
                    result.append(i)
            elif mod_pred(entry):
                result.append(i)
        return result

    def _exclude_filter(self, base: list[int], mod_pred) -> list[int]:
        """Tri-state EXCLUDE step: drop mods matching `mod_pred(entry)`.

        Separators are always retained (they're filtered elsewhere) — an
        exclude filter has no concept of "block has any matching mod".
        """
        result = []
        for i in base:
            entry = self._entries[i]
            if entry.is_separator:
                result.append(i)
            elif not mod_pred(entry):
                result.append(i)
        return result

    # Specs for the eight simple flag-driven filters (steps 4b–4e3).
    # Each tuple: (panel attr that holds the bool flag,
    #              mods_set factory (or None if no precompute needed),
    #              mod predicate builder — receives self and mods_set,
    #              sep predicate builder — receives self and mods_set).
    _SIMPLE_FILTER_SPECS = (
        ("_filter_missing_reqs", None,
         lambda s, _ms: (lambda e: (e.name in s._missing_reqs
                                    and e.name not in s._ignored_missing_reqs)),
         lambda s, _ms: s._sep_block_has_missing_reqs),
        ("_filter_has_disabled_plugins", None,
         lambda s, _ms: (lambda e: e.name in s._disabled_plugins_map),
         lambda s, _ms: s._sep_block_has_disabled_plugins),
        ("_filter_has_notes", None,
         lambda s, _ms: (lambda e: e.name in s._mod_notes_map),
         lambda s, _ms: s._sep_block_has_notes),
        ("_filter_has_plugins", lambda s: s._get_mods_with_plugins(),
         lambda s, ms: (lambda e: e.name in ms),
         lambda s, ms: (lambda i: s._sep_block_has_plugins(i, ms))),
        ("_filter_has_disabled_files", None,
         lambda s, _ms: (lambda e: s._mod_is_modified_in_mf(e.name)),
         lambda s, _ms: s._sep_block_has_disabled_files),
        ("_filter_has_updates", None,
         lambda s, _ms: (lambda e: e.name in s._update_mods),
         lambda s, _ms: s._sep_block_has_updates),
        ("_filter_fomod_only", None,
         lambda s, _ms: (lambda e: e.name in s._fomod_mods),
         lambda s, _ms: s._sep_block_has_fomod),
        ("_filter_bain_only", None,
         lambda s, _ms: (lambda e: e.name in s._bain_mods),
         lambda s, _ms: s._sep_block_has_bain),
        ("_filter_has_bsa", lambda s: s._get_mods_with_bsa(),
         lambda s, ms: (lambda e: e.name in ms),
         lambda s, ms: (lambda i: s._sep_block_has_bsa(i, ms))),
        ("_filter_has_pbr", lambda s: s._get_mods_with_pbr(),
         lambda s, ms: (lambda e: e.name in ms),
         lambda s, ms: (lambda i: s._sep_block_has_pbr(i, ms))),
    )

    # ------------------------------------------------------------------
    # Column sorting helpers (visual only — never touches modlist.txt)
    # ------------------------------------------------------------------

    def _clear_sort(self) -> tuple[str | None, bool]:
        """Clear any active non-priority column sort. Returns (col, asc) from before clearing."""
        col, asc = self._sort_column, self._sort_ascending
        if self._sort_column is None or self._sort_column == "priority":
            return col, asc
        self._sort_column = None
        self._sort_ascending = True
        self._update_header(self._canvas_w)
        return col, asc

    def _restore_sort(self, col: str | None, asc: bool) -> None:
        """Restore a previously saved sort state and persist it."""
        if col is None:
            return
        self._sort_column = col
        self._sort_ascending = asc
        save_sort_state(col, asc)
        self._update_header(self._canvas_w)

    def _on_header_click(self, sort_key: str):
        """Handle a click on a sortable column header."""
        if sort_key == "priority":
            # Priority has 2 modes: reversed (ascending = 0 at top) and default (no sort)
            if self._sort_column == "priority":
                self._sort_column = None
                self._sort_ascending = True
            else:
                self._sort_column = "priority"
                self._sort_ascending = True
        elif self._sort_column == sort_key:
            # Same column clicked again — toggle direction, or clear on third click
            if not self._sort_ascending:
                # Already descending → clear sort
                self._sort_column = None
                self._sort_ascending = True
            else:
                self._sort_ascending = False
        else:
            self._sort_column = sort_key
            self._sort_ascending = True
        save_sort_state(self._sort_column, self._sort_ascending)
        self._update_header(self._canvas_w)
        self._invalidate_derived_caches()
        self._redraw()

    def _apply_column_sort(self, indices: list[int]) -> list[int]:
        """Sort visible indices by the active column. Separators stay in place;
        only mod rows within each separator group are reordered."""
        if not self._sort_column:
            return indices

        # Split indices into groups: each group starts with a separator (or the
        # implicit top-level group if the first entries aren't under a separator).
        groups: list[list[int]] = []
        current_sep: int | None = None
        current_mods: list[int] = []
        for idx in indices:
            entry = self._entries[idx]
            if entry.is_separator:
                # Flush previous group
                if current_sep is not None or current_mods:
                    groups.append((current_sep, current_mods))
                current_sep = idx
                current_mods = []
            else:
                current_mods.append(idx)
        # Flush last group
        if current_sep is not None or current_mods:
            groups.append((current_sep, current_mods))

        # Build sort key function
        key_fn = self._sort_key_fn()

        # For priority sort, also reverse the separator group order so that
        # higher-priority groups (top of list) move to the bottom when ascending.
        if self._sort_column == "priority" and self._sort_ascending:
            # Ascending = low priority first. Root Folder (lowest) goes to top,
            # Overwrite (highest) goes to bottom; real user groups are reversed
            # between them.  Mods that sit above the first user separator in
            # natural order are "ungrouped" (they belong to no user separator)
            # and are the HIGHEST priority — so they float to the bottom, just
            # above Overwrite, rather than reversing up to the top.  Keeping them
            # below the last user group (instead of pinned at the top) matches
            # their real priority while never making them members of that group.
            ow_group  = next(((s, m) for s, m in groups
                              if s is not None and self._entries[s].name == OVERWRITE_NAME), None)
            rf_group  = next(((s, m) for s, m in groups
                              if s is not None and self._entries[s].name == ROOT_FOLDER_NAME), None)
            user_groups = [(s, m) for s, m in groups if (s, m) != ow_group and (s, m) != rf_group]
            ungrouped = None
            if ow_group is not None and ow_group[1]:
                # Ungrouped mods live in OW's group (between OW and the first
                # user separator) — split them out as a separator-less group.
                ungrouped = (None, ow_group[1])
                ow_group = (ow_group[0], [])
            groups = (([rf_group] if rf_group else [])
                      + list(reversed(user_groups))
                      + ([ungrouped] if ungrouped else [])
                      + ([ow_group] if ow_group else []))

            # Show the static divider (BOUNDARY_ROW sentinel) between the last
            # user group and the ungrouped float so it's clear where that group
            # ends.  Shown permanently whenever user separators exist (regardless
            # of whether the last one is expanded/collapsed) so it never appears
            # or disappears on click — that toggling caused a visible flicker
            # when selecting a mod or separator.
            self._static_boundary_visible = bool(user_groups)
        else:
            self._static_boundary_visible = False

        result: list[int] = []
        for sep_idx, mod_indices in groups:
            # Inject the static divider just before the ungrouped float group.
            if (self._static_boundary_visible and ungrouped is not None
                    and mod_indices is ungrouped[1]):
                result.append(BOUNDARY_ROW)
            if sep_idx is not None:
                result.append(sep_idx)
            sorted_mods = sorted(mod_indices, key=key_fn, reverse=not self._sort_ascending)
            result.extend(sorted_mods)
        # When there are no ungrouped float mods, the loop above never hits the
        # injection point — add the divider just before Overwrite instead.
        if self._static_boundary_visible and ungrouped is None:
            _ow_pos = next((k for k, ei in enumerate(result)
                            if ei >= 0 and self._entries[ei].name == OVERWRITE_NAME), len(result))
            result.insert(_ow_pos, BOUNDARY_ROW)
        return result

    def _uninvert_entries_order(self):
        """Convert _entries from inverted-visual order back to natural order.

        Inverted visual order (priority ascending) is:
            [Root, lowest-pri-group, ..., highest-pri-group, OW]
        Natural order (what modlist.txt expects) is:
            [OW, highest-pri-group, ..., lowest-pri-group, Root]

        This reverses the group order (excluding pinned OW/Root) and reverses
        mod order within each group (ascending→descending priority).
        """
        # Split current _entries into groups by separator
        groups: list[tuple[int | None, list[int]]] = []
        current_sep: int | None = None
        current_mods: list[int] = []
        for idx in range(len(self._entries)):
            if self._entries[idx].is_separator:
                if current_sep is not None or current_mods:
                    groups.append((current_sep, current_mods))
                current_sep = idx
                current_mods = []
            else:
                current_mods.append(idx)
        if current_sep is not None or current_mods:
            groups.append((current_sep, current_mods))

        # Identify Root Folder and Overwrite groups
        ow_group = None
        rf_group = None
        middle = []
        for g in groups:
            sep_idx, mods = g
            if sep_idx is not None and self._entries[sep_idx].name == OVERWRITE_NAME:
                ow_group = g
            elif sep_idx is not None and self._entries[sep_idx].name == ROOT_FOLDER_NAME:
                rf_group = g
            else:
                middle.append(g)

        # Recover the high-priority "ungrouped float".  In inverted order it sits
        # below the transient boundary row (BOUNDARY_NAME), between the last user
        # group and Overwrite.  The boundary is itself a separator, so groups_of()
        # already split the float into its own group headed by that boundary —
        # peel that group out (dropping the boundary row) so the float mods
        # uninvert to the top, between OW and the first user group in natural
        # order, never becoming members of the last user group.
        ungrouped_idxs: list[int] = []
        for g in list(middle):
            sep_idx, mods = g
            if sep_idx is not None and self._entries[sep_idx].name == BOUNDARY_NAME:
                ungrouped_idxs = mods          # boundary row itself is discarded
                middle.remove(g)
                break

        # With no user separators, ungrouped mods live in Root's group
        # (first separator in inverted-visual order). Promote them to a
        # separator-less group so they reverse between OW and Root.
        if rf_group is not None and rf_group[1]:
            middle.append((None, rf_group[1]))
            rf_group = (rf_group[0], [])

        # Reverse middle groups and rebuild in natural order:
        # [OW, ungrouped-float, highest-pri-group, ..., lowest-pri-group, Root]
        new_groups = (([ow_group] if ow_group else [])
                      + ([(None, ungrouped_idxs)] if ungrouped_idxs else [])
                      + list(reversed(middle))
                      + ([rf_group] if rf_group else []))

        # Rebuild _entries and _check_vars in natural order.
        # Within each group, reverse mod order (ascending → descending priority).
        old_entries = list(self._entries)
        old_vars = list(self._check_vars)
        new_entries = []
        new_vars = []
        for sep_idx, mod_indices in new_groups:
            if sep_idx is not None:
                new_entries.append(old_entries[sep_idx])
                new_vars.append(old_vars[sep_idx])
            for mi in reversed(mod_indices):
                new_entries.append(old_entries[mi])
                new_vars.append(old_vars[mi])

        self._entries[:] = new_entries
        self._check_vars[:] = new_vars

    def _sort_key_fn(self):
        """Return a key function for sorting entry indices by the active column."""
        col = self._sort_column

        # Use the cached priorities dict (populated in _redraw).
        priorities = self._priorities

        if col == "name":
            return lambda i: self._entries[i].name.lower()
        elif col == "installed":
            def _installed_key(i):
                dt = self._install_datetimes.get(self._entries[i].name)
                if dt is None:
                    return (1, datetime.min)  # mods without date sort last
                return (0, dt)
            return _installed_key
        elif col == "flags":
            def _flags_key(i):
                name = self._entries[i].name
                # Lower number = flagged (sorts first when ascending)
                has_warning  = (name in self._missing_reqs
                               and name not in self._ignored_missing_reqs)
                is_locked    = self._entries[i].locked
                has_update   = name in self._update_mods
                has_root     = (name in self._root_folder_mods
                                or name in self._root_rule_mods)
                has_disabled = self._mod_is_modified_in_mf(name)
                has_info     = name in self._prertx_mods
                has_endorsed = name in self._endorsed_mods
                has_col_flag = (name in self._collection_bundled_mods
                                or name in self._collection_patched_mods)
                score = 0
                if has_warning:  score |= 128
                if is_locked:    score |= 64
                if has_update:   score |= 32
                if has_root:     score |= 16
                if has_disabled: score |= 8
                if has_info:     score |= 4
                if has_col_flag: score |= 2
                if has_endorsed: score |= 1
                return -score  # negate so flagged mods sort first in ascending
            return _flags_key
        elif col == "conflicts":
            # Order: partial (+-), loses (-), wins (+), full (x), none
            _CONFLICT_ORDER = {
                CONFLICT_PARTIAL: 0,
                CONFLICT_LOSES:   1,
                CONFLICT_WINS:    2,
                CONFLICT_FULL:    3,
                CONFLICT_NONE:    4,
            }
            def _conflict_key(i):
                c = self._conflict_map.get(self._entries[i].name, CONFLICT_NONE)
                return _CONFLICT_ORDER.get(c, 4)
            return _conflict_key
        elif col == "priority":
            return lambda i: priorities.get(i, 0)
        elif col == "category":
            return lambda i: (self._category_names.get(self._entries[i].name, "") or "\uffff").lower()
        elif col == "version":
            def _version_key(i):
                v = self._mod_versions.get(self._entries[i].name, "")
                if not v:
                    return (1, ())
                parts: list = []
                for tok in v.replace("-", ".").split("."):
                    try:
                        parts.append((0, int(tok)))
                    except ValueError:
                        parts.append((1, tok.lower()))
                return (0, tuple(parts))
            return _version_key
        elif col == "size":
            return lambda i: self._mod_size_bytes.get(self._entries[i].name, 0)
        else:
            return lambda i: i

    def _on_canvas_resize(self, event):
        w = event.width
        if self._canvas_resize_after_id is not None:
            self.after_cancel(self._canvas_resize_after_id)
        # Use a short after() delay so rapid resize events are coalesced into
        # one redraw rather than firing on every pixel change.
        self._canvas_resize_after_id = self.after(16, lambda: self._apply_canvas_resize(w))

    def _apply_canvas_resize(self, width: int):
        self._canvas_resize_after_id = None
        self._layout_columns(width)
        # Sync overrides with the actual post-resize widths so future drags
        # start from the correct baseline (avoids adjacent columns jumping).
        if self._col_w_override:
            for slot in range(1, 8):
                dc = self._slot_to_data_col(slot)
                if dc in self._col_w_override:
                    self._col_w_override[dc] = self._COL_W[slot]
        self._update_header(width)
        _clear_truncate_cache()
        self._redraw()

    def _schedule_redraw(self) -> None:
        """Coalesce rapid scroll events into a single _redraw() call via after_idle.

        Without this, rapid scroll events fire _redraw() multiple times per
        event-loop cycle.  Because canvas items (canvas-space) and placed widgets
        (widget-space) are updated together inside _redraw(), a partial repaint
        between two consecutive _redraw() calls can show widgets at stale positions
        — the grey-box / misplaced-widget glitch seen when scrolling.
        """
        if self._redraw_after_id is not None:
            self.after_cancel(self._redraw_after_id)
        self._redraw_after_id = self.after_idle(self._redraw)

    def _on_scroll_up(self, _event):
        if LEGACY_WHEEL_REDUNDANT:
            return
        self._canvas.yview("scroll", -50, "units")
        self._schedule_redraw()

    def _on_scroll_down(self, _event):
        if LEGACY_WHEEL_REDUNDANT:
            return
        self._canvas.yview("scroll", 50, "units")
        self._schedule_redraw()

    def _on_mousewheel(self, event):
        self._canvas.yview("scroll", -50 if event.delta > 0 else 50, "units")
        self._schedule_redraw()

    # ------------------------------------------------------------------
    # Hit-testing
    # ------------------------------------------------------------------

    def _canvas_y_to_index(self, canvas_y: int) -> int:
        """Convert a canvas-space y coordinate to a real entry index via visible list."""
        vis = self._visible_indices
        if not vis:
            return 0
        row = int(canvas_y // self.ROW_H)
        row = max(0, min(row, len(vis) - 1))
        ei = vis[row]
        # The static divider sentinel is non-interactive — snap to the nearest
        # real row so clicks/hover near it resolve to a real entry, never -2.
        if ei == BOUNDARY_ROW:
            for r in range(row + 1, len(vis)):
                if vis[r] != BOUNDARY_ROW:
                    return vis[r]
            for r in range(row - 1, -1, -1):
                if vis[r] != BOUNDARY_ROW:
                    return vis[r]
            return 0
        return ei

    def _event_canvas_y(self, event) -> int:
        return int(self._canvas.canvasy(event.y))

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    # Milliseconds the user must hold the mouse button before dragging starts
    _DRAG_DELAY_MS = 500

    def _cancel_drag_timer(self):
        """Cancel any pending drag-start timer."""
        if self._drag_after_id is not None:
            self._canvas.after_cancel(self._drag_after_id)
            self._drag_after_id = None
        self._drag_pending = False

    _DRAG_SCROLL_ZONE = 40     # pixels from edge to trigger auto-scroll
    _DRAG_SCROLL_INTERVAL = 50  # ms between scroll ticks

    def _maybe_start_drag_autoscroll(self):
        """Start or continue auto-scrolling if cursor is near the canvas edge."""
        if self._drag_idx < 0:
            self._cancel_drag_autoscroll()
            return
        h = self._canvas.winfo_height()
        y = self._drag_last_event_y
        zone = self._DRAG_SCROLL_ZONE
        if y < zone:
            speed = max(1, int(6 * (1.0 - y / zone)))
            self._canvas.yview("scroll", -speed, "units")
            self._redraw()
            self._cancel_drag_autoscroll()
            self._drag_scroll_after = self._canvas.after(
                self._DRAG_SCROLL_INTERVAL, self._maybe_start_drag_autoscroll)
        elif y > h - zone:
            speed = max(1, int(6 * (1.0 - (h - y) / zone)))
            self._canvas.yview("scroll", speed, "units")
            self._redraw()
            self._cancel_drag_autoscroll()
            self._drag_scroll_after = self._canvas.after(
                self._DRAG_SCROLL_INTERVAL, self._maybe_start_drag_autoscroll)
        else:
            self._cancel_drag_autoscroll()

    def _cancel_drag_autoscroll(self):
        if self._drag_scroll_after is not None:
            self._canvas.after_cancel(self._drag_scroll_after)
            self._drag_scroll_after = None

    def _on_mouse_press(self, event):
        try:
            self.winfo_toplevel()._last_list_panel = "mod"
        except Exception:
            pass
        if not self._entries:
            return
        # Cancel any previous pending drag
        self._cancel_drag_timer()
        self._pending_multidrag = None
        self._pending_sep_block = False
        cy = self._event_canvas_y(event)
        idx = self._canvas_y_to_index(cy)
        # The transient ungrouped-float boundary row is non-interactive — never
        # select, toggle, or start a drag from it.
        if 0 <= idx < len(self._entries) and self._entries[idx].name == BOUNDARY_NAME:
            return
        shift = bool(event.state & 0x1)
        ctrl  = bool(event.state & 0x4)
        # Alt mask varies across X11 setups: Mod1=0x8 on most Linux, 0x20000 on
        # some KDE/SteamOS configs.  Accept either.
        alt   = bool(event.state & (0x8 | 0x20000))
        # Alt-at-press bypasses locked-separator drag confinement.
        # Shift/Ctrl are already taken by selection gestures; Alt has no other
        # binding here so the drag still starts normally.
        self._drag_lock_bypass = alt

        # Checkbox hit-test: if the click is in the enable/disable column and the
        # entry is toggleable, handle it immediately and return.  This avoids
        # relying on tag bindings (which don't reliably block the widget-level binding).
        # Bundle variants are toggleable even when locked=True (locked only prevents drag).
        _cb_x_max = self._COL_X[1] - 4  # right edge of checkbox column
        _is_bundle_var = self._bundle_name_of(idx) is not None
        if (event.x <= _cb_x_max
                and not self._entries[idx].is_separator
                and (not self._entries[idx].locked or _is_bundle_var)
                and idx < len(self._check_vars)
                and self._check_vars[idx] is not None):
            var = self._check_vars[idx]
            var.set(not var.get())
            for s, di in enumerate(self._pool_data_idx):
                if di == idx and s < len(self._pool_check_vars):
                    self._pool_check_vars[s].set(var.get())
                    break
            self._checkbox_click_handled = True
            self._on_toggle(idx)
            return

        # Flag icon hit-test: clicking on missing-reqs or update flag opens the
        # corresponding dialog directly.
        entry = self._entries[idx]
        if not entry.is_separator:
            flag_slot = self._col_pos.get(3, 3)
            _FLAG_X = self._COL_X[flag_slot]
            _FLAG_W = self._COL_W[flag_slot]
            if _FLAG_X <= event.x < _FLAG_X + _FLAG_W:
                _FLAG_ICON_SPACING = scaled(22)
                _HIT_RADIUS = _FLAG_ICON_SPACING // 2
                has_missing = (entry.name in self._missing_reqs
                               and entry.name not in self._ignored_missing_reqs)
                _items: list[str] = []
                if entry.name in self._mod_notes_map:
                    _items.append("note")
                if entry.name in self._bundle_mods:
                    _items.append("bundle")
                if has_missing:
                    _items.append("missing")
                if entry.locked:
                    _items.append("star")
                if entry.name in self._update_mods:
                    _items.append("update")
                if entry.name in self._endorsed_mods:
                    _items.append("endorsed")
                if entry.name in self._prertx_mods:
                    _items.append("prertx")
                elif entry.name in self._collection_bundled_mods:
                    _items.append("collection_bundled")
                elif entry.name in self._collection_patched_mods:
                    _items.append("collection_patched")
                if self._mod_is_modified_in_mf(entry.name):
                    _items.append("disabled_files")
                if entry.name in self._root_folder_mods:
                    _items.append("root")
                elif entry.name in self._root_rule_mods:
                    _items.append("root_rule")
                _n = len(_items)
                if _n > 0:
                    _group_w = (_n - 1) * _FLAG_ICON_SPACING
                    _fx_start = _FLAG_X + _FLAG_W // 2 - _group_w // 2
                    for _fi, _kind in enumerate(_items):
                        _fx = _fx_start + _fi * _FLAG_ICON_SPACING
                        if abs(event.x - _fx) <= _HIT_RADIUS:
                            if _kind == "note":
                                self._open_note_editor_by_name(entry.name)
                                return
                            elif _kind == "bundle":
                                self._open_bundle_options(idx)
                                return
                            elif _kind == "missing":
                                dep_names = self._missing_reqs_detail.get(entry.name, [])
                                self._show_missing_reqs(entry.name, dep_names)
                                return
                            elif _kind == "update":
                                self._update_nexus_mod(entry.name)
                                return
                            break

        if self._entries[idx].is_separator:
            if self._entries[idx].name in (OVERWRITE_NAME, ROOT_FOLDER_NAME):
                # Synthetic rows are selectable (shows conflict highlights) but not draggable
                self._sel_idx = idx
                self._sel_set = {idx}
                self._drag_idx = -1
                self._drag_moved = False
                self._drag_slot  = -1
                self._redraw()
                self._update_info()
                label = "Overwrite" if self._entries[idx].name == OVERWRITE_NAME else "Root Folder"
                if self._on_mod_selected_cb is not None:
                    self._on_mod_selected_cb()
            else:
                # Click on collapse triangle zone (left 22px) — toggle collapse
                if event.x < 22:
                    self._toggle_collapse(self._entries[idx].name)
                    return
                # Ctrl+click on separator: toggle item in/out of selection
                if ctrl:
                    if idx in self._sel_set:
                        self._sel_set.discard(idx)
                        if self._sel_idx == idx:
                            self._sel_idx = next(iter(self._sel_set)) if self._sel_set else -1
                    else:
                        self._sel_set.add(idx)
                        self._sel_idx = idx
                    if self._on_mod_selected_cb is not None:
                        self._on_mod_selected_cb()
                    self._redraw()
                    return
                # Shift+click on separator: extend selection range (in display order)
                if shift and self._sel_idx >= 0:
                    vis = self._visible_indices
                    try:
                        lo_row = vis.index(self._sel_idx)
                        hi_row = vis.index(idx)
                    except ValueError:
                        self._sel_set = {idx}
                        self._sel_idx = idx
                    else:
                        lo_row, hi_row = min(lo_row, hi_row), max(lo_row, hi_row)
                        self._sel_set = {ei for ei in vis[lo_row : hi_row + 1]
                                         if ei != BOUNDARY_ROW}
                    if self._on_mod_selected_cb is not None:
                        self._on_mod_selected_cb()
                    self._redraw()
                    return
                # If this separator is part of an existing multi-selection, preserve
                # it so the whole selection drags together (same as non-separator path).
                # Defer activation until movement so a plain click doesn't invert
                # _entries in reverse mode (see _pending_multidrag).
                _sep_is_locked = self._sep_locks.get(self._entries[idx].name, False)
                if idx in self._sel_set and len(self._sel_set) > 1 and not _sep_is_locked:
                    self._pending_multidrag = (idx, cy)
                    return
                self._sel_idx = idx
                self._sel_set = {idx}
                if self._on_mod_selected_cb is not None:
                    self._on_mod_selected_cb()
                # Defer activation for ALL separators until the pointer actually
                # moves.  Activating immediately would invert _entries in
                # reverse-priority mode for what is usually a plain click, and
                # the invert→restore round-trip could leave a different
                # separator showing as expanded (see _pending_multidrag).
                # Locked separators drag their whole block — flag that so the
                # promotion rebuilds the block from _sep_block_range.
                self._pending_multidrag = (idx, cy)
                self._pending_sep_block = self._sep_locks.get(
                    self._entries[idx].name, False)
                self._redraw()
            return

        # Ctrl+click: toggle individual item in/out of selection
        if ctrl:
            if idx in self._sel_set:
                self._sel_set.discard(idx)
                if self._sel_idx == idx:
                    self._sel_idx = next(iter(self._sel_set)) if self._sel_set else -1
            else:
                self._sel_set.add(idx)
                self._sel_idx = idx
            if self._on_mod_selected_cb is not None:
                self._on_mod_selected_cb()
            self._redraw()
            self._update_info()
            return

        # Shift+click: extend selection from anchor to clicked row (in display order)
        if shift and self._sel_idx >= 0:
            vis = self._visible_indices
            try:
                lo_row = vis.index(self._sel_idx)
                hi_row = vis.index(idx)
            except ValueError:
                self._sel_set = {idx}
                self._sel_idx = idx
            else:
                lo_row, hi_row = min(lo_row, hi_row), max(lo_row, hi_row)
                self._sel_set = {ei for ei in vis[lo_row : hi_row + 1]
                                 if ei != BOUNDARY_ROW}
            if self._on_mod_selected_cb is not None:
                self._on_mod_selected_cb()
            self._redraw()
            self._update_info()
            return

        # If clicking inside an existing multi-selection, preserve it so the
        # user can drag the whole group — only collapse to single on release.
        # Defer starting the drag (which physically inverts _entries in reverse
        # mode) until the pointer actually moves: a plain click must leave the
        # list untouched and simply collapse the selection on release.
        _is_immovable = self._entries[idx].locked or self._bundle_name_of(idx) is not None
        if idx in self._sel_set and len(self._sel_set) > 1:
            if not _is_immovable:
                self._pending_multidrag = (idx, cy)
            return

        self._sel_idx = idx
        self._sel_set = {idx}
        if self._on_mod_selected_cb is not None:
            self._on_mod_selected_cb()
        self._redraw()
        self._update_info()
        if _is_immovable:
            # locked / bundle-variant entries are selectable but not draggable
            self._drag_idx = -1
            self._drag_moved = False
            self._drag_slot  = -1
            return
        # Activate drag immediately
        self._activate_drag(idx, cy, False, [])

    def _activate_drag(self, idx: int, start_y: int, is_block: bool, block: list):
        """Called after the hold delay — officially begin the drag."""
        self._drag_after_id = None
        self._drag_pending = False

        sel_indices: list[int] = []
        # If multiple items are selected and the dragged item is in the selection,
        # treat the whole selection as the drag block (sorted by entry index).
        if len(self._sel_set) > 1 and idx in self._sel_set and not is_block:
            # Filter out locked / bundle-variant entries — they should not be draggable
            sorted_sel = sorted(
                i for i in self._sel_set
                if not self._entries[i].locked and self._bundle_name_of(i) is None
            )
            if not sorted_sel:
                return
            block = [
                (self._entries[i], self._check_vars[i])
                for i in sorted_sel
            ]
            # Remember the actual (possibly sparse) indices for correct removal later
            sel_indices = sorted_sel
            # Anchor the drag at the first selected index
            idx = sorted_sel[0]
            is_block = True

        self._drag_start_y = start_y
        self._drag_moved = False
        self._drag_is_block = is_block
        self._drag_block = block
        self._drag_sel_indices = sel_indices  # empty list = contiguous block (separator drag)

        # When a column sort is active, real-time reordering mutates _entries
        # which causes the sort to recompute and produce unpredictable results.
        # Save a snapshot so each drag event can restore from the original state.
        if self._sort_column is not None:
            self._drag_entries_snapshot = list(self._entries)
            self._drag_vars_snapshot = list(self._check_vars)
            self._drag_saved_sort_column = self._sort_column
            self._drag_saved_sort_ascending = self._sort_ascending
            # For non-priority sorts, clear immediately so the list shows natural
            # order during the drag.  _entries is already in natural order for
            # these sorts (only _visible_indices was reordered), so no physical
            # reorder is needed.  The snapshot is still used by _on_mouse_drag
            # to reset _entries on each event.
            _is_inverted = (self._sort_column == "priority" and self._sort_ascending)
            if not _is_inverted:
                self._sort_column = None
                self._sort_ascending = True
                self._update_header(self._canvas_w)
                self._invalidate_derived_caches()
                self._vis_dirty = True
                self._mod_to_sep_idx = None
        else:
            self._drag_entries_snapshot = None
            self._drag_vars_snapshot = None
            self._drag_saved_sort_column = None
            self._drag_saved_sort_ascending = None

        # When priority-ascending (inverted) sort is active, physically reorder
        # _entries to match the visual display order.  This lets the standard
        # non-inverted drag logic work unchanged — visual position == _entries
        # position.  The snapshot above preserves the original order for restore.
        _inverted = (self._sort_column == "priority" and self._sort_ascending)
        if _inverted:
            # Build the FULL inverted order of every entry (not just the
            # filtered/collapsed visible subset).  Dropping non-visible
            # separators and their mods from new_order would persist that
            # loss on save — see the search+collapsed-sep bug.
            #
            # Natural order is:
            #   [OW, highest-pri-group, ..., lowest-pri-group, Root]
            # Inverted order (priority ascending) is:
            #   [Root, lowest-pri-group, ..., highest-pri-group, OW]
            # and mods within each group are reversed.  This mirrors
            # _uninvert_entries_order so release can cleanly invert back.
            groups: list[tuple[int | None, list[int]]] = []
            current_sep: int | None = None
            current_mods: list[int] = []
            for _i in range(len(self._entries)):
                if self._entries[_i].is_separator:
                    if current_sep is not None or current_mods:
                        groups.append((current_sep, current_mods))
                    current_sep = _i
                    current_mods = []
                else:
                    current_mods.append(_i)
            if current_sep is not None or current_mods:
                groups.append((current_sep, current_mods))

            ow_group = None
            rf_group = None
            middle: list[tuple[int | None, list[int]]] = []
            for g in groups:
                _sep_idx, _mods = g
                if _sep_idx is not None and self._entries[_sep_idx].name == OVERWRITE_NAME:
                    ow_group = g
                elif _sep_idx is not None and self._entries[_sep_idx].name == ROOT_FOLDER_NAME:
                    rf_group = g
                else:
                    middle.append(g)

            # Ungrouped mods live in OW's group (between OW and the first user
            # separator in natural order) — split them out so they can float to
            # the bottom (just above OW) instead of reversing up to the top.
            # This must match the static display order in _apply_column_sort.
            ungrouped = None
            if ow_group is not None and ow_group[1]:
                ungrouped = (None, ow_group[1])
                ow_group = (ow_group[0], [])

            # The ungrouped float slot sits between the last user group and OW.
            # When user separators exist, always reserve it (even if there are no
            # ungrouped mods yet) so the user can drop a mod at the bottom without
            # it joining the last separator's block.  ungrouped may be empty.
            if ungrouped is None:
                ungrouped = (None, [])

            # Inverted order: Root, reversed user groups, ungrouped float, OW.
            inv_groups = (([rf_group] if rf_group else [])
                          + list(reversed(middle))
                          + [ungrouped]
                          + ([ow_group] if ow_group else []))

            # A transient, non-interactive boundary row marks the divider above
            # the ungrouped float so dragging across it stays unambiguous and the
            # round-trip (uninvert) can recover the split point exactly.  Show it
            # whenever user separators exist (so the bottom-but-ungrouped slot is
            # always reachable) — not only when float mods are already present.
            # The boundary ModEntry lives only in _entries during the drag and is
            # dropped by _uninvert_entries_order; it is never saved.
            self._drag_has_boundary = bool(middle)

            # Build the reordered entries/vars directly so the boundary row can be
            # spliced in.  new_real_order holds old indices of the real entries in
            # their new positions; the boundary (when present) is interleaved.
            new_entries: list = []
            new_vars: list = []
            new_real_order: list[int] = []
            for _sep_idx, _mods in inv_groups:
                if (self._drag_has_boundary and _mods is ungrouped[1]):
                    # This is the floated ungrouped group (possibly empty) —
                    # precede it with the boundary divider row.
                    new_entries.append(ModEntry(
                        name=BOUNDARY_NAME, enabled=True, locked=True,
                        is_separator=True,
                    ))
                    new_vars.append(None)
                if _sep_idx is not None:
                    new_entries.append(self._entries[_sep_idx])
                    new_vars.append(self._check_vars[_sep_idx])
                    new_real_order.append(_sep_idx)
                # Within each group, reverse mod order so ascending priority
                # (lowest-pri first) matches the visual display.
                for _mi in reversed(_mods):
                    new_entries.append(self._entries[_mi])
                    new_vars.append(self._check_vars[_mi])
                    new_real_order.append(_mi)

            self._entries[:] = new_entries
            self._check_vars[:] = new_vars

            # Remap idx, sel_indices, and block references to new positions.
            # new_real_order maps "nth real entry" → old index; invert it and
            # then account for the boundary row(s) inserted before some entries.
            _old_for_pos = {i: old for i, old in enumerate(new_real_order)}
            old_to_new: dict[int, int] = {}
            _real_pos = 0
            for _new_pos, _e in enumerate(new_entries):
                if _e.name == BOUNDARY_NAME:
                    continue
                old_to_new[_old_for_pos[_real_pos]] = _new_pos
                _real_pos += 1
            idx = old_to_new[idx]
            # The selection holds NATURAL-order indices; remap them to the new
            # inverted positions so the highlight stays on the same mods (and not
            # on whatever entry now occupies the old index).
            self._sel_set = {old_to_new[i] for i in self._sel_set if i in old_to_new}
            if self._sel_idx in old_to_new:
                self._sel_idx = old_to_new[self._sel_idx]
            if sel_indices:
                sel_indices = sorted(old_to_new[i] for i in sel_indices)
                self._drag_sel_indices = sel_indices
                # Rebuild block with new entries/vars
                block = [(self._entries[i], self._check_vars[i]) for i in sel_indices]
                self._drag_block = block
                idx = sel_indices[0]
            elif is_block and block:
                # Separator block drag — rebuild block in reordered position order
                new_blk_start = idx
                new_blk_size = len(block)
                block = [
                    (self._entries[i], self._check_vars[i])
                    for i in range(new_blk_start, new_blk_start + new_blk_size)
                ]
                self._drag_block = block

            # Clear column sort so _apply_column_sort is a no-op during drag.
            # The physical order now matches the display.
            self._sort_column = None
            self._sort_ascending = True
            self._invalidate_derived_caches()
            self._vis_dirty = True
            self._mod_to_sep_idx = None

            # Save a second snapshot of the reordered state.  _on_mouse_drag
            # restores from this on each event so mutations don't cascade.
            # (_drag_entries_snapshot holds the ORIGINAL pre-reorder state for
            # full restore on cancel.)
            self._drag_reordered_snapshot = list(self._entries)
            self._drag_reordered_vars = list(self._check_vars)

        self._drag_idx = idx
        self._drag_origin_idx = idx

        # Compute the starting slot — position of the group's top among non-drag
        # items.  _on_mouse_drag uses delta-based movement from this baseline.
        if self._vis_dirty:
            self._visible_indices = self._compute_visible_indices()
            self._vis_dirty = False
        vis = self._visible_indices
        if sel_indices:
            drag_set = set(sel_indices)
            first_drag = sel_indices[0]
        else:
            blk_size = len(block) if is_block else 1
            drag_set = set(range(idx, idx + blk_size))
            first_drag = idx
        # Count non-drag vis entries before the first dragged entry (in display order).
        # When a non-priority sort was just cleared the entry's natural-order position
        # may be far off-screen.  Anchor the drag slot to the cursor's canvas Y so
        # the entry appears directly under the cursor from the first drag event.
        _non_inverted_sort_cleared = (
            self._drag_saved_sort_column is not None
            and self._drag_reordered_snapshot is None
        )
        if _non_inverted_sort_cleared:
            vis_no_drag_count = sum(1 for ei in vis if ei not in drag_set)
            start_slot = max(0, min(int(start_y / self.ROW_H), vis_no_drag_count))
        else:
            start_slot = 0
            for ei in vis:
                if ei == first_drag:
                    break
                if ei not in drag_set:
                    start_slot += 1
        self._drag_start_slot = start_slot
        self._drag_slot = start_slot

        # Resolve locked-separator confinement for this drag.  Only applies when
        # dragging a real mod (or a multi-select of mods) out of a locked block;
        # full-block drags (is_block with empty sel_indices = whole separator
        # moving as a unit) are unaffected.
        self._drag_lock_sep_name = None
        if not self._drag_lock_bypass:
            _is_full_block_drag = is_block and not sel_indices
            if not _is_full_block_drag:
                # Use the anchor (first dragged) entry to identify the owning block.
                anchor = sel_indices[0] if sel_indices else idx
                self._drag_lock_sep_name = self._locked_sep_owning(anchor)

    def _locked_sep_owning(self, mod_idx: int) -> str | None:
        """Return the name of the locked separator whose block contains mod_idx,
        or None if mod_idx is not inside a locked block (or is itself a separator).
        Walks back from mod_idx in the current _entries to the nearest separator."""
        if mod_idx < 0 or mod_idx >= len(self._entries):
            return None
        if self._entries[mod_idx].is_separator:
            return None
        i = mod_idx - 1
        while i >= 0 and not self._entries[i].is_separator:
            i -= 1
        if i < 0:
            return None
        sep_name = self._entries[i].name
        return sep_name if self._sep_locks.get(sep_name, False) else None

    def _locked_block_bounds(self, sep_name: str) -> tuple[int, int] | None:
        """Resolve [lo, hi) entry-index bounds of the locked block named sep_name
        in the current _entries.  Returns None if the separator isn't present."""
        for i, e in enumerate(self._entries):
            if e.is_separator and e.name == sep_name:
                blk = self._sep_block_range(i)
                return (blk.start, blk.stop)
        return None

    def _sep_block_range(self, sep_idx: int) -> range:
        """Return the range of indices [sep_idx, end) belonging to this separator block.
        The block is the separator plus every non-separator entry below it
        until the next separator (or end of list).  For bundle separators the
        block ends as soon as a non-bundle-variant entry is encountered so that
        unrelated mods sitting below the bundle are not absorbed.
        Results are cached."""
        cache = getattr(self, "_sep_block_cache", None)
        if cache is not None:
            cached = cache.get(sep_idx)
            if cached is not None:
                return cached
        end = sep_idx + 1
        is_bsep = self._is_bundle_separator(sep_idx)
        if is_bsep:
            bname = self._entries[sep_idx].display_name
            while end < len(self._entries) and not self._entries[end].is_separator:
                if self._bundle_name_of(end) != bname:
                    break
                end += 1
        else:
            while end < len(self._entries) and not self._entries[end].is_separator:
                end += 1
        result = range(sep_idx, end)
        if cache is not None:
            cache[sep_idx] = result
        return result

    def _sep_block_has_any(self, sep_idx: int, mod_predicate) -> bool:
        """True iff any non-separator entry in this block satisfies mod_predicate.

        mod_predicate receives the ModEntry and returns bool. Used to back the
        twelve _sep_block_has_* shorthands plus any ad-hoc filtering check.
        """
        for i in self._sep_block_range(sep_idx):
            entry = self._entries[i]
            if not entry.is_separator and mod_predicate(entry):
                return True
        return False

    def _sep_block_has_disabled(self, sep_idx: int) -> bool:
        return self._sep_block_has_any(sep_idx, lambda e: not e.enabled)

    def _sep_block_has_enabled(self, sep_idx: int) -> bool:
        return self._sep_block_has_any(sep_idx, lambda e: e.enabled)

    def _sep_block_has_conflict_in(self, sep_idx: int, allowed: set) -> bool:
        cmap, bmap = self._conflict_map, self._bsa_conflict_map
        return self._sep_block_has_any(
            sep_idx,
            lambda e: (cmap.get(e.name, CONFLICT_NONE) in allowed
                       or bmap.get(e.name, CONFLICT_NONE) in allowed),
        )

    def _sep_block_has_missing_reqs(self, sep_idx: int) -> bool:
        missing, ignored = self._missing_reqs, self._ignored_missing_reqs
        return self._sep_block_has_any(
            sep_idx, lambda e: e.name in missing and e.name not in ignored,
        )

    def _sep_block_has_disabled_plugins(self, sep_idx: int) -> bool:
        return self._sep_block_has_any(
            sep_idx, lambda e: e.name in self._disabled_plugins_map,
        )

    def _sep_block_has_notes(self, sep_idx: int) -> bool:
        return self._sep_block_has_any(
            sep_idx, lambda e: e.name in self._mod_notes_map,
        )

    def _get_mods_with_plugins(self) -> set[str]:
        """Set of mod names that own at least one plugin (per plugin panel's filemap)."""
        app = self.winfo_toplevel()
        if hasattr(app, "_plugin_panel"):
            return set(app._plugin_panel._plugin_mod_map.values())
        return set()

    def _sep_block_has_plugins(self, sep_idx: int, mods_with_plugins: set[str]) -> bool:
        return self._sep_block_has_any(
            sep_idx, lambda e: e.name in mods_with_plugins,
        )

    def _mod_is_modified_in_mf(self, mod_name: str) -> bool:
        """True if this mod has any Mod Files tab modifications — excluded
        files or a custom Top Level (strip prefix) entry."""
        if self._excluded_mod_files_map.get(mod_name):
            return True
        sp = self._mod_strip_prefixes.get(mod_name) if self._mod_strip_prefixes else None
        return bool(sp)

    def _sep_block_has_disabled_files(self, sep_idx: int) -> bool:
        return self._sep_block_has_any(
            sep_idx, lambda e: self._mod_is_modified_in_mf(e.name),
        )

    def _sep_block_has_updates(self, sep_idx: int) -> bool:
        return self._sep_block_has_any(
            sep_idx, lambda e: e.name in self._update_mods,
        )

    def _sep_block_has_fomod(self, sep_idx: int) -> bool:
        return self._sep_block_has_any(
            sep_idx, lambda e: e.name in self._fomod_mods,
        )

    def _sep_block_has_bain(self, sep_idx: int) -> bool:
        return self._sep_block_has_any(
            sep_idx, lambda e: e.name in self._bain_mods,
        )

    def _get_mods_with_bsa(self) -> set[str]:
        """Set of mod names that contain at least one BSA/BA2 archive with files."""
        if self._filemap_path is None:
            return set()
        index_path = self._filemap_path.parent / "bsa_index.bin"
        if not index_path.is_file():
            return set()
        try:
            from Utils.bsa_filemap import read_bsa_index
            index = read_bsa_index(index_path) or {}
        except Exception:
            return set()
        return {
            name for name, archives in index.items()
            if any(paths for _bsa, _mt, paths in archives)
        }

    def _sep_block_has_bsa(self, sep_idx: int, mods_with_bsa: set[str]) -> bool:
        return self._sep_block_has_any(
            sep_idx, lambda e: e.name in mods_with_bsa,
        )

    # Texture filename suffixes (before .dds) that PGPatcher acts on, mirroring
    # PGPatcher's getTexSuffixMap(): parallax height maps (_p), complex-material
    # env masks (_m/_em/_envmask) and TruePBR maps (_rmaos + PBR-only ancillaries).
    _PGPATCHER_TEX_SUFFIXES = (
        "_p",                         # parallax height
        "_m", "_em", "_envmask",      # complex material env mask
        "_rmaos", "_cnr", "_s",       # PBR
        "_i", "_f",                   # PBR (inner layer, fuzz)
    )

    def _get_mods_with_pbr(self) -> set[str]:
        """Set of mod names with textures PGPatcher operates on (parallax,
        complex material or PBR), detected by .dds filename suffix."""
        suffixes = self._PGPATCHER_TEX_SUFFIXES
        result: set[str] = set()
        for mod, (normal, root) in self._read_mod_index_safe().items():
            for rel_key in (*normal, *root):
                # rel_key is the normalized (lowercased) relative path.
                if not rel_key.endswith(".dds"):
                    continue
                stem = rel_key[:-4]
                if any(stem.endswith(suf) for suf in suffixes):
                    result.add(mod)
                    break
        return result

    def _sep_block_has_pbr(self, sep_idx: int, mods_with_pbr: set[str]) -> bool:
        return self._sep_block_has_any(
            sep_idx, lambda e: e.name in mods_with_pbr,
        )

    def _sep_block_has_category(self, sep_idx: int, allowed_categories: frozenset[str]) -> bool:
        cats = self._category_names
        return self._sep_block_has_any(
            sep_idx,
            lambda e: (cats.get(e.name, "") or "") in allowed_categories,
        )

    def _read_mod_index_safe(self):
        """Load the persisted mod index ({mod: (normal, root)}) or {} if unavailable."""
        if self._filemap_path is None:
            return {}
        index_path = self._filemap_path.parent / "modindex.bin"
        if not index_path.is_file():
            return {}
        try:
            from Utils.filemap import read_mod_index
            return read_mod_index(index_path) or {}
        except Exception:
            return {}

    def _get_filetype_counts(self) -> dict[str, int]:
        """Map extension (lowercase, with dot) → file count across all mods."""
        from os.path import splitext
        counts: dict[str, int] = {}
        for _mod, (normal, root) in self._read_mod_index_safe().items():
            for rel_key in normal:
                ext = splitext(rel_key)[1]
                if ext:
                    counts[ext] = counts.get(ext, 0) + 1
            for rel_key in root:
                ext = splitext(rel_key)[1]
                if ext:
                    counts[ext] = counts.get(ext, 0) + 1
        return counts

    def _get_mods_with_filetypes(self, exts: frozenset[str]) -> set[str]:
        """Set of mod names that contain at least one file with any of `exts`.

        `exts` must be lowercase extensions with leading dot (e.g. {".esp"}).
        """
        from os.path import splitext
        result: set[str] = set()
        for mod, (normal, root) in self._read_mod_index_safe().items():
            hit = False
            for rel_key in normal:
                if splitext(rel_key)[1] in exts:
                    hit = True
                    break
            if not hit:
                for rel_key in root:
                    if splitext(rel_key)[1] in exts:
                        hit = True
                        break
            if hit:
                result.add(mod)
        return result

    def _sep_block_has_filetypes(self, sep_idx: int, mods_with_filetypes: set[str]) -> bool:
        return self._sep_block_has_any(
            sep_idx, lambda e: e.name in mods_with_filetypes,
        )

    def _on_mouse_drag(self, event):
        # Promote a deferred multi-selection drag to a live drag on the first
        # real movement.  Until the pointer leaves the press row, treat it as a
        # potential click and do nothing (so _entries is never inverted for what
        # turns out to be a plain click).
        if self._pending_multidrag is not None:
            _p_idx, _p_cy = self._pending_multidrag
            if abs(self._event_canvas_y(event) - _p_cy) < self.ROW_H // 2:
                return
            self._pending_multidrag = None
            if self._pending_sep_block:
                # Locked separator: rebuild its block now (entries are still in
                # their pre-drag state) and activate as a full-block drag.
                self._pending_sep_block = False
                _blk = self._sep_block_range(_p_idx)
                _block = [(self._entries[i], self._check_vars[i]) for i in _blk]
                self._activate_drag(_p_idx, _p_cy, True, _block)
            else:
                self._activate_drag(_p_idx, _p_cy, False, [])

        if self._drag_idx < 0 or not self._entries:
            return

        # Auto-scroll near edges (with repeating timer)
        self._drag_last_event_y = event.y
        self._maybe_start_drag_autoscroll()

        cy = self._event_canvas_y(event)
        blk_size = len(self._drag_block) if self._drag_is_block else 1

        # Restore _entries to a clean state before computing the new insertion.
        # For inverted drag, use the reordered snapshot (physical = visual order).
        # For other column sorts, use the original snapshot.
        _reordered = self._drag_reordered_snapshot
        _snap = _reordered if _reordered is not None else self._drag_entries_snapshot
        _snap_vars = (self._drag_reordered_vars
                      if _reordered is not None
                      else self._drag_vars_snapshot)
        _using_snapshot = _snap is not None
        if _using_snapshot:
            self._entries[:] = list(_snap)
            self._check_vars[:] = list(_snap_vars)
            self._drag_idx = self._drag_origin_idx
            if self._drag_sel_indices:
                # Restore original sparse indices
                _block_ids = {id(e) for e, _ in self._drag_block}
                self._drag_sel_indices = sorted(
                    i for i in range(len(self._entries))
                    if id(self._entries[i]) in _block_ids
                )
            self._invalidate_derived_caches()
            self._vis_dirty = True
            self._mod_to_sep_idx = None

        if self._vis_dirty:
            self._visible_indices = self._compute_visible_indices()
            self._vis_dirty = False
        vis = self._visible_indices
        if self._drag_sel_indices:
            drag_set = set(self._drag_sel_indices)
        else:
            drag_set = set(range(self._drag_idx, self._drag_idx + blk_size))

        # vis_without_drag is the list of non-dragged visible entries in display order.
        # slot is an index into this list (0 = before first, len = after last).
        vis_without_drag = [i for i in vis if i not in drag_set]

        # Map cursor movement to slot changes using a pixel delta from drag
        # start.  Because real-time reordering moves the block under the cursor
        # on every slot change, position-based mapping doesn't work — the cursor
        # is always inside the block.  Instead, track cumulative pixel movement
        # and convert to row steps.
        dy = cy - self._drag_start_y
        slot = self._drag_start_slot + int(dy / self.ROW_H)
        slot = max(0, min(slot, len(vis_without_drag)))

        if slot == self._drag_slot:
            if _using_snapshot:
                # Snapshot was restored but slot unchanged — still need to
                # re-apply the insertion so _entries reflects the current slot.
                pass
            else:
                return

        self._drag_slot = slot

        # --- Real-time reorder ---
        # Note: inverted priority sort is handled by physically reordering
        # _entries at drag start (_activate_drag), so by this point
        # _sort_column is None and the standard logic works.

        def _entry_name(ei):
            return self._entries[ei].name

        # In reverse mode the physical _entries order is inverted, so a
        # separator visually sits ABOVE the group it owns (instead of below it
        # as in natural order).  Dropping just above such a separator should
        # join that separator's group at its top, not fall through into the
        # group above — otherwise a boundary drop lands in the Root-adjacent
        # gap that uninvert maps to the very top (the "jumps to position 0"
        # bug, GitHub #165).
        _inverted_physical = (
            self._drag_saved_sort_column == "priority"
            and self._drag_saved_sort_ascending
        )

        # A full separator-block drag (the separator plus its own mods, moving as
        # a unit) is a self-contained group.  Unlike a lone mod, it must NOT join
        # the group below it — dropping it just above another user separator means
        # "become a peer group directly above", so it inserts BEFORE that
        # separator.  Otherwise (inverted join-group semantics + the priority-0
        # top clamp) the block lands between the separator below and that
        # separator's mods, orphaning those mods into the dragged group and making
        # the top slot unreachable (GitHub: can't move a separator above the
        # first group in reverse mode).
        _is_full_sep_block = self._drag_is_block and not self._drag_sel_indices

        if slot == 0 and len(vis_without_drag) > 0:
            _pre_removal_insert = vis_without_drag[0]
        elif slot >= len(vis_without_drag):
            _pre_removal_insert = len(self._entries)
        else:
            above_ei = vis_without_drag[slot - 1]
            below_ei = vis_without_drag[slot]
            below_is_sep = self._entries[below_ei].is_separator
            if below_is_sep:
                below_name = _entry_name(below_ei)
                if below_name == OVERWRITE_NAME:
                    _pre_removal_insert = below_ei + 1
                elif below_name == BOUNDARY_NAME:
                    # The ungrouped-float divider is the boundary itself, not a
                    # group heading.  Dropping just above it means "last mod of
                    # the group above the divider" — insert BEFORE the boundary so
                    # this slot stays reachable.  (Dropping below the divider is
                    # the next slot down, handled by below_ei == first float mod.)
                    _pre_removal_insert = below_ei
                elif _inverted_physical and not _is_full_sep_block:
                    # Join the group this separator heads (insert after it).
                    # Only for lone-mod / multi-select drags — a full separator
                    # block stays above the separator (see _is_full_sep_block).
                    _pre_removal_insert = below_ei + 1
                else:
                    _pre_removal_insert = below_ei
            else:
                _pre_removal_insert = below_ei

        # Prevent non-bundle mods from being dropped inside a bundle block.
        _pre_removal_insert = self._clamp_outside_bundle_blocks(_pre_removal_insert)

        # In reverse mode the lowest-priority mod (priority 0) sits at the top,
        # just under Root Folder.  If a user separator heads that top group, the
        # gap between Root and that separator maps (on uninvert) to the HIGHEST
        # priority — so a mod dropped there jumps to the top.  Forbid that: clamp
        # the insertion to stay at or below the first user separator (i.e. never
        # above the priority-0 position).
        # This clamp guards a lone mod from landing in Root's ungrouped gap (which
        # uninverts to the very top / highest priority).  A full separator block
        # is exempt: dropping it above the first user separator legitimately makes
        # it the lowest-priority group, not a stray priority-0 mod.
        if _inverted_physical and not _is_full_sep_block:
            _rf_idx = next((i for i, e in enumerate(self._entries)
                            if e.is_separator and e.name == ROOT_FOLDER_NAME), None)
            if _rf_idx is not None and _rf_idx + 1 < len(self._entries) \
                    and self._entries[_rf_idx + 1].is_separator:
                # A user separator immediately follows Root — floor is just after it.
                _pre_removal_insert = max(_pre_removal_insert, _rf_idx + 2)

        # Confine drags inside a locked separator's block (Alt bypasses).
        # Re-check Alt each tick so the user can press/release Alt mid-drag.
        _alt_now = bool(event.state & (0x8 | 0x20000))
        if self._drag_lock_sep_name is not None and not _alt_now:
            bounds = self._locked_block_bounds(self._drag_lock_sep_name)
            if bounds is not None:
                _lo, _hi = bounds
                # Valid pre-removal insertion range is [_lo+1, _hi]: never above
                # the separator itself, never past the block's last entry.
                _pre_removal_insert = max(_lo + 1, min(_pre_removal_insert, _hi))

        _drop_insert_at = _pre_removal_insert - sum(1 for d in drag_set if d < _pre_removal_insert)

        if self._drag_is_block:
            if self._drag_sel_indices:
                for i in sorted(self._drag_sel_indices, reverse=True):
                    self._entries.pop(i)
                    self._check_vars.pop(i)
            else:
                del self._entries[self._drag_idx:self._drag_idx + blk_size]
                del self._check_vars[self._drag_idx:self._drag_idx + blk_size]

            insert_at = self._clamp_insert_at(_drop_insert_at, is_block=True)

            for j, (entry, var) in enumerate(self._drag_block):
                self._entries.insert(insert_at + j, entry)
                self._check_vars.insert(insert_at + j, var)
            self._drag_idx = insert_at
            if self._drag_sel_indices:
                self._drag_sel_indices = list(range(insert_at, insert_at + len(self._drag_block)))
                self._sel_set = set(self._drag_sel_indices)
            else:
                self._sel_set = set(range(insert_at, insert_at + len(self._drag_block)))
            self._sel_idx = insert_at
        else:
            entry = self._entries.pop(self._drag_idx)
            var   = self._check_vars.pop(self._drag_idx)

            insert_at = self._clamp_insert_at(_drop_insert_at, is_block=False)

            self._entries.insert(insert_at, entry)
            self._check_vars.insert(insert_at, var)
            self._drag_idx = insert_at
            self._sel_idx  = insert_at
            self._sel_set  = {insert_at}

        # Only count as a real move when the block ends at a different slot than
        # it started.  A click (or jitter) inside a multi-selection enters this
        # handler with slot == _drag_start_slot; in reverse mode _using_snapshot
        # forces the insertion to be re-applied each event, so without this gate
        # a zero-delta click would mark the drag "moved", commit an
        # invert→uninvert round-trip on release, and visibly shuffle the list.
        if slot != self._drag_start_slot:
            self._drag_moved = True
        self._invalidate_derived_caches()
        self._vis_dirty = True
        self._mod_to_sep_idx = None
        self._redraw()

    def _on_mouse_release(self, event):
        self._cancel_drag_timer()
        self._cancel_drag_autoscroll()

        # A deferred multi-selection drag that never moved = a plain click inside
        # the selection.  Collapse to the clicked mod without ever inverting
        # _entries (so reverse-mode order is untouched).
        if self._pending_multidrag is not None:
            _p_idx, _ = self._pending_multidrag
            self._pending_multidrag = None
            self._pending_sep_block = False
            self._sel_idx = _p_idx
            self._sel_set = {_p_idx}
            if self._on_mod_selected_cb is not None:
                self._on_mod_selected_cb()
            self._redraw()
            self._update_info()
            return

        had_multi = len(self._sel_set) > 1

        # Restore sort state if it was cleared for inverted drag
        _saved_col = self._drag_saved_sort_column
        _saved_asc = self._drag_saved_sort_ascending

        # A click (or jitter) inside a multi-selection can momentarily toggle the
        # drag slot away from and back to its origin, leaving _drag_moved set even
        # though the block ended exactly where it began.  Committing that as a
        # move runs an invert→uninvert round-trip that visibly re-sorts the list
        # (priority 0 ends up at the bottom of the group).  Treat "ended at the
        # origin slot" as no movement so the clean restore path runs instead.
        if (self._drag_moved and self._drag_idx >= 0
                and self._drag_slot == self._drag_start_slot):
            self._drag_moved = False

        if self._drag_idx >= 0 and self._drag_moved:
            # Real-time reorder already happened during drag.
            # If the drag was inverted, _entries is in visual (inverted) order
            # and must be converted back to natural order before saving.
            if _saved_col == "priority" and _saved_asc:
                # Remember the dragged entry object(s) before uninverting so we
                # can find their new positions and update the selection.
                _dragged_entry = self._entries[self._drag_idx] if not self._drag_is_block else None
                _dragged_block_entries = (
                    [e for e, _ in self._drag_block] if self._drag_is_block else []
                )
                self._uninvert_entries_order()
                # Update sel_idx/sel_set to the new positions after uninvert
                if _dragged_entry is not None:
                    new_idx = next(
                        (i for i, e in enumerate(self._entries) if e is _dragged_entry), self._sel_idx
                    )
                    self._sel_idx = new_idx
                    self._sel_set = {new_idx}
                    self._drag_idx = new_idx
                elif _dragged_block_entries:
                    _block_set = {id(e) for e in _dragged_block_entries}
                    new_indices = [
                        i for i, e in enumerate(self._entries) if id(e) in _block_set
                    ]
                    if new_indices:
                        self._sel_idx = new_indices[0]
                        # Collapse to single selection so subsequent clicks don't
                        # re-trigger the multi-drag path with stale indices.
                        self._sel_set = {new_indices[0]}
            self._save_modlist()  # always save, regardless of which branch above ran
            if _saved_col is not None:
                # Restore the sort that was active before the drag
                self._sort_column = _saved_col
                self._sort_ascending = _saved_asc
            self._invalidate_derived_caches()
            self._vis_dirty = True
            self._mod_to_sep_idx = None
            self._rebuild_filemap()
        elif self._drag_idx >= 0 and not self._drag_moved:
            # No movement — restore original _entries if we reordered for inverted drag.
            # The selection currently holds INVERTED-order indices (remapped in
            # _activate_drag); capture the selected entry objects so we can recover
            # their natural-order positions after restoring _entries.
            _sel_entries = {id(self._entries[i]) for i in self._sel_set
                            if 0 <= i < len(self._entries)}
            _sel_idx_entry = (self._entries[self._sel_idx]
                              if 0 <= self._sel_idx < len(self._entries) else None)
            _snap = self._drag_entries_snapshot
            if _snap is not None:
                self._entries[:] = _snap
                self._check_vars[:] = list(self._drag_vars_snapshot)
                # Re-resolve the selection against the restored natural order.
                self._sel_set = {i for i, e in enumerate(self._entries)
                                 if id(e) in _sel_entries}
                if _sel_idx_entry is not None:
                    self._sel_idx = next(
                        (i for i, e in enumerate(self._entries) if e is _sel_idx_entry),
                        self._sel_idx,
                    )
            if _saved_col is not None:
                self._sort_column = _saved_col
                self._sort_ascending = _saved_asc
            # _entries is now back in natural order, so the "inverted drag"
            # priority-numbering branch (keyed on _drag_saved_sort_column) no
            # longer applies.  Clear these markers before the collapse block
            # below computes visible indices — otherwise _ensure_priorities
            # numbers natural-order _entries with the inverted (bottom-up)
            # scheme, baking wrong priority numbers AND a no-op sort into the
            # cached visible list (the reverse-priority "list flips after
            # dragging a locked separator" bug).  The local _saved_* copies
            # still drive the header/restore logic below.
            self._drag_saved_sort_column = None
            self._drag_saved_sort_ascending = None
            self._invalidate_derived_caches()
            self._vis_dirty = True
            self._mod_to_sep_idx = None
            if had_multi:
                # Click (no drag) inside a multi-selection — collapse to the clicked item.
                # Recompute visible indices first so _canvas_y_to_index is accurate.
                self._visible_indices = self._compute_visible_indices()
                self._vis_dirty = False
                cy = self._event_canvas_y(event)
                clicked = self._canvas_y_to_index(cy)
                # When inverted drag was active, _sel_set may have stale indices
                # from the previous drag — collapse unconditionally.
                if clicked in self._sel_set or _saved_col is not None:
                    self._sel_idx = clicked
                    self._sel_set = {clicked}
                    self._update_info()
        self._drag_idx = -1
        self._drag_origin_idx = -1
        self._drag_moved = False
        self._drag_slot  = -1
        self._drag_is_block = False
        self._drag_sel_indices = []
        self._drag_entries_snapshot = None
        self._drag_vars_snapshot = None
        self._drag_reordered_snapshot = None
        self._drag_reordered_vars = None
        self._drag_saved_sort_column = None
        self._drag_saved_sort_ascending = None
        self._drag_has_boundary = False
        self._drag_lock_sep_name = None
        self._drag_lock_bypass = False
        if _saved_col is not None:
            self._update_header(self._canvas_w)
        self._redraw()
        self._update_info()

    def refresh_show_summary_tooltips(self) -> None:
        """Re-read the show_summary_tooltips setting from disk (live update)."""
        self._show_summary_tooltips = load_show_summary_tooltips()
        if not self._show_summary_tooltips:
            self._tooltip.hide()

    def refresh_hide_bsa_conflicts(self) -> None:
        """Re-read the hide_bsa_conflicts setting (live update).

        Rebuilds the filemap when the setting changed so BSA conflict flags
        appear/disappear (and parsing starts/stops) without an app restart.
        """
        new_val = load_hide_bsa_conflicts()
        if new_val == self._hide_bsa_conflicts:
            return
        self._hide_bsa_conflicts = new_val
        if new_val:
            # Clear the existing flags immediately; the rebuild will skip parsing.
            self._bsa_conflict_map = {}
            self._bsa_overrides = {}
            self._bsa_overridden_by = {}
        self._rebuild_filemap()

    def _get_mod_description(self, mod_name: str) -> str:
        """Return the cached Nexus summary for *mod_name*, or "" if none.

        Reads ``meta.ini`` lazily on first request per mod; subsequent calls
        hit the in-memory cache. Cache is cleared when the update checker
        completes (the only thing that refreshes descriptions).
        """
        if mod_name in self._description_cache:
            return self._description_cache[mod_name]
        desc = ""
        if self._staging_root is not None:
            meta_path = self._staging_root / mod_name / "meta.ini"
            if meta_path.is_file():
                try:
                    meta = read_meta(meta_path)
                    desc = (meta.description or "").strip()
                except Exception:
                    desc = ""
        self._description_cache[mod_name] = desc
        return desc

    def _build_flag_tooltips(self, entry, flag_x: int, flag_w: int,
                             spacing: int) -> list[tuple[int, str]]:
        """Replicate _redraw()'s flag-icon layout as (center_x, tooltip) pairs."""
        has_missing = (entry.name in self._missing_reqs
                       and entry.name not in self._ignored_missing_reqs)
        _items: list[str] = []
        if entry.name in self._mod_notes_map:
            _items.append("note")
        if entry.name in self._bundle_mods:
            _items.append("bundle")
        if has_missing:
            _items.append("missing")
        if entry.locked:
            _items.append("star")
        if entry.name in self._update_mods:
            _items.append("update")
        if entry.name in self._endorsed_mods:
            _items.append("endorsed")
        if entry.name in self._prertx_mods:
            _items.append("prertx")
        elif entry.name in self._collection_bundled_mods:
            _items.append("collection_bundled")
        elif entry.name in self._collection_patched_mods:
            _items.append("collection_patched")
        if self._mod_is_modified_in_mf(entry.name):
            _items.append("disabled_files")
        if entry.name in self._xedit_modified_mods:
            _items.append("xedit")
        if entry.name in self._root_folder_mods:
            _items.append("root")
        elif entry.name in self._root_rule_mods:
            _items.append("root_rule")
        _flag_tooltips: list[tuple[int, str]] = []
        _n = len(_items)
        if _n > 0:
            _group_w = (_n - 1) * spacing
            _fx_start = flag_x + flag_w // 2 - _group_w // 2
            for _fi, _kind in enumerate(_items):
                _fx = _fx_start + _fi * spacing
                if _kind == "note":
                    _txt = self._mod_notes_map.get(entry.name, "")
                    tip = (_txt[:500] + "…") if len(_txt) > 500 else _txt
                    if not tip:
                        tip = "Note"
                elif _kind == "bundle":
                    tip = "Click here to open bundle settings"
                elif _kind == "missing":
                    missing = self._missing_reqs_detail.get(entry.name, [])
                    tip = ("Missing requirements:\n" + "\n".join(f"  - {m}" for m in missing)
                           if missing else "Missing requirements")
                elif _kind == "update":
                    tip = "Update available on Nexus Mods"
                elif _kind == "endorsed":
                    tip = "Endorsed"
                elif _kind == "prertx":
                    tip = "Pre-RTX mod"
                elif _kind == "collection_bundled":
                    tip = "This mod is a collection bundled mod"
                elif _kind == "collection_patched":
                    tip = "This mod has diff patches applied by the collection install"
                elif _kind == "disabled_files":
                    tip = "Modified in Mod Files tab"
                elif _kind == "xedit":
                    _plugins = self._xedit_modified_mods.get(entry.name, [])
                    if _plugins:
                        tip = ("Contains a plugin modified in xEdit:\n"
                               + "\n".join(f"  - {p}" for p in _plugins))
                    else:
                        tip = "Contains a plugin modified in xEdit"
                elif _kind == "root":
                    tip = "This mod is sent to the root folder"
                elif _kind == "root_rule":
                    tip = "This mod contains files that route to the game root"
                else:
                    tip = ""
                if tip:
                    _flag_tooltips.append((_fx, tip))
        return _flag_tooltips

    def _on_mouse_motion(self, event):
        """Update hover highlight as the mouse moves over the modlist."""
        if not self._entries or self._drag_idx >= 0:
            self._tooltip.hide()
            return
        cy = self._event_canvas_y(event)
        vis = self._visible_indices
        row = cy // self.ROW_H
        new_hover = vis[row] if 0 <= row < len(vis) else -1
        if new_hover != self._hover_idx:
            old_hover = self._hover_idx
            self._hover_idx = new_hover
            self._repaint_hover_rows(old_hover, new_hover)

        # The static divider sentinel is non-interactive — no tooltips / column
        # hit-tests apply (and it is not a valid _entries index).
        if new_hover == BOUNDARY_ROW:
            self._tooltip.hide()
            return

        # Show tooltip when hovering over the flags column icons.
        # Replicate the same layout logic as _redraw() to find which icon the cursor is over.
        x = event.x
        flag_slot = self._col_pos.get(3, 3)
        flags_col_start = self._COL_X[flag_slot]
        flags_col_end = flags_col_start + self._COL_W[flag_slot]
        if flags_col_start <= x < flags_col_end and 0 <= row < len(vis):
            ei = vis[row]
            entry = self._entries[ei]
            if not entry.is_separator:
                _FLAG_ICON_SPACING = scaled(22)
                _HIT_RADIUS = _FLAG_ICON_SPACING // 2
                _cached = self._flag_tip_cache
                if _cached is not None and _cached[0] == ei:
                    _flag_tooltips = _cached[1]
                else:
                    _flag_tooltips = self._build_flag_tooltips(
                        entry, flags_col_start, self._COL_W[flag_slot], _FLAG_ICON_SPACING)
                    self._flag_tip_cache = (ei, _flag_tooltips)
                if _flag_tooltips:
                    # Find which icon the cursor is closest to (within hit radius)
                    for _fx, tip in _flag_tooltips:
                        if abs(x - _fx) <= _HIT_RADIUS:
                            self._tooltip.show(event.x_root, event.y_root, tip)
                            return
                    # Cursor is in the flags column but not over a specific icon —
                    # keep any existing tooltip rather than flashing it away
                    return

        # Show tooltip when hovering over the conflicts column icons/dots.
        conf_slot = self._col_pos.get(4, 4)
        conf_col_start = self._COL_X[conf_slot]
        conf_col_end = conf_col_start + self._COL_W[conf_slot]
        if conf_col_start <= x < conf_col_end and 0 <= row < len(vis):
            entry = self._entries[vis[row]]
            if not entry.is_separator and not entry.locked:
                loose = self._conflict_map.get(entry.name, CONFLICT_NONE)
                bsa   = self._bsa_conflict_map.get(entry.name, CONFLICT_NONE)
                has_loose = loose != CONFLICT_NONE
                has_bsa   = bsa != CONFLICT_NONE
                if has_loose or has_bsa:
                    _conflict_label = {
                        CONFLICT_WINS:    "Winning",
                        CONFLICT_LOSES:   "Losing",
                        CONFLICT_PARTIAL: "Partial",
                        CONFLICT_FULL:    "Full",
                    }
                    cx_center = conf_col_start + self._COL_W[conf_slot] // 2
                    if has_loose and has_bsa:
                        # Left half → loose, right half → BSA
                        if x < cx_center:
                            tip = f"Loose file conflict - {_conflict_label[loose]}"
                        else:
                            tip = f"BSA conflict - {_conflict_label[bsa]}"
                    elif has_loose:
                        tip = f"Loose file conflict - {_conflict_label[loose]}"
                    else:
                        tip = f"BSA conflict - {_conflict_label[bsa]}"
                    self._tooltip.show(event.x_root, event.y_root, tip)
                    return

        # Show tooltip with the Nexus summary when hovering over the name column.
        if not self._show_summary_tooltips:
            self._tooltip.hide()
            return
        name_slot = self._col_pos.get(1, 1)
        name_col_start = self._COL_X[name_slot]
        name_col_end = name_col_start + self._COL_W[name_slot]
        if name_col_start <= x < name_col_end and 0 <= row < len(vis):
            entry = self._entries[vis[row]]
            if not entry.is_separator:
                desc = self._get_mod_description(entry.name)
                if desc:
                    tip = (desc[:500] + "…") if len(desc) > 500 else desc
                    self._tooltip.show(event.x_root, event.y_root, tip, side="right")
                    return

        # Show tooltip when hovering over the separator lock checkbox.
        if 0 <= row < len(vis):
            entry = self._entries[vis[row]]
            if entry.is_separator and entry.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME):
                cw = self._canvas_w
                lock_w = scaled(28)
                lk_cx = cw - lock_w - scaled(8) + lock_w // 2
                cb_size2 = scaled(14)
                if abs(x - lk_cx) <= cb_size2 // 2 + scaled(2):
                    self._tooltip.show(
                        event.x_root, event.y_root,
                        "Lock Separator - Mods in this separator are attached to it",
                    )
                    return

        self._tooltip.hide()

    def _on_mouse_leave(self, event):
        """Clear hover highlight when mouse leaves the canvas."""
        self._tooltip.hide()
        if self._hover_idx != -1:
            old_hover = self._hover_idx
            self._hover_idx = -1
            self._repaint_hover_rows(old_hover)

    def _on_right_click(self, event):
        if not self._entries:
            return
        cy = self._event_canvas_y(event)
        idx = self._canvas_y_to_index(cy)
        entry = self._entries[idx]
        is_sep = entry.is_separator

        # If right-clicking outside the current selection, collapse to clicked item
        if idx not in self._sel_set:
            self._sel_idx = idx
            self._sel_set = {idx}
            self._redraw()

        # Find .ini files in this mod's staging folder (only for non-separators)
        ini_files: list[Path] = []
        mod_folder: Path | None = None
        plugin_files: list[str] = []
        if self._modlist_path is not None:
            staging_root = self._staging_root
            if not is_sep:
                mod_dir = staging_root / entry.name
                mod_folder = mod_dir
                if mod_dir.is_dir():
                    ini_files = [p for p in sorted(mod_dir.rglob("*.ini"))
                                 if p.name.lower() != "meta.ini"]
                    app = self.winfo_toplevel()
                    plugin_ext: set[str] = set()
                    if hasattr(app, "_plugin_panel"):
                        plugin_ext = {e.lower() for e in app._plugin_panel._plugin_extensions}
                    if plugin_ext:
                        plugin_files = []
                        for p in mod_dir.rglob("*"):
                            try:
                                if p.is_file() and p.suffix.lower() in plugin_ext:
                                    plugin_files.append(p.name)
                            except PermissionError:
                                pass
                        plugin_files.sort()
            elif entry.name == OVERWRITE_NAME:
                mod_folder = staging_root.parent / "overwrite"
            elif entry.name == ROOT_FOLDER_NAME:
                mod_folder = (
                    self._game.get_effective_root_folder_path()
                    if self._game is not None
                    else staging_root.parent / "Root_Folder"
                )

        self._show_context_menu(event.x_root, event.y_root, idx, is_sep, ini_files,
                                mod_folder=mod_folder, plugin_files=plugin_files)

    def _show_context_menu(self, x: int, y: int, idx: int, is_separator: bool,
                           ini_files: list[Path] | None = None,
                           mod_folder: Path | None = None,
                           plugin_files: list[str] | None = None):
        """CTkPopupMenu for the mod-list context menu.

        The bulk of selection/meta/profile/Nexus precompute lives in
        _build_context_menu_ctx(); this method orchestrates the precompute,
        builds the alphabetically-ordered item list via _populate_context_menu,
        and pops the menu.
        """
        if self._context_menu is None:
            self._context_menu = CTkPopupMenu(
                self.winfo_toplevel(), width=220, title="",
            )
        menu = self._context_menu
        menu.clear()

        ctx = self._build_context_menu_ctx(
            idx, is_separator,
            ini_files=ini_files, mod_folder=mod_folder, plugin_files=plugin_files,
        )
        self._populate_context_menu(menu, ctx)
        menu.popup(x, y)

    def _build_context_menu_ctx(self, idx: int, is_separator: bool,
                                *, ini_files: list[Path] | None,
                                mod_folder: Path | None,
                                plugin_files: list[str] | None) -> SimpleNamespace:
        """Compute every flag, list and lookup the context menu needs.

        Returns a SimpleNamespace; the populator reads from it and never goes
        back to self for selection or meta state."""
        entries = self._entries
        entry = entries[idx]
        is_overwrite = entry.name == OVERWRITE_NAME
        is_root_folder = entry.name == ROOT_FOLDER_NAME
        is_synthetic = is_overwrite or is_root_folder
        is_real_mod = not is_separator and not is_synthetic
        is_multi = len(self._sel_set) > 1 and idx in self._sel_set
        is_bundle_sep = is_separator and self._is_bundle_separator(idx)
        is_bundle_var = (not is_separator) and (self._bundle_name_of(idx) is not None)
        is_locked = (not is_separator) and entry.locked
        mod_name = entry.name

        # Selection subsets — all "sorted indices into self._entries".
        sel_sorted = sorted(self._sel_set) if is_multi else []
        in_range = [i for i in sel_sorted if 0 <= i < len(entries)]
        non_synthetic_real = [
            i for i in in_range
            if not entries[i].is_separator
            and not entries[i].locked
            and entries[i].name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
        ]
        remove_multi = list(non_synthetic_real)
        multi_sel = [i for i in non_synthetic_real if self._bundle_name_of(i) is None]
        remove_multi_sep = [
            i for i in in_range
            if entries[i].is_separator
            and entries[i].name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
        ]
        # Toggleable also exists on >1-row selection where idx itself isn't in the set.
        toggleable = (
            non_synthetic_real if is_multi else
            [i for i in (sorted(self._sel_set) if len(self._sel_set) > 1 else [])
             if 0 <= i < len(entries)
             and not entries[i].is_separator
             and not entries[i].locked
             and entries[i].name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)]
        )

        sep_names = [
            e.name for e in entries
            if e.is_separator and e.name != OVERWRITE_NAME
            and e.name != ROOT_FOLDER_NAME
            and e.display_name not in self._bundle_groups
        ]

        # Profile copy targets.
        other_profiles: list[str] = []
        copy_mod_name: str | None = None
        copy_mod_names: list[str] = []
        if is_real_mod and self._modlist_path is not None and self._game is not None:
            topbar = getattr(self.winfo_toplevel(), "_topbar", None)
            game_name = topbar._game_var.get() if topbar else ""
            cur_profile = self._modlist_path.parent.name
            other_profiles = [p for p in _profiles_for_game(game_name) if p != cur_profile]
            if other_profiles:
                if is_multi:
                    copy_mod_names = [
                        entries[i].name for i in in_range
                        if not entries[i].is_separator
                        and entries[i].name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
                    ]
                else:
                    copy_mod_name = mod_name

        # Conflict status.
        conflict_status = CONFLICT_NONE
        bsa_conflict_status = CONFLICT_NONE
        if not is_multi and (not is_separator or is_overwrite):
            conflict_status = (
                self._conflict_map.get(mod_name, CONFLICT_NONE)
                if not is_overwrite
                else (CONFLICT_WINS if self._overrides.get(OVERWRITE_NAME) else CONFLICT_NONE)
            )
            if not is_overwrite:
                bsa_conflict_status = self._bsa_conflict_map.get(mod_name, CONFLICT_NONE)

        # Nexus / meta info for the focused mod (single-select only fields).
        ctx_meta = None
        nexus_url: str | None = None
        domain: str | None = None
        archive_path: Path | None = None
        if is_real_mod and self._modlist_path is not None:
            meta_path = self._staging_root / mod_name / "meta.ini"
            if meta_path.is_file():
                try:
                    ctx_meta = read_meta(meta_path)
                    if ctx_meta.mod_id > 0:
                        domain = self._resolve_nexus_domain(ctx_meta)
                        nexus_url = f"https://www.nexusmods.com/{domain}/mods/{ctx_meta.mod_id}"
                    if not is_multi and ctx_meta.installation_file:
                        archive_path = self._find_installation_archive(ctx_meta.installation_file)
                except Exception:
                    ctx_meta = None

        # Multi-select Nexus URLs + endorse/abstain targets (same scan).
        nexus_urls: list[str] = []
        endorse_multi: list[tuple[str, str, object]] = []
        abstain_multi: list[tuple[str, str, object]] = []
        if toggleable and self._staging_root is not None:
            for ti in toggleable:
                tname = entries[ti].name
                tmeta_path = self._staging_root / tname / "meta.ini"
                if not tmeta_path.is_file():
                    continue
                try:
                    tmeta = read_meta(tmeta_path)
                    if tmeta.mod_id > 0:
                        tdomain = self._resolve_nexus_domain(tmeta)
                        nexus_urls.append(
                            f"https://www.nexusmods.com/{tdomain}/mods/{tmeta.mod_id}"
                        )
                        if tmeta.endorsed:
                            abstain_multi.append((tname, tdomain, tmeta))
                        else:
                            endorse_multi.append((tname, tdomain, tmeta))
                except Exception:
                    pass

        # Multi-select Root Folder + Note targets.
        root_folder_enable_multi: list[str] = []
        root_folder_disable_multi: list[str] = []
        # Quick Update targets — update-flagged mods only; premium-gated in the
        # populator (non-premium users can't direct-download).
        is_premium = bool(getattr(self.winfo_toplevel(), "_nexus_is_premium", False))
        quick_update_names: list[str] = []
        if is_multi:
            quick_update_names = [
                entries[i].name for i in toggleable
                if entries[i].name in self._update_mods
            ]
        elif (is_real_mod and ctx_meta is not None and ctx_meta.mod_id > 0
              and mod_name in self._update_mods):
            quick_update_names = [mod_name]

        note_multi_names: list[str] = []
        note_multi_remove_names: list[str] = []
        missing_reqs_multi_names: list[str] = []
        if is_multi:
            for ti in non_synthetic_real:
                tname = entries[ti].name
                if tname in self._root_folder_mods:
                    root_folder_disable_multi.append(tname)
                else:
                    root_folder_enable_multi.append(tname)
                note_multi_names.append(tname)
                if tname in self._mod_notes_map:
                    note_multi_remove_names.append(tname)
                if (tname in self._missing_reqs
                        and tname not in self._ignored_missing_reqs):
                    missing_reqs_multi_names.append(tname)

        return SimpleNamespace(
            idx=idx, mod_name=mod_name,
            is_separator=is_separator, is_overwrite=is_overwrite,
            is_synthetic=is_synthetic, is_real_mod=is_real_mod,
            is_multi=is_multi, is_bundle_sep=is_bundle_sep,
            is_bundle_var=is_bundle_var, is_locked=is_locked,
            ini_files=ini_files, mod_folder=mod_folder, plugin_files=plugin_files,
            remove_multi=remove_multi, multi_sel=multi_sel,
            remove_multi_sep=remove_multi_sep, toggleable=toggleable,
            sep_names=sep_names,
            other_profiles=other_profiles,
            copy_mod_name=copy_mod_name, copy_mod_names=copy_mod_names,
            conflict_status=conflict_status, bsa_conflict_status=bsa_conflict_status,
            ctx_meta=ctx_meta, nexus_url=nexus_url, domain=domain,
            archive_path=archive_path, nexus_urls=nexus_urls,
            endorse_multi=endorse_multi, abstain_multi=abstain_multi,
            root_folder_enable_multi=root_folder_enable_multi,
            root_folder_disable_multi=root_folder_disable_multi,
            note_multi_names=note_multi_names,
            note_multi_remove_names=note_multi_remove_names,
            missing_reqs_multi_names=missing_reqs_multi_names,
            is_premium=is_premium, quick_update_names=quick_update_names,
        )

    def _resolve_nexus_domain(self, meta) -> str:
        """Best-effort game-domain resolution: meta.game_domain, then the
        domain segment of nexus_page_url, then the configured game default."""
        if meta.game_domain:
            return meta.game_domain
        if "/mods/" in meta.nexus_page_url:
            return meta.nexus_page_url.split("/mods/")[0].rsplit("/", 1)[-1]
        topbar_var = getattr(getattr(self.winfo_toplevel(), "_topbar", None),
                             "_game_var", tk.StringVar())
        cur_game = _GAMES.get(topbar_var.get(), None)
        return cur_game.nexus_game_domain if cur_game else ""

    def _find_installation_archive(self, filename: str) -> Path | None:
        """Locate `filename` across the user's Downloads dir and configured caches."""
        xdg = os.environ.get("XDG_DOWNLOAD_DIR")
        dl_dir = Path(xdg) if xdg else Path.home() / "Downloads"
        active_game_name = getattr(self._game, "name", "") or ""
        search_dirs: list[Path] = []
        try:
            from gui.download_locations_overlay import (
                is_default_downloads_disabled,
                load_extra_download_locations,
            )
            if not is_default_downloads_disabled():
                search_dirs.append(dl_dir)
            search_dirs.extend(list_all_cache_dirs(active_game_name))
            search_dirs.extend(Path(p) for p in load_extra_download_locations())
        except Exception:
            search_dirs = [dl_dir]
            search_dirs.extend(list_all_cache_dirs(active_game_name))
        for d in search_dirs:
            cand = Path(d) / filename
            if cand.is_file():
                return cand
        return None

    def _populate_context_menu(self, menu, c: SimpleNamespace) -> None:
        """Add the command/submenu entries, organised into functional groups
        separated by thin spacers. Within each group entries stay alphabetical."""
        idx = c.idx
        mod_name = c.mod_name
        ctx_meta = c.ctx_meta

        # Emit a thin spacer before a group only when the menu already has
        # content *and* the group itself produced at least one entry, so we
        # never get leading/trailing/doubled separators from skipped entries.
        _row_at_group_start = [menu._item_row]

        def _group_break() -> None:
            """Call between groups: insert a separator iff the previous group
            added items and any earlier content exists."""
            if menu._item_row != _row_at_group_start[0] and menu._has_items:
                menu.add_separator()
            _row_at_group_start[0] = menu._item_row

        # ── Group 1: Open / manage the mod ──────────────────────────────
        # Open folder
        if c.mod_folder is not None and not c.is_multi:
            menu.add_command("Open folder", lambda f=c.mod_folder: self._open_folder(f))

        # Bundle options… (RE/Fluffy single-mod bundles)
        if (not c.is_separator and not c.is_multi
                and self._bundle_spec_path(idx) is not None):
            menu.add_command("Bundle options…", lambda: self._open_bundle_options(idx))

        # Create empty mod below
        if self._modlist_path is not None and not c.is_synthetic and not c.is_multi:
            menu.add_command("Create empty mod below", lambda: self._create_empty_mod(idx))

        # Reinstall Mod
        if (c.is_real_mod and not c.is_multi
                and ctx_meta is not None and c.archive_path is not None):
            menu.add_command("Reinstall Mod",
                lambda nc=mod_name, ap=c.archive_path: self._reinstall_mod(nc, ap))

        # Rename mod
        if (not c.is_separator and not c.is_locked
                and not c.is_bundle_var and not c.is_multi):
            menu.add_command("Rename mod", lambda: self._rename_mod(idx),
                             accelerator="F2")

        # ── Group 2: Files & install options ────────────────────────────
        _group_break()

        # Disable Plugins…
        if not c.is_separator and not c.is_locked and c.plugin_files and not c.is_multi:
            menu.add_command("Disable Plugins…",
                lambda n=mod_name, pf=c.plugin_files: self._show_disable_plugins_dialog(n, pf))

        # INI files
        if not c.is_separator and not c.is_locked and c.ini_files and not c.is_multi:
            menu.add_submenu("INI files",
                lambda inis=c.ini_files: self._show_ini_picker(
                    inis, parent_dismiss=menu._withdraw, parent_popup=menu))

        # Root Folder install toggle (single)
        if not c.is_separator and not c.is_locked and not c.is_multi:
            is_rf = mod_name in self._root_folder_mods
            rf_label = "Disable Root Folder install" if is_rf else "Enable Root Folder install"
            menu.add_command(rf_label,
                lambda mn=mod_name: self._toggle_root_folder_flag(mn))

        # Root Folder install toggle (multi)
        if c.is_multi and c.root_folder_disable_multi:
            names = list(c.root_folder_disable_multi)
            menu.add_command(f"Disable Root Folder install ({len(names)})",
                lambda mns=names: self._set_root_folder_flag_multi(mns, False))
        if c.is_multi and c.root_folder_enable_multi:
            names = list(c.root_folder_enable_multi)
            menu.add_command(f"Enable Root Folder install ({len(names)})",
                lambda mns=names: self._set_root_folder_flag_multi(mns, True))

        # ── Group 3: Nexus / online & updates ───────────────────────────
        _group_break()

        # Abstain from Endorsement (single)
        if (c.is_real_mod and not c.is_multi
                and ctx_meta is not None and ctx_meta.mod_id > 0 and ctx_meta.endorsed):
            menu.add_command("Abstain from Endorsement",
                lambda: self._abstain_nexus_mod(mod_name, c.domain, ctx_meta))

        # Abstain selected (multi)
        if c.is_multi and c.abstain_multi:
            targets = list(c.abstain_multi)
            menu.add_command(f"Abstain selected ({len(targets)})",
                lambda t=targets: self._abstain_selected_mods(t))

        # Change Version
        if (c.is_real_mod and not c.is_multi
                and ctx_meta is not None and ctx_meta.mod_id > 0):
            menu.add_command("Change Version",
                lambda mn=mod_name: self._update_nexus_mod(mn))

        # Check Updates (single)
        if (c.is_real_mod and not c.is_multi
                and ctx_meta is not None and ctx_meta.mod_id > 0):
            menu.add_command("Check Updates",
                lambda mn=mod_name: self._on_check_updates_for_mods([mn]))

        # Check Updates (multi)
        if c.is_multi and c.nexus_urls:
            check_names = [self._entries[i].name for i in c.toggleable]
            menu.add_command(f"Check Updates ({len(check_names)})",
                lambda mns=check_names: self._on_check_updates_for_mods(mns))

        # Endorse Mod (single)
        if (c.is_real_mod and not c.is_multi
                and ctx_meta is not None and ctx_meta.mod_id > 0 and not ctx_meta.endorsed):
            menu.add_command("Endorse Mod",
                lambda: self._endorse_nexus_mod(mod_name, c.domain, ctx_meta))

        # Endorse selected (multi)
        if c.is_multi and c.endorse_multi:
            targets = list(c.endorse_multi)
            menu.add_command(f"Endorse selected ({len(targets)})",
                lambda t=targets: self._endorse_selected_mods(t))

        # Missing Requirements (single)
        if c.is_real_mod and not c.is_multi and mod_name in self._missing_reqs:
            dep_names = self._missing_reqs_detail.get(mod_name, [])
            menu.add_command("Missing Requirements",
                lambda: self._show_missing_reqs(mod_name, dep_names))

        # Missing Requirements (multi) — aggregate across selected mods
        if c.is_multi and c.missing_reqs_multi_names:
            names = list(c.missing_reqs_multi_names)
            menu.add_command(f"Missing Requirements ({len(names)})",
                lambda mns=names: self._show_missing_reqs_multi(mns))

        # Open on Nexus (single)
        if (c.is_real_mod and not c.is_multi and ctx_meta is not None and c.nexus_url):
            menu.add_command("Open on Nexus",
                lambda u=c.nexus_url: self._open_nexus_page(u))

        # Open on Nexus (multi)
        if c.nexus_urls:
            urls_cap = list(c.nexus_urls)
            menu.add_command(f"Open on Nexus ({len(urls_cap)})",
                lambda u=urls_cap: self._open_nexus_pages(u))

        # Quick Update (premium only) — auto-install latest name-matched version
        if c.is_premium and c.quick_update_names:
            n = len(c.quick_update_names)
            qu_label = "Quick Update" if n == 1 else f"Quick Update ({n})"
            menu.add_command(qu_label,
                lambda mns=list(c.quick_update_names): self._quick_update_mods(mns))

        # ── Group 4: Organise / layout ──────────────────────────────────
        _group_break()

        # Add separator above / below
        if not c.is_multi:
            menu.add_command("Add separator above", lambda: self._add_separator(idx, above=True))
            menu.add_command("Add separator below", lambda: self._add_separator(idx, above=False))

        # Copy to profile
        if c.other_profiles:
            if c.is_multi and c.copy_mod_names:
                menu.add_submenu(
                    f"Copy to profile ({len(c.copy_mod_names)})",
                    lambda profs=c.other_profiles, mns=c.copy_mod_names:
                        self._show_copy_to_profile_picker_multi(
                            mns, profs, parent_dismiss=menu._withdraw, parent_popup=menu),
                )
            elif not c.is_multi and c.copy_mod_name:
                menu.add_submenu(
                    "Copy to profile",
                    lambda profs=c.other_profiles, mn=c.copy_mod_name:
                        self._show_copy_to_profile_picker(
                            mn, profs, parent_dismiss=menu._withdraw, parent_popup=menu),
                )

        # Disable / Enable selected (n)
        if c.toggleable:
            count = len(c.toggleable)
            menu.add_command(f"Disable selected ({count})",
                lambda inds=list(c.toggleable): self._disable_selected_mods(inds),
                accelerator="Enter")
            menu.add_command(f"Enable selected ({count})",
                lambda inds=list(c.toggleable): self._enable_selected_mods(inds),
                accelerator="Enter")

        # Move to separator
        if not c.is_separator and not c.is_locked and not c.is_bundle_var and c.sep_names:
            if c.multi_sel:
                menu.add_submenu(f"Move to separator ({len(c.multi_sel)})",
                    lambda ms=c.multi_sel: self._show_separator_picker_multi(
                        ms, c.sep_names, parent_dismiss=menu._withdraw, parent_popup=menu))
            else:
                menu.add_submenu("Move to separator",
                    lambda: self._show_separator_picker(
                        idx, c.sep_names, parent_dismiss=menu._withdraw, parent_popup=menu))

        # Set priority…
        if (not c.is_separator and not c.is_locked
                and not c.is_bundle_var and not c.is_multi):
            menu.add_command("Set priority…", lambda: self._set_priority(idx))

        # Sort Alphabetically (multi-select only)
        if c.is_multi and len(c.toggleable) >= 2:
            menu.add_command(f"Sort Alphabetically ({len(c.toggleable)})",
                lambda inds=list(c.toggleable): self._sort_selected_alphabetically(inds))

        # ── Group 5: Info / conflicts / notes ───────────────────────────
        _group_break()

        # Add / Edit note (single)
        if c.is_real_mod and not c.is_multi:
            note_label = "Edit note" if mod_name in self._mod_notes_map else "Add note"
            menu.add_command(note_label,
                lambda mn=mod_name: self._open_note_editor_by_name(mn))

        # Add / append note (multi)
        if c.is_multi and c.note_multi_names:
            names = list(c.note_multi_names)
            menu.add_command(f"Add note ({len(names)})",
                lambda mns=names: self._open_note_editor_for_multi(mns))

        # Remove note (multi)
        if c.is_multi and c.note_multi_remove_names:
            names = list(c.note_multi_remove_names)
            menu.add_command(f"Remove note ({len(names)})",
                lambda mns=names: self._remove_notes_multi(mns))

        # Show Conflicts
        if c.conflict_status != CONFLICT_NONE or c.bsa_conflict_status != CONFLICT_NONE:
            menu.add_command("Show Conflicts",
                lambda: self._show_overwrites_dialog(mod_name))

        # ── Group 6: Destructive — remove ───────────────────────────────
        _group_break()

        # Remove mod
        if not c.is_separator and not c.is_locked:
            if c.remove_multi and self._modlist_path is not None:
                menu.add_command(f"Remove mod ({len(c.remove_multi)})",
                    lambda rm=c.remove_multi: self._remove_selected_mods(rm),
                    accelerator="Del")
            else:
                menu.add_command("Remove mod", lambda: self._remove_mod(idx),
                                 accelerator="Del")

        # ── Separator-row entries (mutually exclusive with the mod groups) ─
        # A separator row sets is_separator; none of the above mod entries
        # fire for it, so these form their own block below any earlier ones.
        if c.is_separator and not c.is_synthetic:
            _group_break()

            # Change separator color
            menu.add_command("Change separator color",
                lambda: self._change_separator_color(idx))

            # Lock / Unlock Separator(s)
            if len(c.remove_multi_sep) >= 2:
                sel_sep_names = [self._entries[i].name for i in c.remove_multi_sep]
                all_locked = all(self._sep_locks.get(n, False) for n in sel_sep_names)
                label = (f"Unlock Separators ({len(sel_sep_names)})" if all_locked
                         else f"Lock Separators ({len(sel_sep_names)})")
                menu.add_command(label,
                    lambda names=sel_sep_names, lock=not all_locked:
                        self._set_sep_locks(names, lock))
            else:
                is_sep_locked = self._sep_locks.get(mod_name, False)
                label = "Unlock Separator" if is_sep_locked else "Lock Separator"
                menu.add_command(label,
                    lambda n=mod_name: self._on_sep_lock_toggle(n))

            # Rename separator
            if not c.is_bundle_sep:
                menu.add_command("Rename separator",
                    lambda: self._rename_separator(idx),
                    accelerator="F2")

            # Separator settings…
            if not c.is_bundle_sep:
                menu.add_command("Separator settings…",
                    lambda: self._show_sep_settings(idx))

            # Remove separator(s)  (destructive — own sub-group)
            _group_break()
            if len(c.remove_multi_sep) >= 2:
                menu.add_command(f"Remove separators ({len(c.remove_multi_sep)})",
                    lambda inds=list(c.remove_multi_sep): self._remove_separators(inds))
            else:
                menu.add_command("Remove separator", lambda: self._remove_separator(idx))

    def _on_root_folder_toggle(self) -> None:
        self._root_folder_enabled = not self._root_folder_enabled
        self._save_root_folder_state()
        # Update the synthetic entry's enabled state in-place
        for entry in self._entries:
            if entry.name == ROOT_FOLDER_NAME:
                entry.enabled = self._root_folder_enabled
                break
        self._redraw()
        # Framework banners distinguish enabled/disabled Root_Folder staging.
        pp = getattr(self.winfo_toplevel(), "_plugin_panel", None)
        if pp is not None and hasattr(pp, "_refresh_framework_banners"):
            pp._refresh_framework_banners()

    def _toggle_root_folder_flag(self, mod_name: str) -> None:
        """Toggle rootFolder=true/false in a mod's meta.ini and refresh the UI."""
        meta_path = self._staging_root / mod_name / "meta.ini"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = read_meta(meta_path) if meta_path.is_file() else None
        if meta is None:
            from Nexus.nexus_meta import NexusModMeta as _ModMeta
            meta = _ModMeta()
        new_val = not meta.root_folder
        meta.root_folder = new_val
        write_meta(meta_path, meta)
        # Update in-memory set immediately so the flag icon appears without a full reload
        if new_val:
            self._root_folder_mods.add(mod_name)
            self._log(f"{mod_name}: Root Folder install ENABLED — files will deploy to game root.")
        else:
            self._root_folder_mods.discard(mod_name)
            self._log(f"{mod_name}: Root Folder install DISABLED — files will deploy to Data/ as normal.")
        self._vis_dirty = True
        self._mod_to_sep_idx = None
        self._redraw()
        # The cached index stores paths with strip_prefixes applied for non-root
        # mods and verbatim for root-flagged mods. Toggling the flag invalidates
        # the entry, so rescan just this mod before rebuilding the filemap.
        self._rescan_root_toggled_mods([mod_name])
        self._rebuild_filemap()

    def _set_root_folder_flag_multi(self, mod_names: list[str], enable: bool) -> None:
        """Apply rootFolder=enable to every mod in mod_names. Skips mods already
        in the desired state so we don't churn meta.ini for no reason."""
        if not mod_names:
            return
        from Nexus.nexus_meta import NexusModMeta as _ModMeta
        changed: list[str] = []
        for mod_name in mod_names:
            already = mod_name in self._root_folder_mods
            if enable == already:
                continue
            meta_path = self._staging_root / mod_name / "meta.ini"
            try:
                meta_path.parent.mkdir(parents=True, exist_ok=True)
                meta = read_meta(meta_path) if meta_path.is_file() else _ModMeta()
                meta.root_folder = enable
                write_meta(meta_path, meta)
            except Exception as exc:
                self._log(f"{mod_name}: Could not update root folder flag — {exc}")
                continue
            if enable:
                self._root_folder_mods.add(mod_name)
            else:
                self._root_folder_mods.discard(mod_name)
            changed.append(mod_name)
        if changed:
            verb = "ENABLED" if enable else "DISABLED"
            tail = ("files will deploy to game root."
                    if enable else "files will deploy to Data/ as normal.")
            self._log(f"Root Folder install {verb} on {len(changed)} mod(s) — {tail}")
            self._vis_dirty = True
            self._mod_to_sep_idx = None
            self._redraw()
            # Index entries for these mods were scanned with the old strip
            # semantics — rescan before rebuilding the filemap.
            self._rescan_root_toggled_mods(changed)
        # Rebuild filemap so filemap_root.txt reflects the updated root-folder assignment.
        self._rebuild_filemap()

    def _rescan_root_toggled_mods(self, mod_names: list[str]) -> None:
        """Rescan the index entries for *mod_names* using current root-flag state.

        The index caches each mod's file list with strip_prefixes applied only
        for non-root mods (see filemap.rebuild_mod_index). When a mod's
        root_folder flag flips, its cached paths are wrong for the new state,
        so this re-runs the scan for just those mods.
        """
        if not mod_names or self._filemap_path is None:
            return
        try:
            rescan_mods_in_index(
                self._filemap_path.parent / "modindex.bin",
                self._staging_root,
                mod_names,
                strip_prefixes=self._strip_prefixes,
                per_mod_strip_prefixes=self._mod_strip_prefixes or None,
                allowed_extensions=self._install_extensions or None,
                normalize_folder_case=self._normalize_folder_case,
                exclude_dirs=self._filemap_exclude_dirs,
                log_fn=self._log,
                root_folder_mods=set(self._root_folder_mods) if self._root_folder_mods else None,
            )
        except Exception as exc:
            self._log(f"Root-folder toggle: index rescan failed — {exc}")

    def _on_sep_lock_toggle(self, sep_name: str) -> None:
        self._sep_locks[sep_name] = not self._sep_locks.get(sep_name, False)
        self._save_sep_locks()
        self._redraw()

    def _on_sep_lock_click(self, rect_id: int) -> None:
        """Resolve the separator name from its lock-checkbox canvas item id.
        Captured-name lambdas go stale across rename — look up live instead."""
        for name, rid in self._lock_cb_rects.items():
            if rid == rect_id:
                self._on_sep_lock_toggle(name)
                return

    def _set_sep_locks(self, sep_names: list[str], locked: bool) -> None:
        changed = False
        for n in sep_names:
            if self._sep_locks.get(n, False) != locked:
                self._sep_locks[n] = locked
                changed = True
        if changed:
            self._save_sep_locks()
            self._redraw()

    def _toggle_collapse(self, sep_name: str) -> None:
        if sep_name in self._collapsed_seps:
            self._collapsed_seps.discard(sep_name)
        else:
            self._collapsed_seps.add(sep_name)
        self._save_collapsed()
        self._update_expand_collapse_all_btn()
        self._invalidate_derived_caches()
        self._redraw()

    def _toggleable_separator_names(self) -> list[str]:
        """Separator names that can be collapsed (excludes Overwrite and Root Folder)."""
        return [e.name for e in self._entries
                if e.is_separator and e.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)]

    def _update_expand_collapse_all_btn(self) -> None:
        if not getattr(self, "_expand_collapse_all_btn", None):
            return
        sep_names = self._toggleable_separator_names()
        if not sep_names:
            self._expand_collapse_all_btn.configure(text="Expand all")
            return
        any_collapsed = any(s in self._collapsed_seps for s in sep_names)
        self._expand_collapse_all_btn.configure(
            text="Expand all" if any_collapsed else "Collapse all"
        )

    def _toggle_all_separators(self) -> None:
        sep_names = self._toggleable_separator_names()
        if not sep_names:
            return
        sep_set = set(sep_names)
        if all(s in self._collapsed_seps for s in sep_names):
            self._collapsed_seps -= sep_set
        else:
            self._collapsed_seps |= sep_set
        self._save_collapsed()
        self._update_expand_collapse_all_btn()
        self._invalidate_derived_caches()
        self._redraw()

    def _update_enable_disable_all_btn(self) -> None:
        if not getattr(self, "_enable_disable_all_btn", None):
            return
        mod_entries = [e for e in self._entries if not e.is_separator]
        all_enabled = mod_entries and all(e.enabled for e in mod_entries)
        self._enable_disable_all_btn.configure(
            text="Disable all" if all_enabled else "Enable all"
        )

    def _toggle_all_mods_enabled(self) -> None:
        mod_indices = [i for i, e in enumerate(self._entries) if not e.is_separator]
        if not mod_indices:
            return
        all_enabled = all(self._entries[i].enabled for i in mod_indices)
        any_enabled = any(self._entries[i].enabled for i in mod_indices)
        mixed = any_enabled and not all_enabled
        new_state = not all_enabled
        if mixed:
            action = "enable" if new_state else "disable"
            alert = CTkAlert(
                state="warning",
                title="Mixed Mod States",
                body_text=f"Some mods are enabled and some are disabled.\n\nDo you want to {action} all mods?",
                btn1=action.capitalize() + " all",
                btn2="Cancel",
                parent=self.winfo_toplevel(),
            )
            if alert.get() != action.capitalize() + " all":
                return
        for i in mod_indices:
            self._entries[i].enabled = new_state
            if i < len(self._check_vars) and self._check_vars[i] is not None:
                self._check_vars[i].set(new_state)
            self._sync_plugins_for_toggle(self._entries[i].name, new_state)
        self._vis_dirty = True
        self._mod_to_sep_idx = None
        self._save_modlist()
        self._rebuild_filemap()
        self._scan_missing_reqs_flags()
        self._update_enable_disable_all_btn()
        self._redraw()
        self._update_info()

    def _remove_plugins_for_mods(self, mod_names: list[str]) -> None:
        """Remove plugins belonging to the given mods from plugins.txt and loadorder.txt."""
        app = self.winfo_toplevel()
        pp = getattr(app, "_plugin_panel", None)
        if pp is None or pp._plugins_path is None:
            return
        plugin_exts = {e.lower() for e in getattr(pp, "_plugin_extensions", [])}
        if not plugin_exts:
            return
        to_remove: set[str] = set()
        for name in mod_names:
            staging = self._staging_root / name
            if staging.is_dir():
                for f in staging.iterdir():
                    if f.is_file() and f.suffix.lower() in plugin_exts:
                        to_remove.add(f.name.lower())
        if not to_remove:
            return
        existing = read_plugins(pp._plugins_path, star_prefix=pp._plugins_star_prefix)
        new_entries = [e for e in existing if e.name.lower() not in to_remove]
        if len(new_entries) < len(existing):
            write_plugins(pp._plugins_path, new_entries, star_prefix=pp._plugins_star_prefix)
        loadorder_path = pp._plugins_path.parent / "loadorder.txt"
        loadorder = read_loadorder(loadorder_path)
        new_lo = [n for n in loadorder if n.lower() not in to_remove]
        if len(new_lo) < len(loadorder):
            write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in new_lo])

    def _remove_separator(self, idx: int):
        if not (0 <= idx < len(self._entries) and self._entries[idx].is_separator):
            return
        flags = self._remove_separator_entry(idx)
        if flags["sep_locks"]:
            self._save_sep_locks()
        if flags["sep_colors"]:
            self._save_sep_colors()
        if flags["collapsed"]:
            self._save_collapsed()
        if flags["deploy_paths"]:
            self._save_sep_deploy_paths()
        self._update_expand_collapse_all_btn()
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _remove_separators(self, indices: list[int]):
        sep_locks_changed = False
        sep_colors_changed = False
        collapsed_changed = False
        deploy_paths_changed = False
        removed = False
        # Remove from highest index first to avoid shifting
        for i in sorted(indices, reverse=True):
            if not (0 <= i < len(self._entries) and self._entries[i].is_separator):
                continue
            flags = self._remove_separator_entry(i)
            removed = True
            sep_locks_changed |= flags["sep_locks"]
            sep_colors_changed |= flags["sep_colors"]
            collapsed_changed |= flags["collapsed"]
            deploy_paths_changed |= flags["deploy_paths"]
        if not removed:
            return
        if sep_locks_changed:
            self._save_sep_locks()
        if sep_colors_changed:
            self._save_sep_colors()
        if collapsed_changed:
            self._save_collapsed()
        if deploy_paths_changed:
            self._save_sep_deploy_paths()
        self._update_expand_collapse_all_btn()
        self._sel_set = set()
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _remove_separator_entry(self, idx: int) -> dict:
        """Remove a single separator at *idx* without persisting/redrawing.
        Returns flags indicating which persisted state changed so the caller
        can coalesce save calls across a batch removal."""
        sname = self._entries[idx].name
        self._entries.pop(idx)
        self._check_vars.pop(idx)
        # Clean up lock canvas items for this separator
        if sname in self._lock_cb_rects:
            self._canvas.delete(self._lock_cb_rects.pop(sname))
        if sname in self._lock_cb_marks:
            self._canvas.delete(self._lock_cb_marks.pop(sname))
        sep_locks_changed = self._sep_locks.pop(sname, None) is not None
        sep_colors_changed = self._sep_colors.pop(sname, None) is not None
        collapsed_changed = sname in self._collapsed_seps
        self._collapsed_seps.discard(sname)
        if self._sel_idx == idx:
            self._sel_idx = -1
        elif self._sel_idx > idx:
            self._sel_idx -= 1
        # Shift selection set indices above the removed row
        if self._sel_set:
            self._sel_set = {
                (i - 1) if i > idx else i
                for i in self._sel_set
                if i != idx
            }
        # If this separator had a custom deploy location, restore any
        # backed-up originals for that path immediately.
        _deploy_info = self._sep_deploy_paths.pop(sname, None)
        deploy_paths_changed = _deploy_info is not None
        if _deploy_info:
            _custom_path_str = _deploy_info.get("path", "")
            if _custom_path_str and self._filemap_path is not None:
                restore_custom_deploy_backup_for_path(
                    self._filemap_path, Path(_custom_path_str)
                )
        return {
            "sep_locks": sep_locks_changed,
            "sep_colors": sep_colors_changed,
            "collapsed": collapsed_changed,
            "deploy_paths": deploy_paths_changed,
        }

    def _remove_mod(self, idx: int, skip_confirm: bool = False):
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if entry.is_separator:
            return

        # For bundle variants, remove all siblings together.
        bundle_indices: list[int] = []
        _entry_bundle = self._bundle_name_of(idx)
        if _entry_bundle:
            bundle_indices = sorted(
                self._bundle_groups.get(_entry_bundle, [idx]), reverse=True
            )
            bundle_name = _entry_bundle
            variant_labels = ", ".join(
                self._variant_name_of(i) or self._entries[i].name
                for i in sorted(bundle_indices)
            )
            if not skip_confirm:
                alert = CTkAlert(
                    state="warning",
                    title="Remove Bundle",
                    body_text=(
                        f"Remove all variants of bundle '{bundle_name}'?\n\n"
                        f"Variants: {variant_labels}\n\n"
                        "This will delete all variant folders and cannot be undone."
                    ),
                    btn1="Remove",
                    btn2="Cancel",
                    parent=self.winfo_toplevel(),
                )
                if alert.get() != "Remove":
                    return
        else:
            bundle_indices = [idx]
            if not skip_confirm:
                alert = CTkAlert(
                    state="warning",
                    title="Remove Mod",
                    body_text=f"Are you sure you want to remove '{entry.name}'?\n\nThis will delete the mod folder and cannot be undone.",
                    btn1="Remove",
                    btn2="Cancel",
                    parent=self.winfo_toplevel(),
                )
                if alert.get() != "Remove":
                    return
        # Delete staging folders and drop from index — process highest index first
        # so earlier indices remain valid while popping.
        removed_names: list[str] = []
        index_path = self._staging_root.parent / "modindex.bin"
        all_names = [self._entries[i].name for i in bundle_indices]
        if self._modlist_path is not None:
            if self._game is not None:
                undeploy_mod_files(
                    all_names,
                    self._game.get_mod_data_path(),
                    self._game.get_game_path(),
                    index_path,
                )
            self._remove_plugins_for_mods(all_names)
            for rem_idx in bundle_indices:  # already sorted high→low
                rem_entry = self._entries[rem_idx]
                staging = self._staging_root / rem_entry.name
                if staging.is_dir():
                    shutil.rmtree(staging)
                removed_names.append(rem_entry.name)
            remove_from_mod_index(index_path, all_names)
            # Also drop from the BSA index if the game uses archive conflicts.
            remove_from_bsa_index(index_path.parent / "bsa_index.bin", all_names)
        # Remove from lists (highest index first to keep lower indices stable)
        for rem_idx in bundle_indices:
            self._entries.pop(rem_idx)
            self._check_vars.pop(rem_idx)
            if self._sel_idx == rem_idx:
                self._sel_idx = -1
            elif self._sel_idx > rem_idx:
                self._sel_idx -= 1
        self._compute_bundle_groups()
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._scan_missing_reqs_flags()
        self._redraw()
        self._update_info()
        label = _entry_bundle or entry.name
        _show_mod_notification(self.winfo_toplevel(), f"Removed: {label}", state="warning")

    def _sort_selected_alphabetically(self, indices: list[int]):
        """Reorder the selected mods alphabetically (case-insensitive).

        Mods are sorted only within their own separator group — a mod beneath
        separator A is never moved across to separator B's slots, even if both
        are part of the selection. Mods above the first separator form their
        own implicit group.

        Within each group, mods with loose-file conflicts are pushed to the
        bottom in their original relative order — never reordered, so the
        loose-file load order is preserved exactly. Only conflict-free mods
        are sorted alphabetically (placed above the conflict block).
        Separators and unselected mods between the slots stay in place.
        """
        slots = sorted(i for i in indices if 0 <= i < len(self._entries))
        if len(slots) < 2:
            return
        # Map each entry index to its parent-separator index (-1 for entries
        # above the first separator).
        group_of: dict[int, int] = {}
        cur_group = -1
        for i, e in enumerate(self._entries):
            if e.is_separator:
                cur_group = i
            else:
                group_of[i] = cur_group
        # Bucket the selected slots by separator group, preserving order.
        groups: dict[int, list[int]] = {}
        for s in slots:
            g = group_of.get(s, -1)
            groups.setdefault(g, []).append(s)

        total_sorted = 0
        total_conflict = 0
        any_changed = False
        # Compute the new ordering for each group independently.
        per_group_pairs: list[tuple[list[int], list[tuple[ModEntry, tk.BooleanVar | None]]]] = []
        for g, gslots in groups.items():
            if len(gslots) < 2:
                continue
            items: list[tuple[ModEntry, tk.BooleanVar | None]] = [
                (self._entries[i], self._check_vars[i]) for i in gslots
            ]
            no_conflict: list[tuple[ModEntry, tk.BooleanVar | None]] = []
            conflict_keep_order: list[tuple[ModEntry, tk.BooleanVar | None]] = []
            for e, cv in items:
                if self._conflict_map_base.get(e.name, CONFLICT_NONE) == CONFLICT_NONE:
                    no_conflict.append((e, cv))
                else:
                    conflict_keep_order.append((e, cv))
            no_conflict.sort(key=lambda pair: pair[0].name.casefold())
            new_pairs = no_conflict + conflict_keep_order
            if [e.name for e, _ in new_pairs] != [e.name for e, _ in items]:
                any_changed = True
            per_group_pairs.append((gslots, new_pairs))
            total_sorted += len(no_conflict)
            total_conflict += len(conflict_keep_order)

        if not any_changed:
            return

        # In reverse-priority mode the display inverts _entries order within each
        # group, so writing A→Z into ascending natural slots would render Z→A.
        # Reverse the computed order (and keep the conflict block at the visual
        # bottom = natural top) so the visible result is alphabetical.
        _inverted = (self._sort_column == "priority" and self._sort_ascending)

        saved_col, saved_asc = self._clear_sort()
        new_sel: set[int] = set()
        for gslots, new_pairs in per_group_pairs:
            _pairs = list(reversed(new_pairs)) if _inverted else new_pairs
            for slot, (e, cv) in zip(gslots, _pairs):
                self._entries[slot] = e
                self._check_vars[slot] = cv
                new_sel.add(slot)
        # Preserve any singleton-group slots that weren't reordered.
        for s in slots:
            new_sel.add(s)
        self._sel_set = new_sel
        if self._sel_idx not in self._sel_set:
            self._sel_idx = slots[0]
        self._restore_sort(saved_col, saved_asc)
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()
        self._log(
            f"Sorted {total_sorted} mod(s) alphabetically within their separator groups; "
            f"{total_conflict} with loose-file conflicts kept in original order."
        )

    def _enable_selected_mods(self, indices: list[int]):
        """Enable all mods at the given indices."""
        for i in indices:
            if 0 <= i < len(self._entries):
                self._entries[i].enabled = True
                if i < len(self._check_vars) and self._check_vars[i] is not None:
                    self._check_vars[i].set(True)
        self._vis_dirty = True
        self._mod_to_sep_idx = None
        self._save_modlist()
        self._rebuild_filemap()
        self._scan_missing_reqs_flags()
        self._redraw()
        self._update_info()

    def _disable_selected_mods(self, indices: list[int]):
        """Disable all mods at the given indices."""
        for i in indices:
            if 0 <= i < len(self._entries):
                self._entries[i].enabled = False
                if i < len(self._check_vars) and self._check_vars[i] is not None:
                    self._check_vars[i].set(False)
        self._vis_dirty = True
        self._mod_to_sep_idx = None
        self._save_modlist()
        self._rebuild_filemap()
        self._scan_missing_reqs_flags()
        self._redraw()
        self._update_info()

    def _remove_selected_mods(self, indices: list[int], skip_confirm: bool = False):
        """Remove multiple mods at once. Shows a confirmation unless *skip_confirm*
        is True — callers that have already confirmed (e.g. the collection
        update flow) should pass True to avoid a double-prompt."""
        names = [self._entries[i].name for i in indices
                 if 0 <= i < len(self._entries)]
        if not names:
            return
        if not skip_confirm:
            alert = CTkAlert(
                state="warning",
                title="Remove Mods",
                body_text=f"Are you sure you want to remove {len(names)} selected mod(s)?\n\nThis will delete the mod folders and cannot be undone.",
                btn1="Remove",
                btn2="Cancel",
                parent=self.winfo_toplevel(),
            )
            if alert.get() != "Remove":
                return
        staging_root = None
        index_path = None
        if self._modlist_path is not None:
            staging_root = self._staging_root
            index_path = self._staging_root.parent / "modindex.bin"
        removed_names: list[str] = []
        # Remove from highest index first to avoid shifting
        for i in sorted(indices, reverse=True):
            if not (0 <= i < len(self._entries)):
                continue
            entry = self._entries[i]
            if entry.is_separator:
                continue
            if staging_root is not None:
                removed_names.append(entry.name)
            self._entries.pop(i)
            self._check_vars.pop(i)
        if index_path is not None and removed_names:
            # Remove deployed files from the game directory before deleting the
            # staging folders so restore_data_core() doesn't misidentify the
            # leftover hardlinks/copies as runtime-generated files.
            if self._game is not None:
                undeploy_mod_files(
                    removed_names,
                    self._game.get_mod_data_path(),
                    self._game.get_game_path(),
                    index_path,
                )
            self._remove_plugins_for_mods(removed_names)
            # Now delete staging folders and update the index.
            for name in removed_names:
                staging = staging_root / name
                if staging.is_dir():
                    shutil.rmtree(staging)
            remove_from_mod_index(index_path, removed_names)
            # Also drop from the BSA index if the game uses archive conflicts.
            remove_from_bsa_index(index_path.parent / "bsa_index.bin", removed_names)
        self._sel_idx = -1
        self._sel_set = set()
        self._compute_bundle_groups()
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._scan_missing_reqs_flags()
        self._redraw()
        self._update_info()
        if removed_names:
            if len(removed_names) == 1:
                msg = f"Removed: {removed_names[0]}"
            else:
                msg = f"Removed {len(removed_names)} mods"
            _show_mod_notification(self.winfo_toplevel(), msg, state="warning")

    def _rename_mod(self, idx: int):
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if entry.is_separator:
            return
        top = self.winfo_toplevel()
        dlg = _RenameDialog(top, entry.name)
        top.wait_window(dlg)
        from gui.mod_name_utils import sanitize_mod_folder_name
        new_name = sanitize_mod_folder_name(dlg.result) if dlg.result else dlg.result
        if not new_name or new_name == entry.name:
            return
        # Rename staging folder on disk
        if self._modlist_path is not None:
            staging_root = self._staging_root
            old_folder = staging_root / entry.name
            new_folder = staging_root / new_name
            if old_folder.is_dir():
                if new_folder.exists():
                    show_error(
                        "Rename Failed",
                        f"A mod named '{new_name}' already exists.",
                        parent=top,
                    )
                    return
                old_folder.rename(new_folder)
        # Update entry in memory
        old_name = entry.name
        entry.name = new_name
        self._migrate_mod_name_state(old_name, new_name)
        self._vis_dirty = True  # name change affects text filter
        self._mod_to_sep_idx = None
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _migrate_mod_name_state(self, old_name: str, new_name: str) -> None:
        """Move all name-keyed state from *old_name* to *new_name*.

        Called by the two rename paths (_rename_mod / rename_mod_by_name).
        Covers:
          * modindex.bin            — so build_filemap finds the mod's files
          * transient in-memory sets/dicts used by the renderer
          * disk-backed per-mod state (strip prefixes, disabled plugins,
            excluded mod files) — each must be re-persisted because the
            on-disk keys are still the old name.

        Conflict tracking dicts (_conflict_map / _overrides / _overridden_by)
        are intentionally skipped; _rebuild_filemap() rewrites them from
        scratch on its next run.
        """
        # 1. modindex.bin — keyed by modlist name, not folder name
        if self._staging_root is not None:
            rename_in_mod_index(
                self._staging_root.parent / "modindex.bin",
                old_name, new_name,
                normalize_folder_case=self._normalize_folder_case,
            )

        # 2. Transient in-memory flags (no disk representation of their own;
        # rebuilt on reload, so just migrate the current snapshot).
        for s in (self._update_mods, self._missing_reqs, self._ignored_missing_reqs,
                  self._endorsed_mods, self._prertx_mods, self._fomod_mods,
                  self._bain_mods,
                  self._collection_bundled_mods, self._collection_patched_mods):
            if old_name in s:
                s.discard(old_name)
                s.add(new_name)
        for d in (self._missing_reqs_detail, self._install_dates,
                  self._install_datetimes, self._category_names, self._mod_versions,
                  self._mod_sizes, self._mod_size_bytes):
            if old_name in d:
                d[new_name] = d.pop(old_name)

        # 3. Disk-backed per-mod state — migrate and re-persist.
        profile_dir = self._modlist_path.parent if self._modlist_path is not None else None

        if old_name in self._mod_strip_prefixes:
            self._mod_strip_prefixes[new_name] = self._mod_strip_prefixes.pop(old_name)
            if profile_dir is not None:
                try:
                    write_mod_strip_prefixes(profile_dir, self._mod_strip_prefixes)
                except Exception as e:
                    self._log(f"Rename: failed to persist strip prefixes: {e}")

        if old_name in self._disabled_plugins_map:
            self._disabled_plugins_map[new_name] = self._disabled_plugins_map.pop(old_name)
            if profile_dir is not None:
                try:
                    write_disabled_plugins(profile_dir, self._disabled_plugins_map)
                except Exception as e:
                    self._log(f"Rename: failed to persist disabled plugins: {e}")

        if old_name in self._excluded_mod_files_map:
            self._excluded_mod_files_map[new_name] = self._excluded_mod_files_map.pop(old_name)
            if profile_dir is not None:
                try:
                    write_excluded_mod_files(profile_dir, self._excluded_mod_files_map)
                except Exception as e:
                    self._log(f"Rename: failed to persist excluded mod files: {e}")

        if old_name in self._mod_notes_map:
            self._mod_notes_map[new_name] = self._mod_notes_map.pop(old_name)
            if profile_dir is not None:
                try:
                    write_mod_notes(profile_dir, self._mod_notes_map)
                except Exception as e:
                    self._log(f"Rename: failed to persist mod notes: {e}")

    def rename_mod_by_name(self, old_name: str, new_name: str) -> bool:
        """Rename a mod by name (on disk + in-memory entry + persisted modlist).
        Returns True on success. Used by the post-install rename prompt so it
        doesn't need to know the mod's index in the list.
        """
        if not old_name or not new_name:
            return False
        from gui.mod_name_utils import sanitize_mod_folder_name
        new_name = sanitize_mod_folder_name(new_name)
        if not new_name or old_name == new_name:
            return False
        # Locate the entry by name.
        idx = -1
        for i, e in enumerate(self._entries):
            if not e.is_separator and e.name == old_name:
                idx = i
                break
        if idx < 0:
            return False
        if self._modlist_path is None:
            return False
        staging_root = self._staging_root
        old_folder = staging_root / old_name
        new_folder = staging_root / new_name
        if new_folder.exists():
            return False
        if old_folder.is_dir():
            try:
                old_folder.rename(new_folder)
            except OSError:
                return False
        entry = self._entries[idx]
        entry.name = new_name
        self._migrate_mod_name_state(old_name, new_name)
        self._vis_dirty = True
        self._mod_to_sep_idx = None
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()
        return True

    def _unique_separator_name(self, base_name: str, *, ignore_idx: int | None = None) -> str:
        """Return base_name suffixed with " (N)" before "_separator" so no other
        separator entry shares the name. Separator name is the primary key for
        lock/collapse/color/canvas-item dicts, so duplicates would collide."""
        existing = {
            e.name for i, e in enumerate(self._entries)
            if e.is_separator and i != ignore_idx
        }
        if base_name not in existing:
            return base_name
        suffix = "_separator"
        stem = base_name[:-len(suffix)] if base_name.endswith(suffix) else base_name
        n = 2
        while True:
            candidate = f"{stem} ({n}){suffix}"
            if candidate not in existing:
                return candidate
            n += 1

    def _rename_separator(self, idx: int):
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if not entry.is_separator:
            return
        top = self.winfo_toplevel()
        dlg = _RenameDialog(top, entry.display_name)
        top.wait_window(dlg)
        new_display = dlg.result
        if not new_display:
            return
        new_name = self._unique_separator_name(new_display + "_separator", ignore_idx=idx)
        if new_name == entry.name:
            return
        # Update collapse/lock tracking keys
        old_name = entry.name
        if old_name in self._collapsed_seps:
            self._collapsed_seps.discard(old_name)
            self._collapsed_seps.add(new_name)
            self._save_collapsed()
            self._update_expand_collapse_all_btn()
        if old_name in self._sep_locks:
            self._sep_locks[new_name] = self._sep_locks.pop(old_name)
            self._save_sep_locks()
        if old_name in self._sep_colors:
            self._sep_colors[new_name] = self._sep_colors.pop(old_name)
            self._save_sep_colors()
        if old_name in self._lock_cb_rects:
            self._lock_cb_rects[new_name] = self._lock_cb_rects.pop(old_name)
        if old_name in self._lock_cb_marks:
            self._lock_cb_marks[new_name] = self._lock_cb_marks.pop(old_name)
        entry.name = new_name
        self._save_modlist()
        self._redraw()

    def _change_separator_color(self, idx: int) -> None:
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if not entry.is_separator:
            return
        current = self._sep_colors.get(entry.name) or None
        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_sep_color_panel", None)
        if show_fn:
            def _on_result(hex_color, reset):
                if reset:
                    self._sep_colors.pop(entry.name, None)
                    self._save_sep_colors()
                    self._redraw()
                elif hex_color is not None:
                    self._sep_colors[entry.name] = hex_color
                    self._save_sep_colors()
                    self._redraw()
            show_fn(entry.name, current, _on_result)

    def _show_sep_settings(self, idx: int) -> None:
        """Open the separator settings panel (overlays the plugin panel)."""
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if not entry.is_separator:
            return
        sep_name = entry.name
        _info = self._sep_deploy_paths.get(sep_name, {})
        current_path = _info.get("path", "") if isinstance(_info, dict) else ""
        current_raw = _info.get("raw", False) if isinstance(_info, dict) else False
        current_mode = _info.get("mode", "") if isinstance(_info, dict) else ""
        current_merge = _info.get("merge", False) if isinstance(_info, dict) else False

        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_sep_settings_panel", None)
        if show_fn:
            def _on_save(val: str, raw: bool, mode: str = "", merge: bool = False):
                if val or raw or mode or merge:
                    self._sep_deploy_paths[sep_name] = {
                        "path": val, "raw": raw, "mode": mode, "merge": merge,
                    }
                else:
                    self._sep_deploy_paths.pop(sep_name, None)
                self._save_sep_deploy_paths()
                self._redraw()
            show_fn(sep_name, current_path, _on_save,
                    current_raw=current_raw, current_mode=current_mode,
                    current_merge=current_merge)
            return

        # Fallback: plain tk overlay on self (uses portal_filechooser for Browse)
        self._close_sep_settings()
        overlay = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=0)
        path_var = tk.StringVar(value=current_path)

        title_bar = tk.Frame(overlay, bg=BG_HEADER, height=scaled(36))
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        tk.Label(title_bar, text=f"Separator Settings \u2014 {sep_name}",
                 bg=BG_HEADER, fg=TEXT_MAIN, font=_theme.TK_FONT_BOLD, anchor="w",
        ).pack(side="left", padx=12, pady=8)

        content = tk.Frame(overlay, bg=BG_PANEL)
        content.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(content, text="Deployment Location", bg=BG_PANEL, fg=TEXT_SEP,
                 font=_theme.TK_FONT_SMALL, anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        entry_w = tk.Entry(content, textvariable=path_var, bg=BG_ENTRY, fg=TEXT_MAIN,
                           insertbackground=TEXT_MAIN, relief="flat", font=_theme.TK_FONT_SMALL)
        entry_w.grid(row=1, column=0, sticky="ew", padx=(0, 6))

        def _browse():
            from Utils.portal_filechooser import pick_folder
            def _cb(chosen):
                if chosen is not None:
                    def _apply():
                        path_var.set(str(chosen))
                    overlay.after(0, _apply)
            pick_folder("Select deployment directory", _cb)

        tk.Button(content, text="Browse", command=_browse, bg=BG_HEADER, fg=TEXT_MAIN,
                  relief="flat", font=_theme.TK_FONT_SMALL, cursor="hand2",
        ).grid(row=1, column=1, padx=(0, 4))
        tk.Button(content, text="Clear", command=lambda: path_var.set(""), bg=BG_HEADER,
                  fg=TEXT_MAIN, relief="flat", font=_theme.TK_FONT_SMALL, cursor="hand2",
        ).grid(row=1, column=2)
        content.columnconfigure(0, weight=1)
        tk.Label(content, text="Leave blank to use the game\u2019s default deployment directory.",
                 bg=BG_PANEL, fg=TEXT_SEP, font=_theme.TK_FONT_SMALL, anchor="w",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        btn_row = tk.Frame(overlay, bg=BG_PANEL)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=12)

        def _save():
            val = path_var.get().strip()
            if val:
                self._sep_deploy_paths[sep_name] = {"path": val, "raw": False}
            else:
                self._sep_deploy_paths.pop(sep_name, None)
            self._save_sep_deploy_paths()
            self._redraw()
            self._close_sep_settings()

        tk.Button(btn_row, text="Save", command=_save, bg=BG_BTN_SAVE, fg=TEXT_MAIN,
                  relief="flat", font=_theme.TK_FONT_SMALL, cursor="hand2", width=10,
        ).pack(side="right", padx=(6, 0))
        tk.Button(btn_row, text="Cancel", command=self._close_sep_settings, bg=BG_HEADER,
                  fg=TEXT_MAIN, relief="flat", font=_theme.TK_FONT_SMALL, cursor="hand2", width=10,
        ).pack(side="right")

        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._sep_settings_overlay = overlay
        entry_w.focus_set()

    def _close_sep_settings(self) -> None:
        panel = getattr(self, "_sep_settings_overlay", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._sep_settings_overlay = None

    def _show_picker_popup(self, items: list, displays: list[str],
                           on_pick, parent_dismiss=None,
                           parent_popup=None) -> tk.Toplevel:
        """Generic scrollable picker popup. *items* and *displays* are parallel lists.
        *on_pick(item)* is called when the user clicks an entry.
        Returns the popup widget."""
        popup = tk.Toplevel(self._canvas)
        popup.wm_withdraw()
        popup.wm_overrideredirect(True)
        popup.configure(bg=BORDER)
        cx, cy = popup.winfo_pointerxy()

        _alive = [True]

        def _pick_item(item):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()
                if parent_dismiss:
                    parent_dismiss()
                on_pick(item)

        ROW_H    = scaled(30)
        MAX_ROWS = 20
        FONT     = (_theme.FONT_FAMILY, _theme.FS11)
        PAD_X    = scaled(24)

        fnt = tkfont.Font(font=FONT)
        max_text_w = max((fnt.measure(d) for d in displays), default=100)
        popup_w = max_text_w + PAD_X * 2

        needs_scroll = len(items) > MAX_ROWS
        visible_rows = min(len(items), MAX_ROWS)
        popup_h      = visible_rows * ROW_H

        outer = tk.Frame(popup, bg=BORDER, bd=0)
        outer.pack(padx=1, pady=1)

        if needs_scroll:
            canvas = tk.Canvas(outer, bg=BG_PANEL, bd=0, highlightthickness=0,
                               width=popup_w, height=popup_h)
            vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                               bg=_theme.BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
                               highlightthickness=0, bd=0)
            canvas.configure(yscrollcommand=vsb.set)
            canvas.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            inner = tk.Frame(canvas, bg=BG_PANEL, bd=0)
            canvas_window = canvas.create_window((0, 0), window=inner, anchor="nw")

            def _on_inner_resize(e):
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.itemconfigure(canvas_window, width=canvas.winfo_width())
            inner.bind("<Configure>", _on_inner_resize)

            def _on_wheel(evt):
                if getattr(evt, "delta", 0) > 0:
                    canvas.yview_scroll(-3, "units")
                else:
                    canvas.yview_scroll(3, "units")

            def _bind_scroll(widget):
                if not LEGACY_WHEEL_REDUNDANT:
                    widget.bind("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
                    widget.bind("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))
                widget.bind("<MouseWheel>", _on_wheel)

            for w in (canvas, vsb, inner, outer, popup):
                _bind_scroll(w)
        else:
            inner = tk.Frame(outer, bg=BG_PANEL, bd=0, width=popup_w)
            inner.pack(fill="both", expand=True)
            _bind_scroll = None

        for item, display in zip(items, displays):
            btn = tk.Label(
                inner, text=display, anchor="w",
                bg=BG_PANEL, fg=TEXT_MAIN,
                font=FONT,
                padx=scaled(12), pady=scaled(5), cursor="hand2",
                width=0,
            )
            btn.pack(fill="x")
            btn.bind("<ButtonRelease-1>", lambda _e, it=item: _pick_item(it))
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=BG_SELECT))
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=BG_PANEL))
            if _bind_scroll:
                _bind_scroll(btn)

        popup.update_idletasks()
        pw = popup.winfo_reqwidth()
        ph = popup.winfo_reqheight()
        _app_tl = self.winfo_toplevel()
        app_right  = _app_tl.winfo_rootx() + _app_tl.winfo_width()
        app_bottom = _app_tl.winfo_rooty() + _app_tl.winfo_height()
        if parent_popup is not None:
            px = parent_popup.winfo_rootx() + parent_popup.winfo_width()
            py = cy - ph // 2
        else:
            px = cx
            py = cy
        px = min(px, app_right - pw)
        py = min(py, app_bottom - ph)
        px = max(px, 0)
        py = max(py, 0)
        popup.wm_geometry(f"+{px}+{py}")
        popup.wm_deiconify()

        return popup

    def _show_separator_picker(self, mod_idx: int, sep_names: list[str],
                               parent_dismiss=None,
                               parent_popup=None) -> tk.Toplevel:
        """Show a popup listing all separators; clicking one moves the mod below it."""
        displays = [
            name[:-len("_separator")] if name.endswith("_separator") else name
            for name in sep_names
        ]
        return self._show_picker_popup(
            sep_names, displays,
            on_pick=lambda sep_name: self._move_to_separator(mod_idx, sep_name),
            parent_dismiss=parent_dismiss, parent_popup=parent_popup,
        )

    def _make_copy_progress_popup(self, target_profile: str, total: int) -> CTkProgressPopup:
        """Create a progress popup for a copy-to-profile operation."""
        popup = CTkProgressPopup(
            self.winfo_toplevel(),
            title=f"Copying to '{target_profile}'",
            label="Starting…",
            message=f"0 / {total}",
        )
        # The X button on CTkProgressPopup sets `cancelled` and destroys the window.
        # The worker thread checks `popup.cancelled` between mods, but mid-copy the
        # current shutil.copytree finishes before we stop.
        return popup

    def _finish_copy_popup(self, popup: CTkProgressPopup, copied: int, skipped: int,
                           target_profile: str, copied_names: list[str]) -> None:
        """Log result and close the copy progress popup."""
        self._log(
            f"Copied {copied} mod(s) → profile '{target_profile}'"
            + (f" ({skipped} skipped)" if skipped else "")
        )
        if popup.winfo_exists():
            popup.destroy()

    def _show_copy_to_profile_picker(self, mod_name: str, profiles: list[str],
                                     parent_dismiss=None,
                                     parent_popup=None) -> tk.Toplevel:
        """Show a popup listing other profiles; clicking one copies the mod there."""
        return self._show_picker_popup(
            profiles, profiles,
            on_pick=lambda profile: self._copy_mod_to_profile(mod_name, profile),
            parent_dismiss=parent_dismiss, parent_popup=parent_popup,
        )

    def _copy_mod_to_profile(self, mod_name: str, target_profile: str) -> None:
        """Copy a mod's staging folder to another profile's staging folder."""
        if self._game is None or self._modlist_path is None:
            return
        src_folder = self._staging_root / mod_name
        if not src_folder.is_dir():
            show_error("Error", f"Mod folder not found:\n{src_folder}", parent=self.winfo_toplevel())
            return

        # Determine the target staging root
        game = self._game
        profile_root = game.get_profile_root()
        target_profile_dir = profile_root / "profiles" / target_profile
        from gui.game_helpers import profile_uses_specific_mods
        if profile_uses_specific_mods(target_profile_dir):
            target_staging = target_profile_dir / "mods"
        else:
            target_staging = game.get_mod_staging_path()

        dest_folder = target_staging / mod_name

        if dest_folder.exists():
            rename_conflict: str | None = None
            while True:
                dlg = _ReplaceModDialog(self.winfo_toplevel(), mod_name,
                                        rename_conflict=rename_conflict)
                self.wait_window(dlg)
                if dlg.result == "cancel":
                    return
                if dlg.result != "rename":
                    break
                new_name = dlg.new_name
                if not new_name:
                    return
                candidate = target_staging / new_name
                if not candidate.exists():
                    dest_folder = candidate
                    break
                rename_conflict = new_name
            if dlg.result == "all":
                def _force_remove(func, path, _exc):
                    os.chmod(path, 0o700)
                    func(path)
                shutil.rmtree(dest_folder, onexc=_force_remove)

        src_profile_dir = self._modlist_path.parent
        popup = self._make_copy_progress_popup(target_profile, total=1)
        popup.update_label(mod_name)
        popup.update_message(f"0 / 1")

        def _do_copy():
            try:
                shutil.copytree(str(src_folder), str(dest_folder))
                _copy_fomod_choice(src_profile_dir, target_profile_dir, mod_name)
                self.after(0, lambda: self._finish_copy_popup(
                    popup, 1, 0, target_profile, [mod_name]))
            except Exception as exc:
                self.after(0, lambda e=exc: (
                    self._finish_copy_popup(popup, 0, 0, target_profile, []),
                    show_error("Copy Failed", f"Failed to copy mod:\n{e}",
                               parent=self.winfo_toplevel()),
                ))

        threading.Thread(target=_do_copy, daemon=True).start()

    def _show_copy_to_profile_picker_multi(self, mod_names: list[str], profiles: list[str],
                                          parent_dismiss=None,
                                          parent_popup=None) -> tk.Toplevel:
        """Show a popup listing other profiles; clicking one copies all mods there."""
        return self._show_picker_popup(
            profiles, profiles,
            on_pick=lambda profile: self._copy_mods_to_profile(mod_names, profile),
            parent_dismiss=parent_dismiss, parent_popup=parent_popup,
        )

    def _copy_mods_to_profile(self, mod_names: list[str], target_profile: str) -> None:
        """Copy multiple mods' staging folders to another profile's staging folder."""
        if self._game is None or self._modlist_path is None:
            return
        game = self._game
        profile_root = game.get_profile_root()
        target_profile_dir = profile_root / "profiles" / target_profile
        from gui.game_helpers import profile_uses_specific_mods
        if profile_uses_specific_mods(target_profile_dir):
            target_staging = target_profile_dir / "mods"
        else:
            target_staging = game.get_mod_staging_path()

        # Pre-check which destinations already exist and confirm once.
        existing = [m for m in mod_names if (target_staging / m).exists()]
        replace_existing = False
        if existing:
            replace_existing = ask_yes_no(
                self.winfo_toplevel(),
                f"{len(existing)} mod(s) already exist in profile '{target_profile}':\n\n"
                + "\n".join(existing[:10])
                + ("\n…" if len(existing) > 10 else "")
                + "\n\nReplace them? (No = skip existing)",
                title="Mods Exist",
            )

        # Build ordered list of (name, enabled) from source entries
        _entry_map = {e.name: e for e in self._entries}
        _ordered_mods = [
            (mn, _entry_map[mn].enabled if mn in _entry_map else True)
            for mn in mod_names
        ]
        src_profile_dir = self._modlist_path.parent
        src_staging_root = self._staging_root
        total = len(_ordered_mods)
        popup = self._make_copy_progress_popup(target_profile, total=total)

        def _update_popup(done: int, current: str):
            if not popup.winfo_exists() or popup.cancelled:
                return
            popup.update_label(current)
            popup.update_message(f"{done} / {total}")
            popup.update_progress(done / total if total else 1.0)

        def _do_copy():
            copied, skipped = 0, 0
            copied_mods: list[tuple[str, bool]] = []
            for idx, (mod_name, enabled) in enumerate(_ordered_mods):
                if popup.winfo_exists() and popup.cancelled:
                    break
                self.after(0, _update_popup, idx, mod_name)
                src_folder = src_staging_root / mod_name
                if not src_folder.is_dir():
                    skipped += 1
                    continue
                dest_folder = target_staging / mod_name
                try:
                    if dest_folder.exists():
                        if not replace_existing:
                            skipped += 1
                            continue
                        def _force_remove(func, path, _exc):
                            os.chmod(path, 0o700)
                            func(path)
                        shutil.rmtree(dest_folder, onexc=_force_remove)
                    shutil.copytree(str(src_folder), str(dest_folder))
                    _copy_fomod_choice(src_profile_dir, target_profile_dir, mod_name)
                    copied += 1
                    copied_mods.append((mod_name, enabled))
                except Exception as exc:
                    self.after(0, lambda e=exc, n=mod_name: (
                        self._finish_copy_popup(popup, copied, skipped,
                                                target_profile, [m for m, _ in copied_mods]),
                        show_error("Copy Failed", f"Failed to copy '{n}':\n{e}",
                                   parent=self.winfo_toplevel()),
                    ))
                    return

            # Insert copied mods into the target profile's modlist,
            # preserving their relative order from the source profile.
            if copied_mods:
                target_modlist = target_profile_dir / "modlist.txt"
                from Utils.modlist import read_modlist, write_modlist, ModEntry
                entries = read_modlist(target_modlist) if target_modlist.exists() else []
                existing_names = {e.name for e in entries}
                # Prepend in order (low index = high priority, matching source)
                new_entries = [
                    ModEntry(name=mn, enabled=en, locked=False)
                    for mn, en in copied_mods
                    if mn not in existing_names
                ]
                if new_entries:
                    entries = new_entries + entries
                    write_modlist(target_modlist, entries)

            self.after(0, lambda c=copied, s=skipped: self._finish_copy_popup(
                popup, c, s, target_profile, [m for m, _ in copied_mods]))

        threading.Thread(target=_do_copy, daemon=True).start()

    def _show_separator_picker_multi(self, indices: list[int], sep_names: list[str],
                                     parent_dismiss=None,
                                     parent_popup=None) -> tk.Toplevel:
        """Like _show_separator_picker but moves all mods at `indices` to the chosen separator."""
        displays = [
            name[:-len("_separator")] if name.endswith("_separator") else name
            for name in sep_names
        ]
        return self._show_picker_popup(
            sep_names, displays,
            on_pick=lambda sep_name: self._move_selected_to_separator(indices, sep_name),
            parent_dismiss=parent_dismiss, parent_popup=parent_popup,
        )

    def _show_disable_plugins_dialog(self, mod_name: str, plugin_files: list[str]) -> None:
        """Open the Disable Plugins panel/dialog for a mod and save results."""
        if self._modlist_path is None:
            return
        profile_dir = self._modlist_path.parent
        all_disabled = read_disabled_plugins(profile_dir, self.__profile_state)
        currently_disabled = set(all_disabled.get(mod_name, []))

        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_disable_plugins_panel", None)
        if show_fn:
            def _on_panel_done(panel):
                if panel.result is None:
                    return
                self._finish_disable_plugins(mod_name, panel.result, currently_disabled,
                                             profile_dir, all_disabled)
            show_fn(mod_name, plugin_files, currently_disabled, _on_panel_done)
        else:
            dlg = _DisablePluginsDialog(
                self.winfo_toplevel(),
                mod_name=mod_name,
                plugin_names=plugin_files,
                disabled=currently_disabled,
            )
            self.wait_window(dlg)
            if dlg.result is None:
                return
            self._finish_disable_plugins(mod_name, dlg.result, currently_disabled,
                                         profile_dir, all_disabled)

    def _finish_disable_plugins(self, mod_name, result, currently_disabled,
                                profile_dir, all_disabled):
        """Persist disable-plugins result and update plugins.txt immediately."""
        if result:
            all_disabled[mod_name] = sorted(result)
        else:
            all_disabled.pop(mod_name, None)

        # Compute which plugins for this mod were just re-enabled vs newly disabled
        newly_disabled = result - currently_disabled
        newly_enabled  = currently_disabled - result

        write_disabled_plugins(profile_dir, all_disabled)
        self._disabled_plugins_map = all_disabled
        # Keep snapshot in sync so reads that use __profile_state see all mods' disables
        self.__profile_state = read_profile_state(profile_dir)

        # Immediately update plugins.txt and refresh the panel without waiting
        # for the async filemap rebuild to complete.
        app = self.winfo_toplevel()
        if hasattr(app, "_plugin_panel"):
            pp = app._plugin_panel
            if pp._plugins_path is not None:
                changed = False
                existing = read_plugins(pp._plugins_path, star_prefix=pp._plugins_star_prefix)

                loadorder_path = pp._plugins_path.parent / "loadorder.txt"
                loadorder = read_loadorder(loadorder_path)
                lo_changed = False

                # Remove newly-disabled plugins from both plugins.txt and loadorder.txt
                if newly_disabled:
                    disabled_lower = {n.lower() for n in newly_disabled}
                    kept = [e for e in existing if e.name.lower() not in disabled_lower]
                    if len(kept) < len(existing):
                        existing = kept
                        changed = True
                    new_lo = [n for n in loadorder if n.lower() not in disabled_lower]
                    if len(new_lo) < len(loadorder):
                        loadorder = new_lo
                        lo_changed = True

                # Append newly re-enabled plugins to bottom of plugins.txt and loadorder.txt
                if newly_enabled:
                    existing_lower = {e.name.lower() for e in existing}
                    loadorder_lower = {n.lower() for n in loadorder}
                    for name in sorted(newly_enabled):
                        if name.lower() not in existing_lower:
                            existing.append(PluginEntry(name=name, enabled=True))
                            existing_lower.add(name.lower())
                            changed = True
                        if name.lower() not in loadorder_lower:
                            loadorder.append(name)
                            loadorder_lower.add(name.lower())
                            lo_changed = True

                if lo_changed:
                    write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in loadorder])

                if changed:
                    write_plugins(pp._plugins_path, existing, star_prefix=pp._plugins_star_prefix)
                    pp._refresh_plugins_tab()

        self._rebuild_filemap()

    def _show_ini_picker(self, ini_files: list[Path],
                         parent_dismiss=None,
                         parent_popup=None) -> tk.Toplevel:
        """Show a submenu listing all INI files; clicking one opens it."""
        displays = [f"Open {p.name}" for p in ini_files]
        return self._show_picker_popup(
            ini_files, displays,
            on_pick=lambda ini_path: self._open_ini(ini_path),
            parent_dismiss=parent_dismiss, parent_popup=parent_popup,
        )

    def _move_to_separator(self, mod_idx: int, sep_name: str):
        """Move the mod at mod_idx to directly below the named separator."""
        if not (0 <= mod_idx < len(self._entries)):
            return
        # Find the separator's current index
        sep_idx = next(
            (i for i, e in enumerate(self._entries)
             if e.is_separator and e.name == sep_name),
            None,
        )
        if sep_idx is None:
            return
        saved_col, saved_asc = self._clear_sort()

        # Pull the mod out
        entry = self._entries.pop(mod_idx)
        var   = self._check_vars.pop(mod_idx)

        # Recalculate sep_idx after removal
        if mod_idx < sep_idx:
            sep_idx -= 1

        # Insert directly below the separator
        dest = sep_idx + 1
        self._entries.insert(dest, entry)
        self._check_vars.insert(dest, var)

        self._sel_idx = dest
        self._restore_sort(saved_col, saved_asc)
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _move_selected_to_separator(self, indices: list[int], sep_name: str):
        """Move all mods at the given indices to directly below the named separator, grouped together."""
        # Snapshot the entries to move (in list order)
        to_move = []
        for i in sorted(indices):
            if 0 <= i < len(self._entries):
                to_move.append((self._entries[i], self._check_vars[i]))
        if not to_move:
            return
        saved_col, saved_asc = self._clear_sort()

        # Remove them from the list (highest index first to preserve lower indices)
        for i in sorted(indices, reverse=True):
            if 0 <= i < len(self._entries):
                self._entries.pop(i)
                self._check_vars.pop(i)

        # Find separator after removals
        sep_idx = next(
            (i for i, e in enumerate(self._entries)
             if e.is_separator and e.name == sep_name),
            None,
        )
        if sep_idx is None:
            # Separator was removed (shouldn't happen), put items back at end
            for entry, var in to_move:
                self._entries.append(entry)
                self._check_vars.append(var)
            self._restore_sort(saved_col, saved_asc)
            self._invalidate_derived_caches()
            self._save_modlist()
            self._rebuild_filemap()
            self._redraw()
            self._update_info()
            return

        # Insert all mods directly below the separator, preserving their relative order
        dest = sep_idx + 1
        for j, (entry, var) in enumerate(to_move):
            self._entries.insert(dest + j, entry)
            self._check_vars.insert(dest + j, var)

        # Update selection to the moved block
        self._sel_set = set(range(dest, dest + len(to_move)))
        self._sel_idx = dest
        self._restore_sort(saved_col, saved_asc)
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _open_ini(self, path: Path):
        """Open an .ini file in the user's default text editor via xdg-open."""
        try:
            xdg_open(path)
            self._log(f"Opened: {path.name}")
        except Exception as e:
            self._log(f"Could not open {path.name}: {e}")

    def _open_folder(self, path: Path) -> None:
        """Open a directory in the system file manager via xdg-open."""
        if not path.is_dir():
            self._log(f"Folder not found: {path}")
            return
        try:
            xdg_open(path)
        except Exception as e:
            self._log(f"Could not open folder: {e}")

    def _on_middle_click(self, event) -> None:
        """Middle-click: open the hovered mod's Nexus page in the browser."""
        if not self._entries or self._modlist_path is None:
            return
        cy = self._event_canvas_y(event)
        idx = self._canvas_y_to_index(cy)
        entry = self._entries[idx]
        if entry.is_separator:
            return
        staging_root = self._staging_root
        meta_path = staging_root / entry.name / "meta.ini"
        if not meta_path.is_file():
            return
        try:
            meta = read_meta(meta_path)
        except Exception:
            return
        if meta.mod_id <= 0:
            return
        app = self.winfo_toplevel()
        _cur_game = _GAMES.get(getattr(
            getattr(app, "_topbar", None), "_game_var", tk.StringVar()).get(), None)
        # Prefer the mod's own domain (from meta.ini gamename) so cross-game
        # mods inside a collection (e.g. Skyrim mods in an Enderal collection)
        # open on the correct Nexus page. Fall back to the URL stored in the
        # meta, then to the current game's domain.
        domain = (
            meta.game_domain
            or (meta.nexus_page_url.split("/mods/")[0].rsplit("/", 1)[-1]
                if "/mods/" in meta.nexus_page_url else "")
            or (_cur_game.nexus_game_domain if _cur_game else "")
        )
        url = f"https://www.nexusmods.com/{domain}/mods/{meta.mod_id}"
        self._open_nexus_page(url)

    def _open_note_editor_by_name(self, mod_name: str) -> None:
        """Open the mod-note editor (overlay over the plugin panel) for a given mod."""
        if self._modlist_path is None:
            return
        profile_dir = self._modlist_path.parent
        initial = self._mod_notes_map.get(mod_name, "")

        def _save(text: str):
            text = text.strip()
            if text:
                self._mod_notes_map[mod_name] = text
            else:
                self._mod_notes_map.pop(mod_name, None)
            try:
                write_mod_notes(profile_dir, self._mod_notes_map)
            except Exception as e:
                self._log(f"Failed to save note for {mod_name}: {e}")
            self._redraw()

        def _remove():
            self._mod_notes_map.pop(mod_name, None)
            try:
                write_mod_notes(profile_dir, self._mod_notes_map)
            except Exception as e:
                self._log(f"Failed to remove note for {mod_name}: {e}")
            self._redraw()

        app = self.winfo_toplevel()
        pp = getattr(app, "_plugin_panel", None)
        if pp is None or not hasattr(pp, "show_notes_editor"):
            self._log("Cannot open note editor: plugin panel unavailable.")
            return
        pp.show_notes_editor(mod_name, initial, _save, _remove)

    def _open_note_editor_for_multi(self, mod_names: list[str]) -> None:
        """Open the mod-note editor with an empty initial buffer; on save the
        text is applied to every mod in *mod_names*. If a mod already has a
        note, the new text is appended on a new line (preserving the prior
        note); otherwise the new text becomes the note."""
        if self._modlist_path is None or not mod_names:
            return
        profile_dir = self._modlist_path.parent
        names = list(mod_names)
        title = f"{len(names)} mods"

        def _save(text: str):
            text = text.strip()
            if not text:
                return
            for mn in names:
                existing = self._mod_notes_map.get(mn, "").rstrip()
                if existing:
                    self._mod_notes_map[mn] = f"{existing}\n{text}"
                else:
                    self._mod_notes_map[mn] = text
            try:
                write_mod_notes(profile_dir, self._mod_notes_map)
            except Exception as e:
                self._log(f"Failed to save notes for {len(names)} mods: {e}")
            self._log(f"Note applied to {len(names)} mod(s).")
            self._redraw()

        def _remove():
            removed = 0
            for mn in names:
                if self._mod_notes_map.pop(mn, None) is not None:
                    removed += 1
            try:
                write_mod_notes(profile_dir, self._mod_notes_map)
            except Exception as e:
                self._log(f"Failed to remove notes: {e}")
            if removed:
                self._log(f"Removed note from {removed} mod(s).")
            self._redraw()

        app = self.winfo_toplevel()
        pp = getattr(app, "_plugin_panel", None)
        if pp is None or not hasattr(pp, "show_notes_editor"):
            self._log("Cannot open note editor: plugin panel unavailable.")
            return
        pp.show_notes_editor(title, "", _save, _remove)

    def _remove_notes_multi(self, mod_names: list[str]) -> None:
        """Remove the note from every mod in *mod_names* without opening the editor."""
        if self._modlist_path is None or not mod_names:
            return
        profile_dir = self._modlist_path.parent
        removed = 0
        for mn in mod_names:
            if self._mod_notes_map.pop(mn, None) is not None:
                removed += 1
        if not removed:
            return
        try:
            write_mod_notes(profile_dir, self._mod_notes_map)
        except Exception as e:
            self._log(f"Failed to remove notes: {e}")
            return
        self._log(f"Removed note from {removed} mod(s).")
        self._redraw()

    def _build_missing_req_spec(self, mod_name: str, default_domain: str):
        """Resolve (mod_id, domain, missing_ids) for one mod, or None on failure.

        Logs the reason and returns None when the mod has no usable meta.ini /
        Nexus mod-id / game domain.
        """
        meta_path = self._staging_root / mod_name / "meta.ini"
        if not meta_path.is_file():
            self._log(f"{mod_name}: No meta.ini found.")
            return None
        try:
            meta = read_meta(meta_path)
        except Exception:
            self._log(f"{mod_name}: Could not read meta.ini.")
            return None
        if meta.mod_id <= 0:
            self._log(f"{mod_name}: No Nexus mod ID.")
            return None
        domain = default_domain
        if not domain and "/mods/" in meta.nexus_page_url:
            domain = meta.nexus_page_url.split("/mods/")[0].rsplit("/", 1)[-1]
        if not domain:
            self._log(f"{mod_name}: Could not determine game domain.")
            return None

        missing_ids: set[int] = set()
        for pair in (meta.missing_requirements or "").split(";"):
            part = pair.split(":", 1)[0].strip()
            if part:
                try:
                    missing_ids.add(int(part))
                except ValueError:
                    pass
        return SimpleNamespace(
            mod_name=mod_name, mod_id=meta.mod_id,
            domain=domain, missing_ids=missing_ids,
        )

    def _missing_reqs_install_cb(self, app, api):
        """Build the install-from-browse callback used by the missing-reqs panel."""
        from gui.nexus_browser_overlay import install_nexus_mod_from_entry
        mod_panel = self
        game = self._game
        log_fn_install = self._log
        if api and game and game.is_configured():
            return (lambda entry: install_nexus_mod_from_entry(
                app, api, game, mod_panel, log_fn_install, entry))
        return None

    def _show_missing_reqs(self, mod_name: str, dep_names: list[str]) -> None:
        """Show missing requirements as an inline overlay over the plugin panel."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Nexus: Login to Nexus first.")
            return
        if self._modlist_path is None:
            self._log("No profile loaded.")
            return
        topbar = getattr(app, "_topbar", None)
        game = _GAMES.get(topbar._game_var.get()) if topbar else None
        domain = (game.nexus_game_domain if game and game.is_configured() else "") or ""

        spec = self._build_missing_req_spec(mod_name, domain)
        if spec is None:
            return

        if hasattr(app, "show_missing_reqs_panel"):
            app.show_missing_reqs_panel(
                mod_name=mod_name,
                domain=spec.domain,
                mod_id=spec.mod_id,
                missing_ids=spec.missing_ids,
                api=api,
                install_from_browse=self._missing_reqs_install_cb(app, api),
                ignored_set=self._ignored_missing_reqs,
                save_ignored_fn=self._save_ignored_missing_reqs,
                redraw_fn=self._redraw,
            )

    def _show_missing_reqs_multi(self, mod_names: list[str]) -> None:
        """Show the combined missing requirements of several selected mods.

        Aggregates every mod's requirements into one panel; the panel itself
        dedupes by requirement mod-id so a shared dependency is listed once.
        """
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Nexus: Login to Nexus first.")
            return
        if self._modlist_path is None:
            self._log("No profile loaded.")
            return
        topbar = getattr(app, "_topbar", None)
        game = _GAMES.get(topbar._game_var.get()) if topbar else None
        domain = (game.nexus_game_domain if game and game.is_configured() else "") or ""

        specs = []
        for mn in mod_names:
            spec = self._build_missing_req_spec(mn, domain)
            if spec is not None and spec.missing_ids:
                specs.append(spec)
        if not specs:
            self._log("No resolvable missing requirements in the selection.")
            return

        # Single resolvable mod → fall back to the normal single-mod panel.
        if len(specs) == 1:
            self._show_missing_reqs(specs[0].mod_name, [])
            return

        if hasattr(app, "show_missing_reqs_panel"):
            app.show_missing_reqs_panel(
                mod_name=f"{len(specs)} mods",
                domain=specs[0].domain,
                mod_id=specs[0].mod_id,
                missing_ids=specs[0].missing_ids,
                api=api,
                install_from_browse=self._missing_reqs_install_cb(app, api),
                ignored_set=self._ignored_missing_reqs,
                save_ignored_fn=self._save_ignored_missing_reqs,
                redraw_fn=self._redraw,
                mods=[
                    {"mod_name": s.mod_name, "mod_id": s.mod_id,
                     "domain": s.domain, "missing_ids": s.missing_ids}
                    for s in specs
                ],
            )

    def _show_overwrites_dialog(self, mod_name: str) -> None:
        """Open the conflict detail dialog for a mod (I/O runs in a background thread)."""
        if self._modlist_path is None:
            return
        # Snapshot state needed by the worker
        filemap_path = self._filemap_path
        staging_root = self._staging_root
        profile_dir = self._modlist_path.parent
        modlist_path = self._modlist_path
        strip_prefixes = set(self._strip_prefixes)
        beaten_mods = set(self._overrides.get(mod_name, set()))
        call_threadsafe = self._call_threadsafe
        from Games.ue5_game import UE5Game as _UE5Game
        _captured_game = getattr(self, "_game", None)
        _archive_exts: frozenset[str] = getattr(_captured_game, "archive_extensions", frozenset())
        bsa_index_path = (filemap_path.parent / "bsa_index.bin") if filemap_path else None
        mod_index_path = (staging_root.parent / "modindex.bin") if staging_root else None
        # Snapshot plugin load order so the BSA conflict replay in the worker
        # matches the engine load order used everywhere else.
        _plugin_order_snap: list[str] = []
        _plugin_exts_snap: frozenset[str] = frozenset()
        _pp = getattr(self.winfo_toplevel(), "_plugin_panel", None)
        if _pp is not None:
            _plugin_order_snap = [e.name for e in getattr(_pp, "_plugin_entries", []) if e.enabled]
            _plugin_exts_snap = frozenset(
                e.lower() for e in getattr(_pp, "_plugin_extensions", []) or []
            )
        _ckfn = None
        if isinstance(_captured_game, _UE5Game):
            def _ckfn(rel: str, _g=_captured_game) -> str:
                dest, final = _g._resolve_entry(rel)
                return ((dest + "/" + final) if dest else final).lower()

        def _worker():
            per_mod = load_per_mod_strip_prefixes(profile_dir)
            strip_lower = {s.lower() for s in strip_prefixes}

            def _strip_for(name: str, rel: str) -> str:
                """Strip prefixes the same way filemap.py does for a given mod."""
                mod_paths = sorted(
                    (p for p in per_mod.get(name, []) if "/" in p),
                    key=lambda p: -len(p),
                )
                if mod_paths:
                    rl = rel.lower()
                    for p in mod_paths:
                        pl = p.lower()
                        if rl.startswith(pl + "/"):
                            rel = rel[len(p) + 1:]
                            break
                        elif rl == pl:
                            rel = ""
                            break
                mod_segs = strip_lower | {s.lower() for s in per_mod.get(name, []) if "/" not in s}
                while "/" in rel and rel.split("/", 1)[0].lower() in mod_segs:
                    rel = rel.split("/", 1)[1]
                return rel

            # Build winner map from filemap.txt, keyed by deploy path (or staged path).
            # When _ckfn is set (UE5), remap via routing so cross-path conflicts are found.
            winning_map: dict[str, tuple[str, str]] = {}
            if filemap_path.is_file():
                with filemap_path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.rstrip("\n")
                        if "\t" not in line:
                            continue
                        rel_path, winner = line.split("\t", 1)
                        key = _ckfn(rel_path) if _ckfn else rel_path.lower()
                        winning_map[key] = (rel_path, winner)

            # Collect this mod's files. Prefer modindex.bin (already normalized
            # with the same strip logic filemap.py uses, so keys match filemap.txt
            # and other mods' index entries exactly); fall back to a staging walk.
            my_files: dict[str, str] = {}
            _my_index_entry = None
            if mod_index_path is not None and mod_index_path.is_file():
                try:
                    from Utils.filemap import read_mod_index as _read_mi
                    _mi = _read_mi(mod_index_path)
                    if _mi is not None:
                        _my_index_entry = _mi.get(mod_name)
                except Exception:
                    _my_index_entry = None
            if _my_index_entry is not None:
                _normal, _root = _my_index_entry
                for _k, _rel_str in _normal.items():
                    my_files[_k] = _rel_str
                for _k, _rel_str in _root.items():
                    my_files[_k] = _rel_str
            else:
                my_staging = (staging_root.parent / "overwrite"
                              if mod_name == OVERWRITE_NAME else staging_root / mod_name)
                if my_staging.is_dir():
                    for dirpath, _, fnames in os.walk(my_staging):
                        for fname in fnames:
                            if fname.lower() == "meta.ini":
                                continue
                            full = os.path.join(dirpath, fname)
                            rel = os.path.relpath(full, my_staging).replace("\\", "/")
                            rel = _strip_for(mod_name, rel)
                            if rel:
                                key = _ckfn(rel) if _ckfn else rel.lower()
                                my_files[key] = rel

            # Classify each file
            files_i_win: list[tuple[str, str]] = []
            files_i_lose: list[tuple[str, str]] = []
            for deploy_key, orig_rel in sorted(my_files.items()):
                if deploy_key in winning_map:
                    orig, winner = winning_map[deploy_key]
                    if winner == mod_name:
                        files_i_win.append((deploy_key, ""))
                    else:
                        files_i_lose.append((deploy_key, winner))
                else:
                    files_i_lose.append((deploy_key, "(no winner — disabled?)"))

            # Annotate wins: look up each beaten mod's files in modindex.bin.
            # The index already has normalized lowercase keys that match
            # filemap.txt, so no per-mod strip-prefix logic is needed here.
            rel_to_losers: dict[str, list[str]] = {}
            mod_index = None
            if mod_index_path is not None and mod_index_path.is_file():
                try:
                    from Utils.filemap import read_mod_index as _read_mi
                    mod_index = _read_mi(mod_index_path)
                except Exception:
                    mod_index = None
            if mod_index is not None:
                for loser_mod in beaten_mods:
                    entry = mod_index.get(loser_mod)
                    if not entry:
                        continue
                    normal_files, root_files = entry
                    for _key in normal_files:
                        if _key in my_files:
                            rel_to_losers.setdefault(_key, []).append(loser_mod)
                    for _key in root_files:
                        if _key in my_files:
                            rel_to_losers.setdefault(_key, []).append(loser_mod)
            # Annotate wins against BSA-only losers: when this mod's loose files
            # beat a lower-priority mod's BSA contents (engine rule: loose > BSA),
            # the BSA mod appears in beaten_mods but has no entry in the loose
            # mod_index. Consult bsa_index to surface those wins.
            if _archive_exts and bsa_index_path is not None and bsa_index_path.is_file():
                try:
                    from Utils.bsa_filemap import read_bsa_index as _read_bi
                    _bi = _read_bi(bsa_index_path) or {}
                    for loser_mod in beaten_mods:
                        archives = _bi.get(loser_mod)
                        if not archives:
                            continue
                        for _bsa, _mt, _paths in archives:
                            for _fp in _paths:
                                if _fp in my_files and loser_mod not in rel_to_losers.get(_fp, ()):
                                    rel_to_losers.setdefault(_fp, []).append(loser_mod)
                except Exception:
                    pass
            if mod_index is None:
                # Fallback: walk beaten mods' staging directly (older profiles
                # without a mod index).
                for loser_mod in beaten_mods:
                    loser_staging = staging_root / loser_mod
                    if not loser_staging.is_dir():
                        continue
                    for dirpath, _, fnames in os.walk(loser_staging):
                        for fname in fnames:
                            if fname.lower() == "meta.ini":
                                continue
                            full = os.path.join(dirpath, fname)
                            rel = _strip_for(loser_mod, os.path.relpath(full, loser_staging).replace("\\", "/"))
                            if rel:
                                key = _ckfn(rel) if _ckfn else rel.lower()
                                if key in my_files:
                                    rel_to_losers.setdefault(key, []).append(loser_mod)

            files_i_win_final: list[tuple[str, str]] = [
                (deploy_key, beaten_str)
                for deploy_key, _ in files_i_win
                if (beaten_str := ", ".join(rel_to_losers.get(deploy_key, [])))
            ]
            # Also include files where this mod beats a lower-priority mod but
            # ultimately loses to a higher-priority winner. The conflict engine
            # reports these as wins (this mod sits above the loser in the load
            # order for that path), so they belong in "Files overriding others"
            # — annotated so the user knows a higher mod still takes the file.
            _win_keys = {k for k, _ in files_i_win}
            for _lose_key, _ in files_i_lose:
                _losers_under = rel_to_losers.get(_lose_key)
                if _losers_under and _lose_key not in _win_keys:
                    files_i_win_final.append((_lose_key, ", ".join(_losers_under)))
            files_no_conflict: list[str] = [
                deploy_key
                for deploy_key, _ in files_i_win
                if not rel_to_losers.get(deploy_key)
            ]

            # BSA-vs-BSA conflicts — append rows from this mod's archives.
            # Each row's path is prefixed with the BSA filename so the user
            # can tell it comes from an archive, e.g. "SkyUI_SE.bsa : interface/foo.swf".
            if _archive_exts and bsa_index_path is not None and bsa_index_path.is_file():
                try:
                    from Utils.bsa_filemap import read_bsa_index, compute_bsa_winner_map
                    from Utils.modlist import read_modlist as _read_ml
                    bsa_index = read_bsa_index(bsa_index_path) or {}
                    entries_ml = _read_ml(modlist_path)
                    enabled_ml = [e for e in entries_ml if not e.is_separator and e.enabled]
                    priority_low_to_high = [e.name for e in reversed(enabled_ml)]

                    # Use the shared engine-load-order helper so the dialog
                    # winners match the modlist dot and the Archive tab.
                    bsa_winner, bsa_losers = compute_bsa_winner_map(
                        bsa_index, priority_low_to_high,
                        _plugin_order_snap or None, _plugin_exts_snap or None,
                        mod_index_path,
                    )

                    # Walk this mod's archives and classify each file.
                    # Engine behavior: a loose file at the same path always wins
                    # over any BSA entry, regardless of load order, so a BSA
                    # entry also loses whenever another mod has a loose file
                    # at that path.
                    my_archives = bsa_index.get(mod_name, [])
                    for _bsa_name, _mt, _paths in my_archives:
                        for _fp in sorted(_paths):
                            _display = f"{_bsa_name} : {_fp}"
                            winner = bsa_winner.get(_fp)
                            if winner is None:
                                continue
                            _loose = winning_map.get(_fp)
                            _loose_winner = _loose[1] if _loose else None
                            if _loose_winner is not None and _loose_winner != mod_name:
                                files_i_lose.append((_display, _loose_winner))
                                continue
                            if winner == mod_name:
                                _losers = [
                                    l for l in bsa_losers.get(_fp, []) if l != mod_name
                                ]
                                if _losers:
                                    files_i_win_final.append(
                                        (_display, ", ".join(_losers))
                                    )
                                else:
                                    files_no_conflict.append(_display)
                            else:
                                files_i_lose.append((_display, winner))
                except Exception:
                    pass

            # Dispatch results back to the main thread
            def _show():
                app = self.winfo_toplevel()
                show_fn = getattr(app, "show_conflicts_panel", None)
                if show_fn:
                    show_fn(mod_name, files_i_win_final, files_i_lose, files_no_conflict)
                else:
                    _OverwritesDialog(
                        app,
                        mod_name=mod_name,
                        files_win=files_i_win_final,
                        files_lose=files_i_lose,
                    )

            if call_threadsafe:
                call_threadsafe(_show)
            else:
                self.after(0, _show)

        threading.Thread(target=_worker, daemon=True).start()

    def _add_separator(self, ref_idx: int, above: bool):
        """Prompt for a separator name and insert it above or below ref_idx."""
        dialog = _SeparatorNameDialog(self.winfo_toplevel())
        self.winfo_toplevel().wait_window(dialog)
        if dialog.result is None:
            return
        sep_name = self._unique_separator_name(dialog.result.strip() + "_separator")
        inverted = (self._sort_column == "priority" and self._sort_ascending)
        ref_is_sep = self._entries[ref_idx].is_separator

        if ref_is_sep:
            # A separator is a group header that owns the mod block following it
            # in natural order. Inserting between the header and its mods would
            # transfer those mods to the new separator, so we must anchor to the
            # whole group block — and account for inverted display where group
            # order is reversed (visual-above == natural-after-block, and
            # visual-below == natural-before-header).
            block_end = ref_idx + 1
            while (block_end < len(self._entries)
                   and not self._entries[block_end].is_separator):
                block_end += 1
            if inverted:
                insert_at = block_end if above else ref_idx
            else:
                insert_at = ref_idx if above else block_end
        else:
            # Under inverted priority sort, visual above/below is flipped in
            # natural order for mod rows (mods within a group are reversed).
            visually_above = (not above) if inverted else above
            insert_at = ref_idx if visually_above else ref_idx + 1
        entry = ModEntry(name=sep_name, enabled=True, locked=True, is_separator=True)
        self._entries.insert(insert_at, entry)
        # Keep check_vars aligned (None for separators)
        self._check_vars.insert(insert_at, None)
        if self._sel_idx >= insert_at:
            self._sel_idx += 1
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _generate_separators(self) -> None:
        """Create a separator for each category and move loose mods (not already inside a separator) into it.

        Rules:
        - Mods already inside a separator block are untouched.
        - Loose mods with conflicts stay at the bottom in their original relative order.
        - Loose mods without conflicts are grouped by category, each group placed under
          a new (or existing) separator named after the category.
        - Mods with no category get a separator named "Uncategorized".
        - New separators are inserted just above the conflict/bottom section.
        """
        OVERWRITE = OVERWRITE_NAME  # synthetic first entry
        ROOT = ROOT_FOLDER_NAME     # synthetic last entry

        # --- Step 1: Identify which mods are already inside a separator block ---
        # A mod is "in a separator" if a real (non-synthetic) separator appears above it.
        # Synthetic separators (Overwrite, Root_Folder) don't count as real separators.
        SYNTHETIC = {OVERWRITE, ROOT}
        in_separator: set[str] = set()
        under_real_sep = False
        for entry in self._entries:
            if entry.is_separator:
                if entry.name not in SYNTHETIC:
                    under_real_sep = True
            elif under_real_sep:
                in_separator.add(entry.name)

        # --- Step 2: Collect loose mods (not in a separator, not synthetic, not disabled) ---
        # Preserve original relative order.
        loose: list[ModEntry] = [
            e for e in self._entries
            if not e.is_separator
            and e.name not in in_separator
            and e.name not in (OVERWRITE, ROOT)
            and e.enabled
        ]

        if not loose:
            return  # nothing to do

        # --- Step 3: Split loose mods into conflict vs non-conflict groups ---
        conflict_mods: list[ModEntry] = []
        no_conflict_mods: list[ModEntry] = []
        for entry in loose:
            c = self._conflict_map.get(entry.name, CONFLICT_NONE)
            if c != CONFLICT_NONE:
                conflict_mods.append(entry)
            else:
                no_conflict_mods.append(entry)

        # --- Step 4: Group non-conflict mods by category (sorted alphabetically within group) ---
        cat_groups: dict[str, list[ModEntry]] = {}
        for entry in no_conflict_mods:
            cat = self._category_names.get(entry.name, "") or "Uncategorized"
            if cat not in cat_groups:
                cat_groups[cat] = []
            cat_groups[cat].append(entry)
        for mods in cat_groups.values():
            mods.sort(key=lambda e: e.name.casefold())

        # --- Step 5: Build the new entries list ---
        # Keep: synthetic Overwrite + all existing separator blocks (untouched)
        # Then append: new category separators + their mods
        # Then append: "Conflicts" separator + conflict mods at bottom

        # Remove all loose mods from _entries (they will be re-inserted).
        loose_names = {e.name for e in loose}
        new_entries: list[ModEntry] = [e for e in self._entries if e.name not in loose_names]

        # Find insertion point: just before ROOT_FOLDER (if present), otherwise end.
        insert_base = len(new_entries)
        for i, e in enumerate(new_entries):
            if e.name == ROOT:
                insert_base = i
                break

        # Build the block to insert: category separators + mods, then conflict mods.
        to_insert: list[ModEntry] = []
        existing_sep_names = {e.name for e in new_entries if e.is_separator}
        for cat, mods in sorted(cat_groups.items()):
            sep_name = cat + "_separator"
            if sep_name not in existing_sep_names:
                sep_entry = ModEntry(name=sep_name, enabled=True, locked=True, is_separator=True)
                to_insert.append(sep_entry)
                existing_sep_names.add(sep_name)
                to_insert.extend(mods)
            else:
                # Separator already exists — find the end of its block and insert mods there.
                sep_idx = next(i for i, e in enumerate(new_entries) if e.name == sep_name)
                # Advance past all non-separator entries already in the block.
                insert_after = sep_idx + 1
                while insert_after < len(new_entries) and not new_entries[insert_after].is_separator:
                    insert_after += 1
                for offset, mod in enumerate(mods):
                    new_entries.insert(insert_after + offset, mod)
                # Update insert_base since new_entries grew.
                insert_base += len(mods)

        if conflict_mods:
            conflicts_sep_name = "Conflicts_separator"
            if conflicts_sep_name not in existing_sep_names:
                to_insert.append(ModEntry(name=conflicts_sep_name, enabled=True, locked=True, is_separator=True))
            to_insert.extend(conflict_mods)

        # Insert the block at the correct position.
        for offset, entry in enumerate(to_insert):
            new_entries.insert(insert_base + offset, entry)

        self._entries = new_entries

        # --- Step 6: Rebuild check vars to stay aligned ---
        self._rebuild_check_widgets()
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()

    def _create_empty_mod(self, ref_idx: int):
        """Prompt for a mod name, create an empty staging folder, and insert a new mod entry below ref_idx."""
        if self._modlist_path is None:
            return
        dialog = _ModNameDialog(self.winfo_toplevel())
        self.winfo_toplevel().wait_window(dialog)
        if dialog.result is None:
            return
        mod_name = dialog.result.strip()
        if not mod_name:
            return
        # Check for name collision
        existing = {e.name for e in self._entries}
        if mod_name in existing:
            show_error(
                "Name Conflict",
                f"A mod or separator named '{mod_name}' already exists.",
                parent=self.winfo_toplevel(),
            )
            return
        # Create the staging folder
        staging = self._staging_root / mod_name
        staging.mkdir(parents=True, exist_ok=True)
        # Write a minimal meta.ini so MO2 recognizes the folder (incl. installed in MO2 format)
        installed = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        (staging / "meta.ini").write_text(
            f"[General]\ninstalled={installed}\n", encoding="utf-8"
        )
        # Insert the new mod in the visual row directly below ref_idx.  Under
        # reverse-priority sort the display is inverted, so "below" in natural
        # _entries order is flipped.
        inverted = (self._sort_column == "priority" and self._sort_ascending)
        if self._entries[ref_idx].is_separator:
            # Separator ref: the new mod should join that separator's group as
            # the row visually beneath the header.  In natural order that's the
            # first group member (ref_idx + 1); under inversion the visually
            # lower-priority end is the last member, just before the next
            # separator (block_end).
            block_end = ref_idx + 1
            while (block_end < len(self._entries)
                   and not self._entries[block_end].is_separator):
                block_end += 1
            insert_at = block_end if inverted else ref_idx + 1
        else:
            # Mod ref: "below" is ref_idx + 1 in natural order, ref_idx when the
            # display is inverted (mods within a group are reversed).
            insert_at = ref_idx if inverted else ref_idx + 1
        entry = ModEntry(name=mod_name, enabled=True, locked=False, is_separator=False)
        self._entries.insert(insert_at, entry)
        # Create logical var for the new mod (visual rendering uses pool)
        var = tk.BooleanVar(value=True)
        self._check_vars.insert(insert_at, var)
        if self._sel_idx >= insert_at:
            self._sel_idx += 1
        self._invalidate_derived_caches()
        self._save_modlist()
        self._rebuild_filemap()
        self._redraw()
        self._update_info()
        self._log(f"Created empty mod: {mod_name}")

    # ------------------------------------------------------------------
    # Toggle
    # ------------------------------------------------------------------

    def _on_toggle(self, idx: int):
        if not self._check_vars or not self._entries:
            return
        if 0 <= idx < len(self._entries) and idx < len(self._check_vars):
            var = self._check_vars[idx]
            if var is None:
                return
            entry = self._entries[idx]
            now_enabled = var.get()
            entry.enabled = now_enabled
            self._sync_plugins_for_toggle(entry.name, now_enabled)

            self._vis_dirty = True  # enabled state affects show-disabled/enabled filters
            self._mod_to_sep_idx = None
            self._save_modlist()
            self._rebuild_filemap()
            self._scan_missing_reqs_flags()
            self._update_enable_disable_all_btn()
            # Skip the synchronous _redraw here. The Tk BooleanVar already
            # updated the checkbox visually for instant click feedback;
            # conflict-highlight repaints happen in _done after the filemap
            # rebuild finishes (which is when they become accurate).
            self._update_info()

    def _sync_plugins_for_toggle(self, mod_name: str, now_enabled: bool) -> None:
        """Add or remove a mod's plugins from plugins.txt and loadorder.txt on toggle."""
        app = self.winfo_toplevel()
        pp = getattr(app, "_plugin_panel", None)
        if pp is None or pp._plugins_path is None:
            return
        plugin_exts = {e.lower() for e in getattr(pp, "_plugin_extensions", [])}
        if not plugin_exts:
            return
        staging = self._staging_root / mod_name
        if not staging.is_dir():
            return
        mod_plugins = [
            f.name for f in staging.iterdir()
            if f.is_file() and f.suffix.lower() in plugin_exts
        ]
        if not mod_plugins:
            return

        loadorder_path = pp._plugins_path.parent / "loadorder.txt"

        if now_enabled:
            # Append to plugins.txt and loadorder.txt if not already present
            existing = read_plugins(pp._plugins_path, star_prefix=pp._plugins_star_prefix)
            existing_lower = {e.name.lower() for e in existing}
            added = [n for n in mod_plugins if n.lower() not in existing_lower]
            if added:
                for name in added:
                    existing.append(PluginEntry(name=name, enabled=True))
                write_plugins(pp._plugins_path, existing, star_prefix=pp._plugins_star_prefix)
            loadorder = read_loadorder(loadorder_path)
            lo_lower = {n.lower() for n in loadorder}
            lo_added = [n for n in mod_plugins if n.lower() not in lo_lower]
            if lo_added:
                loadorder.extend(lo_added)
                write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in loadorder])
        else:
            # Remove from plugins.txt and loadorder.txt
            remove_lower = {n.lower() for n in mod_plugins}
            existing = read_plugins(pp._plugins_path, star_prefix=pp._plugins_star_prefix)
            new_entries = [e for e in existing if e.name.lower() not in remove_lower]
            if len(new_entries) < len(existing):
                write_plugins(pp._plugins_path, new_entries, star_prefix=pp._plugins_star_prefix)
            loadorder = read_loadorder(loadorder_path)
            new_lo = [n for n in loadorder if n.lower() not in remove_lower]
            if len(new_lo) < len(loadorder):
                write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in new_lo])

    # ------------------------------------------------------------------
    # Toolbar button handlers
    # ------------------------------------------------------------------

    def _on_collections(self, initial_slug: str | None = None, initial_game_domain: str | None = None, initial_revision: int | None = None):
        """Slide the Collections browser over the modlist panel.

        If initial_slug is provided (e.g. from an nxm:// collection link), the
        dialog will open that collection directly.
        """
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        game = self._game
        domain = (game.nexus_game_domain if game and game.nexus_game_domain else "") or ""
        if not domain:
            self._log("Collections: No game selected or game has no Nexus domain.")
            return
        # Destroy any existing panel first (e.g. double-click)
        self._close_collections()
        panel = CollectionsDialog(
            self, game_domain=domain, api=api, game=game,
            log_fn=self._log,
            app_root=app,
            on_close=self._close_collections,
            on_open_workshop=self._on_workshop,
            initial_slug=initial_slug,
            initial_game_domain=initial_game_domain,
            initial_revision=initial_revision,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._collections_panel = panel

    def _close_collections(self):
        """Destroy the inline collections panel and restore the modlist."""
        panel = getattr(self, "_collections_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._collections_panel = None

    def _on_workshop(self, entries: list):
        """Open the Workshop overlay over the modlist panel."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        game = self._game
        domain = (game.nexus_game_domain if game and game.nexus_game_domain else "") or ""
        overlay_parent = getattr(app, "_plugin_panel_container", None)
        self._close_workshop()
        panel = WorkshopDialog(
            self, entries=entries, game=game, api=api,
            game_domain=domain, on_close=self._close_workshop,
            overlay_parent=overlay_parent,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._workshop_panel = panel

    def _close_workshop(self):
        """Destroy the Workshop overlay and restore the modlist."""
        panel = getattr(self, "_workshop_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._workshop_panel = None

    def _on_nexus_browser(self):
        """Show the Nexus Browse/Tracked/Endorsed overlay over the modlist panel.

        A popout toggle re-hosts the browser in a separate window so the user
        can keep browsing/downloading while sorting mods in the main window.
        Tk can't reparent a live widget across toplevels, so the toggle tears
        down the current overlay and builds a fresh one in the other host
        (the panels are stateless and reload on show)."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        game = self._game
        domain = (game.nexus_game_domain if game and game.nexus_game_domain else "") or ""
        if not domain:
            self._log("Nexus: No game selected or game has no Nexus domain.")
            return
        self._close_nexus_browser()
        open_settings = app.get_nexus_settings_opener() if hasattr(app, "get_nexus_settings_opener") else None

        def _build(popped_out: bool, initial_tab: str = "Browse"):
            # Always clear any existing host first.
            self._close_nexus_browser()

            def _on_rehost(going_to_popout, current_tab):
                _build(going_to_popout, current_tab)

            common = dict(
                game_domain=domain, api=api, game=game,
                log_fn=self._log,
                app_root=app,
                on_close=self._close_nexus_browser,
                on_open_settings=open_settings,
                on_rehost=_on_rehost,
                is_popped_out=popped_out,
                initial_tab=initial_tab,
            )

            if popped_out:
                host = ctk.CTkToplevel(app, fg_color=BG_DEEP)
                host.title("Nexus Mods")
                try:
                    root = app.winfo_toplevel()
                    root.update_idletasks()
                    w = max(int(root.winfo_width() * 0.85), 900)
                    h = max(int(root.winfo_height() * 0.85), 600)
                    x = root.winfo_rootx() + (root.winfo_width() - w) // 2
                    y = root.winfo_rooty() + (root.winfo_height() - h) // 2
                    host.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
                except Exception:
                    host.geometry("1000x680")
                host.minsize(700, 480)
                host.grid_rowconfigure(0, weight=1)
                host.grid_columnconfigure(0, weight=1)
                # Closing the popout window closes the browser entirely.
                host.protocol("WM_DELETE_WINDOW", self._close_nexus_browser)
                self._nexus_browser_window = host

                panel = NexusBrowserOverlay(host, **common)
                panel.grid(row=0, column=0, sticky="nsew")
                self._nexus_browser_panel = panel
                try:
                    # Independent window (not transient/topmost) so the manager
                    # underneath stays usable for sorting mods.
                    host.attributes("-topmost", False)
                    host.lift()
                    host.focus_force()
                except Exception:
                    pass
            else:
                panel = NexusBrowserOverlay(self, **common)
                panel.place(relx=0, rely=0, relwidth=1, relheight=1)
                self._nexus_browser_panel = panel

        _build(popped_out=False)

    def _close_nexus_browser(self):
        """Destroy the Nexus browser overlay (and its popout window) and restore
        the modlist."""
        panel = getattr(self, "_nexus_browser_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._nexus_browser_panel = None
        win = getattr(self, "_nexus_browser_window", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
            self._nexus_browser_window = None

    def show_profile_settings(self, game_name: str, current_profile: str,
                               on_profile_renamed=None, on_profile_removed=None,
                               on_profiles_changed=None):
        """Show the Profile Settings overlay over the modlist panel."""
        from gui.profile_settings_overlay import ProfileSettingsOverlay
        self._close_profile_settings()
        panel = ProfileSettingsOverlay(
            self,
            game_name=game_name,
            current_profile=current_profile,
            on_close=self._close_profile_settings,
            on_profile_renamed=on_profile_renamed,
            on_profile_removed=on_profile_removed,
            on_profiles_changed=on_profiles_changed,
            log_fn=self._log,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._profile_settings_panel = panel

    def _close_profile_settings(self):
        """Destroy the profile settings overlay."""
        panel = getattr(self, "_profile_settings_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._profile_settings_panel = None

    def _on_changelog(self):
        """Show the Changelog overlay over the modlist panel."""
        self._close_changelog()
        panel = ChangelogOverlay(self, on_close=self._close_changelog)
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._changelog_panel = panel

    def _close_changelog(self):
        """Destroy the changelog overlay and restore the modlist."""
        panel = getattr(self, "_changelog_panel", None)
        if panel is not None:
            try:
                panel.destroy()
            except Exception:
                pass
            self._changelog_panel = None

    def _on_restore_backup(self):
        """Open the backup restore panel/dialog for the current profile."""
        if not self._modlist_path or not self._modlist_path.parent.is_dir():
            return
        app = self.winfo_toplevel()
        profile_dir = self._modlist_path.parent
        profile_name = getattr(
            getattr(app, "_topbar", None),
            "_profile_var",
            None,
        )
        profile_name = profile_name.get() if profile_name is not None else "default"
        show_fn = getattr(app, "show_backup_restore_panel", None)
        if show_fn:
            show_fn(
                profile_dir,
                profile_name,
                on_restored=lambda: app._topbar._reload_mod_panel(),
            )
        else:
            dlg = BackupRestoreDialog(
                app,
                profile_dir,
                profile_name=profile_name,
                on_restored=lambda: app._topbar._reload_mod_panel(),
            )
            app.wait_window(dlg)

    def _move_up(self):
        # Under inverted priority sort, visual up = natural down.
        inverted = (self._sort_column == "priority" and self._sort_ascending)
        if inverted:
            self._shift_selection(+1, "up")
        else:
            self._shift_selection(-1, "up")

    def _move_down(self):
        inverted = (self._sort_column == "priority" and self._sort_ascending)
        if inverted:
            self._shift_selection(-1, "down")
        else:
            self._shift_selection(+1, "down")

    def _shift_selection(self, delta: int, label_dir: str):
        """Shift the current selection by `delta` positions in natural order.
        delta=-1 moves toward index 0, delta=+1 toward the end."""
        if delta == 0:
            return
        # Process highest-index-first when moving toward end, lowest-first when moving toward 0
        indices = sorted(self._sel_set, reverse=(delta > 0)) if self._sel_set else (
            [self._sel_idx] if self._sel_idx >= 0 else []
        )
        if not indices:
            return
        # Boundary check: extremum in direction of travel
        if delta < 0 and min(indices) <= 0:
            return
        if delta > 0 and max(indices) >= len(self._entries) - 1:
            return
        if any(self._entries[i].locked for i in indices):
            return
        # Don't swap past a pinned separator (Overwrite/Root_Folder)
        for i in indices:
            neighbor = self._entries[i + delta]
            if neighbor.name in (OVERWRITE_NAME, ROOT_FOLDER_NAME):
                return
        saved_col, saved_asc = self._clear_sort()
        for i in indices:
            j = i + delta
            self._entries[i], self._entries[j] = self._entries[j], self._entries[i]
            self._check_vars[i], self._check_vars[j] = self._check_vars[j], self._check_vars[i]
        self._sel_set = {i + delta for i in indices}
        self._sel_idx = self._sel_idx + delta if self._sel_idx >= 0 else -1
        self._restore_sort(saved_col, saved_asc)
        self._invalidate_derived_caches()
        self._redraw()
        self._update_info()
        self._save_modlist()
        self._rebuild_filemap()
        first = min(indices) + delta if delta < 0 else max(indices) + delta
        label = self._entries[first].name if len(indices) == 1 else f"{len(indices)} items"
        self._log(f"Moved '{label}' {label_dir}")

    def _set_priority(self, idx: int):
        """Prompt for a target position and move the mod there.

        Priority: 0 = bottom (lowest), highest number = top. So e.g. with 200 mods,
        entering 0 puts the mod at the bottom; entering 199 or 470 puts it at the top.
        """
        if not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        if entry.is_separator or entry.name in (OVERWRITE_NAME, ROOT_FOLDER_NAME):
            return
        if entry.locked:
            return

        mod_indices = [
            i for i, e in enumerate(self._entries)
            if not e.is_separator and e.name not in (OVERWRITE_NAME, ROOT_FOLDER_NAME)
        ]
        total_mods = len(mod_indices)
        if total_mods <= 1:
            return

        top = self.winfo_toplevel()
        dlg = _PriorityDialog(top, entry.name, total_mods)
        top.wait_window(dlg)
        value = dlg.result
        if value is None:
            return

        # 0 = bottom (rank total_mods-1), highest = top (rank 0)
        target_rank = total_mods - 1 - min(value, total_mods - 1)

        try:
            current_rank = mod_indices.index(idx)
        except ValueError:
            return

        if target_rank == current_rank:
            return

        target_idx = mod_indices[target_rank]
        from_idx = idx
        to_idx = target_idx

        moved_entry = self._entries.pop(from_idx)
        moved_var = self._check_vars.pop(from_idx)

        self._entries.insert(to_idx, moved_entry)
        self._check_vars.insert(to_idx, moved_var)

        self._sel_idx = to_idx
        self._sel_set = {to_idx}
        self._invalidate_derived_caches()
        self._redraw()
        self._update_info()
        self._save_modlist()
        self._rebuild_filemap()
        self._log(f"Set priority for '{moved_entry.name}' to position {value}")

    # ------------------------------------------------------------------
    # Persist + info
    # ------------------------------------------------------------------

    def _rebuild_filemap(self):
        """Kick off a background filemap rebuild. Safe to call from the main thread.

        Calls are debounced: rapid successive calls within 150 ms are coalesced
        into a single rebuild to avoid hammering the disk when the user quickly
        enables/disables several mods in a row.
        """
        if self._modlist_path is None:
            return
        # Cancel any pending debounce timer and reset it.
        if self._filemap_after_id is not None:
            self.after_cancel(self._filemap_after_id)
            self._filemap_after_id = None
        if self._filemap_pending:
            # A rebuild is already running; mark dirty so we re-run when it finishes.
            self._filemap_dirty = True
            return
        # Debounce: wait 150 ms before actually starting the rebuild so that a
        # burst of rapid changes (e.g. toggling several mods) becomes one rebuild.
        self._filemap_after_id = self.after(150, self._rebuild_filemap_now)

    def _rebuild_filemap_now(self):
        """Internal: actually start the rebuild after the debounce delay."""
        self._filemap_after_id = None
        if self._modlist_path is None:
            return
        if self._filemap_pending:
            self._filemap_dirty = True
            return
        self._filemap_pending = True
        self._filemap_dirty = False

        modlist_path        = self._modlist_path
        staging             = self._staging_root
        output              = self._filemap_path
        strip_prefixes      = self._strip_prefixes
        install_extensions  = self._install_extensions
        root_deploy_folders = self._root_deploy_folders
        rescan_index        = self._filemap_rescan_index
        per_mod_strip       = dict(self._mod_strip_prefixes) if self._mod_strip_prefixes else {}
        conflict_ignore_fn  = set(self._conflict_ignore_filenames) if self._conflict_ignore_filenames else None
        excluded_loose_fn   = set(self._excluded_loose_filenames) if self._excluded_loose_filenames else None
        allowed_top_level   = set(self._allowed_top_level_folders) if self._allowed_top_level_folders else None
        exclude_dirs        = set(self._filemap_exclude_dirs) if self._filemap_exclude_dirs else None
        from Games.ue5_game import UE5Game as _UE5Game
        from Utils.deploy_pipeline import _make_ue5_conflict_key_fn
        _captured_game = getattr(self, "_game", None)
        _conflict_key_fn = None
        if isinstance(_captured_game, _UE5Game):
            _conflict_key_fn = _make_ue5_conflict_key_fn(
                _captured_game, output.parent / "modindex.bin",
            )
        elif _captured_game is not None:
            _legacy = getattr(_captured_game, "filemap_conflict_key_fn", None)
            if _legacy is not None:
                def _conflict_key_fn(_mod: str, rk: str, _f=_legacy) -> str:
                    return _f(rk)
        # Pre-RTX detection: gather the source prefixes that indicate old-format mods
        # (e.g. "natives/x64/" for RE2/RE3/RE7).
        _prertx_prefixes: list[str] = []
        if _captured_game is not None:
            _path_remap = getattr(_captured_game, "mod_deploy_path_remap", {}) or {}
            _prertx_prefixes = [k.lower() for k in _path_remap]
        # Archive extensions for BSA conflict detection (Bethesda games only).
        # Empty frozenset disables the BSA pipeline entirely. The "Hide BSA
        # conflicts" setting also empties it so the (expensive) parsing is
        # skipped and no flags are computed.
        _archive_exts: frozenset[str] = frozenset()
        if _captured_game is not None and not self._hide_bsa_conflicts:
            _archive_exts = frozenset(getattr(_captured_game, "archive_extensions", frozenset()) or frozenset())
        # Plugin load order + extensions — used so BSA conflicts resolve by
        # plugin load order (the engine loads BSAs via their owning plugin),
        # not by mod priority. Snapshot on the main thread.
        _plugin_order_snap: list[str] = []
        _plugin_exts_snap: frozenset[str] = frozenset()
        _pp = getattr(self.winfo_toplevel(), "_plugin_panel", None)
        if _pp is not None:
            _plugin_order_snap = [e.name for e in getattr(_pp, "_plugin_entries", []) if e.enabled]
            _plugin_exts_snap = frozenset(e.lower() for e in getattr(_pp, "_plugin_extensions", []) or [])
        staging_requires_subdir = self._staging_requires_subdir
        staging_wrap_signals    = self._staging_wrap_signals
        staging_already_struct  = self._staging_already_structured
        normalize_folder_case   = self._normalize_folder_case
        filemap_casing          = self._filemap_casing
        self._filemap_rescan_index = False
        disabled_plugins    = read_disabled_plugins(modlist_path.parent, None)  # fresh read for rebuild
        self._disabled_plugins_map = disabled_plugins
        _exc_raw            = read_excluded_mod_files(modlist_path.parent, None)
        excluded_mod_files  = {k: set(v) for k, v in _exc_raw.items()} if _exc_raw else None
        self._excluded_mod_files_map = _exc_raw or {}
        if excluded_mod_files:
            total_exc = sum(len(v) for v in excluded_mod_files.values())
            self.after(0, lambda n=total_exc: self._log(
                f"Filemap: excluding {n} file(s) (profile_state excluded_mod_files)"))
        # Snapshot of root-flagged mods at the time of this rebuild (thread-safe copy)
        root_folder_mods_snap = set(self._root_folder_mods) if self._root_folder_mods else None

        def _log_thread_safe(msg: str) -> None:
            self.after(0, lambda m=msg: self._log(m))

        def _worker():
            nonlocal rescan_index
            try:
                if staging_requires_subdir:
                    _wrap_names, _wrap_exts = staging_wrap_signals
                    fixed = fix_flat_staging_folders(
                        staging, _wrap_names, _wrap_exts, staging_already_struct)
                    if fixed:
                        rescan_index = True
                        self.after(0, lambda names=fixed: self._log(
                            f"Auto-fixed {len(names)} mod(s) with flat staging structure: "
                            + ", ".join(names)
                        ))
                if rescan_index:
                    rebuild_mod_index(
                        output.parent / "modindex.bin",
                        staging,
                        strip_prefixes=strip_prefixes,
                        per_mod_strip_prefixes=per_mod_strip,
                        allowed_extensions=install_extensions or None,
                        root_deploy_folders=root_deploy_folders or None,
                        normalize_folder_case=normalize_folder_case,
                        exclude_dirs=exclude_dirs,
                        log_fn=_log_thread_safe,
                        root_folder_mods=root_folder_mods_snap,
                    )
                count, conflict_map, overrides, overridden_by = build_filemap(
                    modlist_path, staging, output,
                    strip_prefixes=strip_prefixes,
                    per_mod_strip_prefixes=per_mod_strip,
                    allowed_extensions=install_extensions or None,
                    root_deploy_folders=root_deploy_folders or None,
                    disabled_plugins=disabled_plugins or None,
                    conflict_ignore_filenames=conflict_ignore_fn,
                    excluded_loose_filenames=excluded_loose_fn,
                    allowed_top_level_folders=allowed_top_level,
                    excluded_mod_files=excluded_mod_files or None,
                    normalize_folder_case=normalize_folder_case,
                    filemap_casing=filemap_casing,
                    conflict_key_fn=_conflict_key_fn,
                    exclude_dirs=exclude_dirs,
                    log_fn=_log_thread_safe,
                    root_folder_mods=root_folder_mods_snap,
                )
                _game = getattr(self, "_game", None)
                if _game is not None:
                    _game.post_build_filemap(output, staging)
                # Detect pre-RTX mods: any mod with at least one file under a
                # remapped source prefix (e.g. natives/x64/).
                prertx_mods: set[str] = set()
                if _prertx_prefixes:
                    _index = read_mod_index(output.parent / "modindex.bin")
                    if _index:
                        for mod_name, (normal, _) in _index.items():
                            for rel_key in normal:
                                if any(rel_key.startswith(p) for p in _prertx_prefixes):
                                    prertx_mods.add(mod_name)
                                    break
                # BSA/BA2 archive conflict detection (Bethesda games only).
                bsa_conflict_map: dict[str, int] = {}
                bsa_overrides: dict[str, set[str]] = {}
                bsa_overridden_by: dict[str, set[str]] = {}
                loose_over_bsa: dict[str, set[str]] = {}
                bsa_over_loose: dict[str, set[str]] = {}
                if _archive_exts:
                    bsa_index_path = output.parent / "bsa_index.bin"
                    # Rebuild BSA index if the loose-file index is also being rescanned,
                    # or if the BSA index does not exist yet.
                    if rescan_index or not bsa_index_path.is_file():
                        rebuild_bsa_index(
                            bsa_index_path, staging, _archive_exts,
                            log_fn=_log_thread_safe,
                        )
                    (bsa_conflict_map, bsa_overrides, bsa_overridden_by,
                     loose_over_bsa, bsa_over_loose) = build_bsa_conflicts(
                        modlist_path, bsa_index_path, _archive_exts,
                        loose_index_path=output.parent / "modindex.bin",
                        plugin_order=_plugin_order_snap or None,
                        plugin_extensions=_plugin_exts_snap or None,
                        log_fn=_log_thread_safe,
                    )
                # Preserve the untransformed loose dicts; _done will fold
                # loose↔BSA relationships (idempotently) on top of them.
                base_conflict_map  = dict(conflict_map)
                base_overrides     = {k: set(v) for k, v in overrides.items()}
                base_overridden_by = {k: set(v) for k, v in overridden_by.items()}
                # Compute root-rule mods here (off main thread) — was ~930 ms
                # of main-thread time on large modlists (read_mod_index + scan).
                try:
                    worker_root_rule_mods = self._compute_root_rule_mods()
                except Exception:
                    worker_root_rule_mods = set()
                def _done_wrapped():
                    self._root_rule_mods = worker_root_rule_mods
                    _done(count,
                          base_conflict_map, base_overrides, base_overridden_by,
                          bsa_conflict_map, bsa_overrides, bsa_overridden_by,
                          loose_over_bsa, bsa_over_loose,
                          None, prertx_mods)
                self.after(0, _done_wrapped)
            except Exception as exc:
                self.after(0, lambda e=exc: _done(0, {}, {}, {}, {}, {}, {}, {}, {}, e, set()))

        def _done(count,
                  base_conflict_map, base_overrides, base_overridden_by,
                  bsa_conflict_map, bsa_overrides, bsa_overridden_by,
                  loose_over_bsa, bsa_over_loose,
                  exc, prertx_mods=set()):
            self._filemap_pending = False
            if exc is not None:
                self._conflict_map = {}
                self._overrides = {}
                self._overridden_by = {}
                self._conflict_map_base = {}
                self._overrides_base = {}
                self._overridden_by_base = {}
                self._bsa_conflict_map = {}
                self._bsa_overrides = {}
                self._bsa_overridden_by = {}
                self._log(f"Filemap error: {exc}")
            else:
                self._conflict_map_base  = base_conflict_map
                self._overrides_base     = base_overrides
                self._overridden_by_base = base_overridden_by
                self._bsa_conflict_map  = bsa_conflict_map
                self._bsa_overrides     = bsa_overrides
                self._bsa_overridden_by = bsa_overridden_by
                self._prertx_mods   = prertx_mods
                self._apply_loose_bsa_fold(loose_over_bsa, bsa_over_loose)
                self._log(f"Filemap updated: {count} file(s).")
            # Root-rule mod set was already computed in the worker thread
            # (see worker_root_rule_mods) and assigned to self._root_rule_mods
            # in _done_wrapped, before this function ran.
            self._vis_dirty = True  # conflict filters depend on conflict_map
            self._mod_to_sep_idx = None
            self._redraw()
            # Defer _on_filemap_rebuilt to the next event-loop iteration so
            # the current _redraw geometry is fully settled before the plugin
            # panel destroys/creates widgets (framework banners, data tree),
            # which can trigger cascading resize events that cause column
            # items to momentarily disappear.
            if self._on_filemap_rebuilt:
                self.after_idle(self._on_filemap_rebuilt)
            # If something changed while we were running, rebuild again.
            if self._filemap_dirty:
                self._rebuild_filemap()

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_loose_bsa_fold(
        self,
        loose_over_bsa: dict[str, set[str]],
        bsa_over_loose: dict[str, set[str]],
    ) -> None:
        """Re-derive self._conflict_map / _overrides / _overridden_by from the
        base (loose-only) state plus the loose↔BSA cross-relationships.
        """
        conflict_map  = dict(self._conflict_map_base)
        overrides     = {k: set(v) for k, v in self._overrides_base.items()}
        overridden_by = {k: set(v) for k, v in self._overridden_by_base.items()}
        for loose_mod, bsa_mods in loose_over_bsa.items():
            overrides.setdefault(loose_mod, set()).update(bsa_mods)
            cur = conflict_map.get(loose_mod, CONFLICT_NONE)
            if cur == CONFLICT_NONE:
                conflict_map[loose_mod] = CONFLICT_WINS
            elif cur == CONFLICT_LOSES:
                conflict_map[loose_mod] = CONFLICT_PARTIAL
        for bsa_mod, loose_mods in bsa_over_loose.items():
            overridden_by.setdefault(bsa_mod, set()).update(loose_mods)
        self._conflict_map  = conflict_map
        self._overrides     = overrides
        self._overridden_by = overridden_by

    def recompute_bsa_conflicts(self) -> None:
        """Recompute BSA conflicts only — no loose-file disk scan.

        Call this when plugin load order changes so BSA winners (which
        depend on their owning plugin's load position) stay in sync.
        Runs the merge on a background thread since very large BSA
        indices can be sluggish.
        """
        if self._modlist_path is None or self._filemap_path is None:
            return
        if self._hide_bsa_conflicts:
            return
        _captured_game = getattr(self, "_game", None)
        _archive_exts: frozenset[str] = frozenset()
        if _captured_game is not None:
            _archive_exts = frozenset(getattr(_captured_game, "archive_extensions", frozenset()) or frozenset())
        if not _archive_exts:
            return
        bsa_index_path = self._filemap_path.parent / "bsa_index.bin"
        if not bsa_index_path.is_file():
            return
        modlist_path = self._modlist_path
        loose_index_path = self._filemap_path.parent / "modindex.bin"

        _plugin_order_snap: list[str] = []
        _plugin_exts_snap: frozenset[str] = frozenset()
        _pp = getattr(self.winfo_toplevel(), "_plugin_panel", None)
        if _pp is not None:
            _plugin_order_snap = [e.name for e in getattr(_pp, "_plugin_entries", []) if e.enabled]
            _plugin_exts_snap = frozenset(e.lower() for e in getattr(_pp, "_plugin_extensions", []) or [])

        def _worker():
            try:
                result = build_bsa_conflicts(
                    modlist_path, bsa_index_path, _archive_exts,
                    loose_index_path=loose_index_path,
                    plugin_order=_plugin_order_snap or None,
                    plugin_extensions=_plugin_exts_snap or None,
                )
            except Exception as exc:
                self.after(0, lambda e=exc: self._log(f"BSA recompute error: {e}"))
                return
            self.after(0, lambda r=result: _apply(r))

        def _apply(result):
            (bsa_conflict_map, bsa_overrides, bsa_overridden_by,
             loose_over_bsa, bsa_over_loose) = result
            self._bsa_conflict_map  = bsa_conflict_map
            self._bsa_overrides     = bsa_overrides
            self._bsa_overridden_by = bsa_overridden_by
            self._apply_loose_bsa_fold(loose_over_bsa, bsa_over_loose)
            self._vis_dirty = True
            self._mod_to_sep_idx = None
            self._redraw()
            # Invalidate plugin panel's BSA cache so its Archive/Data tabs
            # reflect the new winners.
            pp = getattr(self.winfo_toplevel(), "_plugin_panel", None)
            if pp is not None:
                pp._bsa_conflict_cache = None
                # Re-render (or mark dirty) so the Archive tab reflects the
                # freshly built bsa_index — it may have gone from "no index"
                # to "all archives" after this rebuild.
                if hasattr(pp, "show_mod_archives"):
                    sep_name = getattr(pp, "_archive_separator_name", None)
                    sep_mods = getattr(pp, "_archive_separator_mods", None)
                    if sep_name is not None and sep_mods is not None:
                        pp.show_mod_archives_for_separator(sep_name, sep_mods)
                    else:
                        pp.show_mod_archives(pp._archive_mod_name)

        threading.Thread(target=_worker, daemon=True).start()

    def _save_modlist(self):
        if self._modlist_path is None:
            return
        from dataclasses import replace as _dc_replace
        entries = []
        for i, e in enumerate(self._entries):
            if e.name in (OVERWRITE_NAME, ROOT_FOLDER_NAME):
                continue
            # Bundle variants should never be written as locked (*) — locked only
            # prevents dragging in the panel, not toggling.  Write as +/- instead.
            if self._bundle_name_of(i) is not None and e.locked:
                e = _dc_replace(e, locked=False)
            entries.append(e)
        write_modlist(self._modlist_path, entries)

    def _update_info(self):
        mods    = [e for e in self._entries if not e.is_separator]
        enabled = sum(1 for e in mods if e.enabled)
        total   = len(mods)
        sel_entry = self._entries[self._sel_idx] if 0 <= self._sel_idx < len(self._entries) else None
        sel = (f" | Selected: {sel_entry.name}"
               if sel_entry and not sel_entry.is_separator else "")
        if self._status_bar is not None:
            self._status_bar.set_mod_count(f"{enabled}/{total} mods active{sel}")

    def set_status_bar(self, status_bar) -> None:
        """Wire up the StatusBar so _update_info can push the mod count into it."""
        self._status_bar = status_bar

    def set_highlighted_mod(self, mod_name: str | None):
        """Highlight the given mod (by name) in the modlist, e.g. when a plugin is selected."""
        if mod_name != self._highlighted_mod:
            self._highlighted_mod = mod_name
            self._redraw()  # _redraw calls _draw_marker_strip internally

    def refresh_theme(self) -> None:
        """Force a redraw after theme colours change. Rendering reads live via
        `_theme.<name>`, so a redraw is enough to apply new colours."""
        try:
            self._redraw()
        except Exception:
            pass

    def _on_marker_strip_resize(self, _event):
        if self._marker_strip_after_id is not None:
            self.after_cancel(self._marker_strip_after_id)
        self._marker_strip_after_id = self.after(250, self._draw_marker_strip)

    def _draw_marker_strip(self):
        """Paint the combined scrollbar + marker strip.

        Layer order (bottom → top):
          1. Trough background (BG_DEEP)
          2. Tick marks spanning full strip width
             - Orange : the mod whose plugin is selected (_highlighted_mod)
             - Green  : mods that win over the selected mod (conflict_higher)
             - Red    : mods that lose to the selected mod (conflict_lower)
          3. Thumb rectangle with gray50 stipple so underlying ticks bleed through.
        """
        c = self._marker_strip
        c.delete("all")
        strip_h = c.winfo_height()
        strip_w = c.winfo_width()
        if strip_h <= 1 or strip_w <= 1:
            return

        # Trough background
        c.create_rectangle(0, 0, strip_w, strip_h, fill=BG_DEEP, outline="", tags="trough")

        vis = self._visible_indices
        n = len(vis) if vis else 0

        if n:
            # Name/entry-index → row lookups for the visible list. Rebuilt only
            # when _redraw replaces _visible_indices (identity check) — not on
            # every scroll tick.
            if self._marker_maps_vis is not vis:
                # Skip the static-divider sentinel (BOUNDARY_ROW): it is not a
                # valid _entries index and has no mod/separator to map.
                self._marker_ei_to_row = {ei: r for r, ei in enumerate(vis)
                                          if ei != BOUNDARY_ROW}
                self._marker_name_to_row = {self._entries[ei].name: r
                                            for r, ei in enumerate(vis)
                                            if ei != BOUNDARY_ROW}
                self._marker_maps_vis = vis
            ei_to_row = self._marker_ei_to_row
            name_to_row = self._marker_name_to_row

            def _row_for_mod(mod_name: str) -> int | None:
                row = name_to_row.get(mod_name)
                if row is not None:
                    return row
                sep_ei = self._sep_idx_for_mod(mod_name)
                if sep_ei >= 0:
                    return ei_to_row.get(sep_ei)
                return None

            def _tick(row_idx: int, colour: str):
                frac = row_idx / n
                y = max(2, min(int(frac * strip_h), strip_h - 4))
                c.create_rectangle(0, y, strip_w, y + 3, fill=colour, outline="", tags="marker")

            if self._highlighted_mod:
                row = _row_for_mod(self._highlighted_mod)
                if row is not None:
                    _tick(row, _theme.plugin_mod)

            sel_indices = sorted(self._sel_set) if self._sel_set else (
                [self._sel_idx] if 0 <= self._sel_idx < len(self._entries) else []
            )
            all_higher: set[str] = set()
            all_lower: set[str] = set()
            for si in sel_indices:
                if si < 0 or si >= len(self._entries):
                    continue
                e = self._entries[si]
                if e.is_separator and e.name != OVERWRITE_NAME:
                    for ci in self._sep_block_range(si):
                        child = self._entries[ci]
                        if not child.is_separator:
                            all_higher.update(self._overrides.get(child.name, set()))
                            all_lower.update(self._overridden_by.get(child.name, set()))
                            all_higher.update(self._bsa_overrides.get(child.name, set()))
                            all_lower.update(self._bsa_overridden_by.get(child.name, set()))
                    continue
                all_higher.update(self._overrides.get(e.name, set()))
                all_lower.update(self._overridden_by.get(e.name, set()))
                all_higher.update(self._bsa_overrides.get(e.name, set()))
                all_lower.update(self._bsa_overridden_by.get(e.name, set()))
            for mod_name in all_higher:
                row = _row_for_mod(mod_name)
                if row is not None:
                    _tick(row, _theme.conflict_higher)
            for mod_name in all_lower:
                row = _row_for_mod(mod_name)
                if row is not None:
                    _tick(row, _theme.conflict_lower)

        # Thumb (stippled so ticks show through). Hide if content fits fully.
        first = max(0.0, min(1.0, self._scroll_first))
        last = max(first, min(1.0, self._scroll_last))
        if last - first < 0.999:
            y1 = int(first * strip_h)
            y2 = max(y1 + 8, int(last * strip_h))  # min thumb size
            if y2 > strip_h:
                y2 = strip_h
                y1 = max(0, y2 - 8)
            c.create_rectangle(
                0, y1, strip_w, y2,
                fill=_theme.BG_SEP, outline="", tags="thumb",
            )

    def _scroll_set(self, first: str, last: str) -> None:
        """yscrollcommand callback: store thumb range and repaint thumb only."""
        try:
            f = float(first); l = float(last)
        except (TypeError, ValueError):
            return
        if f == self._scroll_first and l == self._scroll_last:
            return
        self._scroll_first = f
        self._scroll_last = l
        # Repaint only the thumb — ticks + trough stay valid until content
        # changes (which triggers _draw_marker_strip via _redraw).
        self._redraw_thumb()

    def _redraw_thumb(self) -> None:
        c = self._marker_strip
        c.delete("thumb")
        strip_h = c.winfo_height()
        strip_w = c.winfo_width()
        if strip_h <= 1 or strip_w <= 1:
            return
        first = max(0.0, min(1.0, self._scroll_first))
        last = max(first, min(1.0, self._scroll_last))
        if last - first >= 0.999:
            return
        y1 = int(first * strip_h)
        y2 = max(y1 + 8, int(last * strip_h))
        if y2 > strip_h:
            y2 = strip_h
            y1 = max(0, y2 - 8)
        c.create_rectangle(
            0, y1, strip_w, y2,
            fill=_theme.BG_SEP, outline="", tags="thumb",
        )

    def _on_scrollbar_press(self, event):
        strip_h = self._marker_strip.winfo_height()
        if strip_h <= 1:
            return
        first = self._scroll_first
        last = self._scroll_last
        thumb_top = first * strip_h
        thumb_bot = last * strip_h
        if thumb_top <= event.y <= thumb_bot:
            # Start drag: remember offset within thumb
            self._thumb_drag_offset = (event.y - thumb_top) / strip_h
        else:
            # Page: jump so thumb is centred on click
            self._thumb_drag_offset = (last - first) / 2.0
            self._scroll_to_pointer(event.y)

    def _on_scrollbar_drag(self, event):
        if self._thumb_drag_offset is None:
            return
        self._scroll_to_pointer(event.y)

    def _on_scrollbar_release(self, _event):
        self._thumb_drag_offset = None

    def _scroll_to_pointer(self, py: int) -> None:
        strip_h = self._marker_strip.winfo_height()
        if strip_h <= 1 or self._thumb_drag_offset is None:
            return
        frac = (py / strip_h) - self._thumb_drag_offset
        frac = max(0.0, min(1.0, frac))
        self._canvas.yview_moveto(frac)
        self._schedule_redraw()

    def clear_selection(self):
        """Clear the mod list selection, e.g. when a plugin is selected."""
        if self._sel_idx >= 0 or self._sel_set:
            self._sel_idx = -1
            self._sel_set = set()
            self._redraw()

    def _sep_idx_for_mod(self, mod_name: str) -> int:
        """Return the index of the separator immediately above mod_name in _entries, or -1.

        Backed by a dict cache rebuilt lazily — invalidated alongside _vis_dirty
        because the same structural changes (insert/remove/reorder) move mods
        between separator blocks.
        """
        cache = self._mod_to_sep_idx
        if cache is None:
            cache = {}
            cur_sep = -1
            for i, e in enumerate(self._entries):
                if e.is_separator:
                    cur_sep = i
                else:
                    cache[e.name] = cur_sep
            self._mod_to_sep_idx = cache
        return cache.get(mod_name, -1)

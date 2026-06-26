"""
Plugin panel: Plugins, Mod Files, Data, Downloads tabs.
Used by App. Imports theme, game_helpers, dialogs, install_mod, subpanels.
Browse/Tracked/Endorsed are shown in the Nexus overlay on the modlist panel.
"""

import json
import os
import re
import subprocess
import threading
import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path

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
    BG_LIST,
    BG_PANEL,
    BG_ROW,
    BG_ROW_ALT,
    BG_SELECT,
    BORDER,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_OK,
    TEXT_SEP,
    SCROLL_BG,
    SCROLL_TROUGH,
    SCROLL_ACTIVE,
    TAG_INI_PROFILE,
    TAG_BSA,
    TAG_FOLDER,
    BTN_SUCCESS,
    BTN_SUCCESS_HOV,
    BTN_SUCCESS_ALT,
    BTN_SUCCESS_ALT_HOV,
    BTN_INFO,
    BTN_INFO_DEEP,
    BTN_INFO_DEEP_HOV,
    BTN_INFO_HOV,
    BTN_CANCEL,
    RED_BTN,
    RED_HOV,
    TONE_CYAN,
    STATUS_BADGE_RED,
    STATUS_BADGE_GREEN,
    BG_GREEN_ROW,
    BG_GREEN_DEEP,
    BG_RED_DEEP,
    BG_ORANGE_DEEP,
    BG_GREEN_TEXT,
    BG_RED_TEXT,
    BG_ORANGE_TEXT,
    BG_BLUE_DEEP,
    BG_BLUE_TEXT,
    BG_OVERLAY_ERR,
    STATUS_ERR_BRIGHT,
    TEXT_WHITE,
    scaled,
    _ICONS_DIR,
    load_icon as _load_icon,
)
import gui.theme as _theme
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.game_helpers import _GAMES, _vanilla_plugins_for_game, foreign_deployed_plugin_basenames
from gui.dialogs import _PriorityDialog, _ExeConfigDialog, _ExeFilterDialog, confirm_deploy_appdata
from gui.install_mod import install_mod_from_archive
from gui.mod_name_utils import _suggest_mod_names as suggest_mod_names
from gui.loot_groups_overlay import LootGroupsOverlay
from gui.loot_plugin_rules_overlay import LootPluginRulesOverlay
from gui.plugin_cycle_overlay import PluginCycleOverlay

from Utils.config_paths import get_exe_args_path, get_game_config_dir, get_game_config_path
from Utils.profile_state import (
    read_plugin_locks,
    write_plugin_locks,
    read_excluded_mod_files,
    write_excluded_mod_files,
    read_mod_strip_prefixes,
    write_mod_strip_prefixes,
)
from Utils.filemap import OVERWRITE_NAME as _OVERWRITE_NAME, build_filemap
from Utils.xdg import xdg_open, open_url
from Utils.plugins import (
    PluginEntry,
    read_plugins,
    write_plugins,
    append_plugin,
    read_loadorder,
    write_loadorder,
    sync_plugins_from_filemap,
    prune_plugins_from_filemap,
)
from Utils.plugin_parser import check_missing_masters, check_late_masters, check_version_mismatched_masters, read_masters, is_esl_flagged, is_master_flagged, set_esl_flag, check_esl_eligible
from LOOT.loot_sorter import (
    sort_plugins as loot_sort,
    is_available as loot_available,
    write_loot_info as loot_write_info,
    read_loot_info as loot_read_info,
)
from Nexus.nexus_meta import write_meta, read_meta

# Bump this whenever check_esl_eligible() changes its verdict criteria so that
# cached results from older algorithm versions are invalidated on next scan.
# v1 = libloot-backed is_valid_as_light_plugin via load_plugin_headers (broken —
#      returned True for every plugin because records aren't loaded).
# v2 = libloot-backed, using load_plugins so record data is actually parsed.
_ESL_ELIG_CACHE_VERSION = 2


def _file_exists_ci(base: Path, rel: Path) -> bool:
    """Case-insensitive file existence check.

    Walks each component of *rel* under *base*, matching directory/file names
    case-insensitively so that framework DLL paths like
    ``red4ext/plugins/ArchiveXL/ArchiveXL.dll`` are found even when the actual
    folders on a case-sensitive filesystem use different casing.
    """
    current = base
    for part in rel.parts:
        try:
            entries = {e.name.lower(): e for e in current.iterdir()}
        except OSError:
            return False
        match = entries.get(part.lower())
        if match is None:
            return False
        current = match
    return current.is_file()


def _resolve_compat_data(prefix_path: Path) -> Path:
    """Return the STEAM_COMPAT_DATA_PATH for a given user-selected pfx/ folder.

    Steam layout: compatdata/<id>/pfx/ → compat_data = prefix_path.parent.
    Heroic layout: <prefix>/pfx is a symlink to "." → compat_data = prefix_path
    itself (config_info lives alongside the pfx symlink, not one level up)."""
    if (prefix_path / "config_info").is_file():
        return prefix_path
    parent = prefix_path.parent
    if (parent / "config_info").is_file():
        return parent
    return parent


def _read_prefix_runner(compat_data: Path) -> str:
    """Read the Proton runner name from <compat_data>/config_info (first line).
    Returns an empty string if the file is absent or unreadable."""
    try:
        return (compat_data / "config_info").read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return ""


from gui.text_utils import truncate_text as _truncate_plugin_name, clear_truncate_cache as _clear_truncate_cache
from gui.tk_tooltip import TkTooltip


# ---------------------------------------------------------------------------
# Launch options parser
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def _parse_launch_options(opts: str, command: list) -> "tuple[dict, list]":
    """Parse Steam-style launch options into (env_vars, final_command).

    Tokens matching KEY=VALUE are extracted as environment variables.
    If ``%command%`` is present it is replaced by the actual *command* list
    (wrappers before it are prepended; tokens after it are appended).
    If ``%command%`` is absent the remaining tokens are appended as a suffix.
    """
    import shlex

    opts = (opts or "").strip()
    if not opts:
        return {}, list(command)

    env_vars: dict = {}

    if "%command%" in opts:
        idx = opts.index("%command%")
        prefix_str = opts[:idx]
        suffix_str = opts[idx + len("%command%"):]

        try:
            prefix_tokens = shlex.split(prefix_str)
        except ValueError:
            prefix_tokens = prefix_str.split()
        try:
            suffix_tokens = shlex.split(suffix_str)
        except ValueError:
            suffix_tokens = suffix_str.split()

        wrappers: list = []
        for token in prefix_tokens:
            if _ENV_VAR_RE.match(token):
                k, v = token.split("=", 1)
                env_vars[k] = v
            else:
                wrappers.append(token)

        suffix: list = []
        for token in suffix_tokens:
            if _ENV_VAR_RE.match(token):
                k, v = token.split("=", 1)
                env_vars[k] = v
            else:
                suffix.append(token)

        return env_vars, wrappers + list(command) + suffix
    else:
        try:
            tokens = shlex.split(opts)
        except ValueError:
            tokens = opts.split()

        suffix = []
        for token in tokens:
            if _ENV_VAR_RE.match(token):
                k, v = token.split("=", 1)
                env_vars[k] = v
            else:
                suffix.append(token)

        return env_vars, list(command) + suffix


# ---------------------------------------------------------------------------
# PluginPanel
# ---------------------------------------------------------------------------
from gui.plugin_panel_exe_launcher import PluginPanelExeLauncherMixin
from gui.plugin_panel_loot import PluginPanelLOOTMixin
from gui.plugin_panel_userlist_cycle import PluginPanelUserlistCycleMixin
from gui.plugin_panel_archive import PluginPanelArchiveMixin
from gui.plugin_panel_mod_files import PluginPanelModFilesMixin
from gui.plugin_panel_data import PluginPanelDataMixin
from gui.plugin_panel_ini import PluginPanelIniMixin


class _PackOptionsDialog(tk.Frame):
    """In-app pre-pack confirmation overlay.

    Renders as a full-coverage dim backdrop over a host panel (typically
    the modlist panel) with a centred card.  The card surfaces the
    overwrite warning when applicable and offers two optional
    behaviours:

      * "Delete loose files after packing" — destructive convenience.
      * "Separate textures archive" — only shown for BSA; BA2 always
        splits Main+Textures so the option is hidden there.

    Use :meth:`ask` (classmethod) to display the overlay and block until
    the user picks; returns ``{"delete_loose": bool, "split_textures":
    bool}`` on confirm or ``None`` on cancel/close.

    Compared to the previous CTkToplevel implementation, the overlay
    can't be dragged off-window or hidden behind another app — it sits
    inside the host panel and is dismissed only by Pack/Cancel/Esc.
    """

    # Visual style — chunky enough to use comfortably on a Steam Deck
    # touchscreen.  Checkbox cells, button height, and label fonts all
    # scale together.
    _CHECKBOX_W = 22
    _CHECKBOX_H = 22

    def __init__(
        self,
        parent: tk.Misc,
        *,
        bsa_filename: str,
        existing: bool,
        kind: str = "bsa",
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._parent_ref = parent
        self._kind = kind
        self.result: dict | None = None
        # Caller blocks on this until the user picks.  Confirm / cancel
        # set it to True; we use wait_variable so the parent's event
        # loop keeps spinning (lets the underlying panel render correctly
        # and lets us catch <Escape> bound on this widget).
        self._done_var = tk.BooleanVar(self, value=False)

        # The whole frame absorbs clicks so the modlist panel under it
        # is not interactable while the dialog is up.  We don't dim the
        # backdrop further because BG_DEEP already contrasts with the
        # card.
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Centred card.
        card = tk.Frame(self, bg=BG_PANEL, highlightthickness=1,
                        highlightbackground=BORDER)
        card.grid(row=0, column=0)
        card.grid_columnconfigure(0, weight=1)

        # Title band.
        tk.Label(
            card,
            text=f"Pack {bsa_filename}",
            font=(_theme.FONT_FAMILY, _theme.FS12, "bold"),
            fg=TEXT_MAIN, bg=BG_PANEL, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))

        row = 1
        if existing:
            tk.Label(
                card,
                text=(
                    f"⚠  {bsa_filename} already exists in this mod and will "
                    "be overwritten."
                ),
                font=(_theme.FONT_FAMILY, _theme.FS10),
                fg="#e8a83a", bg=BG_PANEL,
                anchor="w", justify="left", wraplength=380,
            ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 6))
            row += 1

        # Delete-loose checkbox.
        self._delete_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            card,
            text="Delete loose files after packing",
            variable=self._delete_var,
            font=(_theme.FONT_FAMILY, _theme.CTK_FS11),
            text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            border_color=BORDER, checkmark_color="white",
            checkbox_width=self._CHECKBOX_W,
            checkbox_height=self._CHECKBOX_H,
        ).grid(row=row, column=0, sticky="w", padx=16, pady=(10, 4))
        row += 1

        tk.Label(
            card,
            text=(
                "Files that get packed will be removed from the mod folder. "
                "Files outside the packable filter (plugins, readmes, .bik "
                "videos) and files you've disabled in the Mod Files tab are "
                "left alone."
            ),
            font=(_theme.FONT_FAMILY, _theme.FS9),
            fg=TEXT_DIM, bg=BG_PANEL,
            anchor="w", justify="left", wraplength=380,
        ).grid(row=row, column=0, sticky="ew", padx=(40, 16), pady=(0, 14))
        row += 1

        # Separate-textures checkbox (BSA only).  BA2 always splits
        # Main+Textures, so showing it there would be confusing.
        self._split_textures_var = tk.BooleanVar(value=False)
        if kind == "bsa":
            ctk.CTkCheckBox(
                card,
                text="Separate textures archive",
                variable=self._split_textures_var,
                font=(_theme.FONT_FAMILY, _theme.CTK_FS11),
                text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
                border_color=BORDER, checkmark_color="white",
                checkbox_width=self._CHECKBOX_W,
                checkbox_height=self._CHECKBOX_H,
            ).grid(row=row, column=0, sticky="w", padx=16, pady=(2, 4))
            row += 1
            tk.Label(
                card,
                text=(
                    "Writes textures to a sibling “… - Textures.bsa” instead "
                    "of bundling them with the main archive.  Optional for "
                    "Skyrim / FNV / Oblivion; mostly useful for very large "
                    "texture packs."
                ),
                font=(_theme.FONT_FAMILY, _theme.FS9),
                fg=TEXT_DIM, bg=BG_PANEL,
                anchor="w", justify="left", wraplength=380,
            ).grid(row=row, column=0, sticky="ew", padx=(40, 16), pady=(0, 14))
            row += 1

        # Skip-winners checkbox.  Loose files that *win* a conflict
        # would lose if packed (BSAs lose to loose files of subsequent
        # mods), so the user has the option to leave winners loose.
        # Files that already lose, or have no conflict, pack normally.
        self._skip_winners_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            card,
            text="Keep winning conflict files loose",
            variable=self._skip_winners_var,
            font=(_theme.FONT_FAMILY, _theme.CTK_FS11),
            text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            border_color=BORDER, checkmark_color="white",
            checkbox_width=self._CHECKBOX_W,
            checkbox_height=self._CHECKBOX_H,
        ).grid(row=row, column=0, sticky="w", padx=16, pady=(2, 4))
        row += 1
        tk.Label(
            card,
            text=(
                "Files this mod currently wins as loose are left out of "
                "the archive so deploy still picks them.  Files this mod "
                "already loses, or that have no conflict, are packed "
                "normally."
            ),
            font=(_theme.FONT_FAMILY, _theme.FS9),
            fg=TEXT_DIM, bg=BG_PANEL,
            anchor="w", justify="left", wraplength=380,
        ).grid(row=row, column=0, sticky="ew", padx=(40, 16), pady=(0, 14))
        row += 1

        # Button row.
        btn_row = tk.Frame(card, bg=BG_PANEL)
        btn_row.grid(row=row, column=0, sticky="ew", padx=12, pady=(4, 14))

        ctk.CTkButton(
            btn_row, text="Cancel", width=110, height=34,
            fg_color="transparent", border_width=1,
            text_color=TEXT_MAIN,
            font=(_theme.FONT_FAMILY, _theme.CTK_FS11),
            command=self._on_cancel,
        ).pack(side="right", padx=(8, 4))

        ctk.CTkButton(
            btn_row, text="Pack", width=110, height=34,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color=TEXT_ON_ACCENT,
            font=(_theme.FONT_FAMILY, _theme.CTK_FS11),
            command=self._on_confirm,
        ).pack(side="right")

        # Make Esc / Enter work even though we're not a top-level.
        self.bind("<Escape>", lambda e: self._on_cancel())
        self.bind("<Return>", lambda e: self._on_confirm())
        # Steal keyboard focus so Esc / Enter work without first
        # clicking inside the card.
        self.focus_set()

    def _on_confirm(self) -> None:
        self.result = {
            "delete_loose": bool(self._delete_var.get()),
            "split_textures": bool(self._split_textures_var.get()),
            "skip_winners": bool(self._skip_winners_var.get()),
        }
        self._done_var.set(True)

    def _on_cancel(self) -> None:
        self.result = None
        self._done_var.set(True)

    @classmethod
    def ask(
        cls, parent: tk.Misc, *,
        bsa_filename: str, existing: bool, kind: str = "bsa",
    ) -> dict | None:
        """Show the overlay over *parent* (typically the modlist panel)
        and block until the user clicks Pack or Cancel.

        Returns ``{"delete_loose": bool, "split_textures": bool}`` on
        confirm or ``None`` on cancel / Escape.
        """
        overlay = cls(
            parent,
            bsa_filename=bsa_filename,
            existing=existing,
            kind=kind,
        )
        # Cover the entire host panel.  The card itself is centred via
        # the overlay's grid, so the dim backdrop fills whatever's left.
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.lift()
        # Block until the user picks.  wait_variable spins the parent's
        # event loop, so the overlay (and the rest of the app) keeps
        # responding to events while we wait.
        overlay.wait_variable(overlay._done_var)
        result = overlay.result
        try:
            overlay.destroy()
        except tk.TclError:
            pass
        return result


class PluginPanel(PluginPanelExeLauncherMixin, PluginPanelLOOTMixin,
                  PluginPanelUserlistCycleMixin, PluginPanelArchiveMixin,
                  PluginPanelModFilesMixin, PluginPanelDataMixin,
                  PluginPanelIniMixin, ctk.CTkFrame):
    """Right panel: tabview with Plugins, Mod Files, Data, Downloads, Tracked."""

    PLUGIN_HEADERS = ["", "Plugin Name", "Flags", "🔒", "Index"]
    ROW_H = scaled(30)

    def __init__(self, parent, log_fn=None, get_filemap_path=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._log = log_fn or (lambda msg: None)
        self._get_filemap_path = get_filemap_path or (lambda: None)

        # Shared mtime-keyed parse of filemap.txt. On a large profile the file
        # is ~74k lines; several main-thread consumers (plugin sync, master
        # checking, staged-path resolution, the Data/Ini tabs) each used to
        # re-read+parse it per refresh. This caches the parsed
        # list[(rel_path, mod_name)] once per filemap mtime — see
        # _get_parsed_filemap().
        self._filemap_parse_cache: list[tuple[str, str]] | None = None
        self._filemap_parse_cache_sig: tuple[str, float] | None = None

        # Current game (set by caller when game changes)
        self._game = None

        # Plugin system state
        self._plugins_path: Path | None = None
        self._plugin_extensions: list[str] = []
        self._plugin_entries: list[PluginEntry] = []
        self._sel_idx: int = -1
        self._psel_set: set[int] = set()  # all selected plugin indices
        self._phover_idx: int = -1        # plugin row index under the mouse cursor
        self._plugin_mod_map: dict[str, str] = {}  # plugin name → staging mod folder name
        self._highlighted_plugins: set[str] = set()  # plugin names highlighted when their mod is selected
        self._master_highlights: set[str] = set()    # master plugin names for the currently selected plugin
        self._masters_lower_cache: set[str] = set()  # lowercased _master_highlights (keyed by identity)
        self._masters_lower_src: set[str] | None = None
        self._fi_reverse_cache: dict[int, int] = {}  # data idx → view row (keyed by identity)
        self._fi_reverse_src: list[int] | None = None
        self._plugin_counter_texts: tuple = ()
        # Plugins of mods in a BSA conflict with the currently-selected mod.
        # Semantics mirror the modlist panel's row colouring:
        #   _bsa_conflict_higher_plugins — plugins whose mod LOSES to the
        #       selection (the selection is "higher") → paint green.
        #   _bsa_conflict_lower_plugins  — plugins whose mod BEATS the
        #       selection (the selection is "lower") → paint red.
        # Plugin names are stored lowercase.
        self._bsa_conflict_higher_plugins: set[str] = set()
        self._bsa_conflict_lower_plugins: set[str] = set()
        self._plugin_paths: dict[str, "Path"] = {}   # plugin name.lower() → path on disk
        self._on_plugin_selected_cb = None  # callable(mod_name: str | None)
        self._on_mod_selected_cb = None     # callable() — notify mod panel a plugin was selected
        self._on_plugin_row_selected_cb = None  # callable(plugin_name: str) — notify when a plugin row is selected

        # Missing masters detection
        self._missing_masters: dict[str, list[str]] = {}
        self._late_masters: dict[str, list[str]] = {}
        self._version_mismatch_masters: dict[str, list[str]] = {}
        self._staging_root: Path | None = None
        self._data_dir: Path | None = None

        _flag_icon_sz = scaled(18)

        # Warning icon for missing masters (canvas-compatible PhotoImage)
        self._warning_icon: ImageTk.PhotoImage | None = None
        _warn_path = _ICONS_DIR / "warning2.png"
        if _warn_path.is_file():
            _img = PilImage.open(_warn_path).convert("RGBA").resize((_flag_icon_sz, _flag_icon_sz), PilImage.LANCZOS)
            self._warning_icon = ImageTk.PhotoImage(_img)

        # Warning icon for late-loaded masters
        self._late_warn_icon: ImageTk.PhotoImage | None = None
        _late_warn_path = _ICONS_DIR / "warning.png"
        if _late_warn_path.is_file():
            _img2 = PilImage.open(_late_warn_path).convert("RGBA").resize((_flag_icon_sz, _flag_icon_sz), PilImage.LANCZOS)
            self._late_warn_icon = ImageTk.PhotoImage(_img2)

        # Warning icon for version-mismatched masters
        self._version_mismatch_icon: ImageTk.PhotoImage | None = None
        _vmm_path = _ICONS_DIR / "info.png"
        if _vmm_path.is_file():
            _img3 = PilImage.open(_vmm_path).convert("RGBA").resize((_flag_icon_sz, _flag_icon_sz), PilImage.LANCZOS)
            self._version_mismatch_icon = ImageTk.PhotoImage(_img3)

        # LOOT info icon — shown when a plugin has one or more masterlist messages
        self._loot_info_icon: ImageTk.PhotoImage | None = None
        _loot_info_path = _ICONS_DIR / "Loot_info.png"
        if _loot_info_path.is_file():
            _img4 = PilImage.open(_loot_info_path).convert("RGBA").resize((_flag_icon_sz, _flag_icon_sz), PilImage.LANCZOS)
            self._loot_info_icon = ImageTk.PhotoImage(_img4)

        # Dirty-edits icon — shown when a plugin has CRC-matched dirty info
        self._loot_dirty_icon: ImageTk.PhotoImage | None = None
        _loot_dirty_path = _ICONS_DIR / "brush.png"
        if _loot_dirty_path.is_file():
            _img5 = PilImage.open(_loot_dirty_path).convert("RGBA").resize((_flag_icon_sz, _flag_icon_sz), PilImage.LANCZOS)
            self._loot_dirty_icon = ImageTk.PhotoImage(_img5)

        # Bash Tags icon — shown when a plugin has current/suggested Bash Tags
        self._loot_tag_icon: ImageTk.PhotoImage | None = None
        _loot_tag_path = _ICONS_DIR / "tag.png"
        if _loot_tag_path.is_file():
            _img6 = PilImage.open(_loot_tag_path).convert("RGBA").resize((_flag_icon_sz, _flag_icon_sz), PilImage.LANCZOS)
            self._loot_tag_icon = ImageTk.PhotoImage(_img6)

        # Lock icon
        self._icon_lock: ImageTk.PhotoImage | None = None
        _lock_path = _ICONS_DIR / "lock.png"
        if _lock_path.is_file():
            _lk_sz = scaled(18)
            self._icon_lock = ImageTk.PhotoImage(
                PilImage.open(_lock_path).convert("RGBA").resize((_lk_sz, _lk_sz), PilImage.LANCZOS))

        # Tooltip state
        self._tooltip = TkTooltip(
            self,
            bg=BG_OVERLAY_ERR,
            fg=STATUS_ERR_BRIGHT,
            font=(_theme.FONT_FAMILY, _theme.FS10),
        )

        # Mod note editor overlay (instantiated on demand, see show_notes_editor)
        self._notes_overlay = None

        # Canvas column x-positions (patched in _layout_plugin_cols)
        self._pcol_x = [scaled(4), scaled(32), 0, 0, 0]  # checkbox, name, flags, lock, index

        # Drag state
        self._drag_idx: int = -1
        self._drag_start_y: int = 0
        self._drag_moved: bool = False
        self._drag_slot: int = -1
        self._pdrag_scroll_after: str | None = None  # after() id for auto-scroll repeat
        self._pdrag_last_event_y: int = 0  # last widget-space Y from mouse drag

        # Vanilla plugins (locked — cannot be disabled by the user)
        self._vanilla_plugins: dict[str, str] = {}  # lowercase -> original name

        # User-locked plugins: plugin name (original case) → bool
        self._plugin_locks: dict[str, bool] = {}

        # Framework status banners (one CTkFrame per framework entry)
        self._framework_banner_widgets: list[ctk.CTkFrame] = []

        # Plugin search filter
        self._plugin_search_var: tk.StringVar | None = None  # initialised in _build_plugins_tab
        self._plugin_filtered_indices: list[int] | None = None  # None = no filter active

        # Virtual-list pool (fixed-size widget + canvas item pool for visible rows)
        self._pool_size: int = 60
        # Per-slot rendered-state cache — tuple of inputs; if unchanged we skip Tk calls.
        self._pool_last_state: list[tuple | None] = []
        self._pool_data_idx: list[int] = []
        self._pool_bg: list[int] = []
        self._pool_name: list[int] = []
        self._pool_idx_text: list[int] = []
        self._pool_warn: list[int | None] = []
        self._pool_late_warn: list[int | None] = []
        self._pool_vmm_warn: list[int | None] = []
        self._pool_missing_strip: list[int] = []
        self._pool_check_rects: list[int] = []
        self._pool_check_marks: list[int] = []
        self._pool_lock_rects: list[int] = []
        self._pool_lock_marks: list[int] = []
        self._pool_ul_dot: list[int] = []
        self._pool_esl_badge: list[int] = []
        self._pool_loot_info: list[int | None] = []
        self._pool_loot_dirty: list[int | None] = []
        self._pool_loot_tag: list[int | None] = []
        self._pool_bos_sp_badge: list[int] = []
        # lowercase plugin name -> full info dict persisted to loot.json:
        # {messages, requirements, incompatibilities, locations, dirty, clean}
        self._loot_info: dict[str, dict] = {}
        self._esl_flagged_plugins: set[str] = set()  # lowercase plugin names with ESL flag set
        self._master_flag_plugins: set[str] = set()  # lowercase .esp names with the master header bit
        self._esl_safe_plugins: set[str] = set()    # lowercase plugin names eligible for ESL flag
        self._esl_unsafe_plugins: set[str] = set()  # lowercase plugin names ineligible for ESL flag
        # Cache for ESL eligibility results.
        # Key: ((path_str, mtime_ns, size), game_type_attr, algo_version).
        # check_esl_eligible() now delegates to libloot, which is stricter than
        # the old FormID-range scan — bumping _ESL_ELIG_CACHE_VERSION invalidates
        # any stale True results that libloot would now reject.
        self._esl_eligible_cache: dict[tuple, bool] = {}
        # Cache for _check_all_masters() — the filemap+staging scan and master/
        # version-mismatch checks are expensive (~450 ms for 1300 plugins).
        # Keyed by (filemap_mtime, plugins_tuple, data_dir_str).
        self._masters_cache_key: tuple | None = None
        # BOS / SkyPatcher patch detection
        # plugin_name.lower() -> "bos", "sp", or "both"
        self._bos_sp_plugins: dict[str, str] = {}
        self._bos_sp_cache_key: tuple | None = None  # (staging_mtime, staging_str)
        self._userlist_plugins: set[str] = set()
        # Plugins (lowercased names) that participate in a userlist.yaml cycle.
        # Used to paint the userlist dot red instead of white.
        self._userlist_cycle_plugins: set[str] = set()
        # plugin_name_lower → frozenset of all plugins in the same SCC.
        # Drives the "Show cycle" overlay so one click surfaces the full cycle.
        self._userlist_cycle_components: dict[str, frozenset[str]] = {}
        # (u_lower, v_lower) → list of reason dicts for the u→v edge
        # ("must load before"). Used by the overlay to explain / flip rules.
        self._userlist_cycle_edges: dict[tuple[str, str], list[dict]] = {}
        # Open-overlay bookkeeping — lowercased plugin name the overlay is
        # pinned to, plus the fixed scope (SCC at open time) so we can keep
        # showing rules after the user flips the cycle away.
        self._plugin_cycle_overlay = None
        self._plugin_cycle_anchor: str = ""
        self._plugin_cycle_scope: frozenset[str] = frozenset()
        # Plugin filter panel state
        self._plugin_filter_state: dict = {}
        self._plugin_filter_panel_open: bool = False
        self._plugin_group_map: dict[str, str] = {}  # plugin name lower → group name
        self._predraw_after_id: str | None = None
        self._marker_strip_after_id: str | None = None

        # Canvas dimensions
        self._pcanvas_w: int = 400

        # Mod Files tab state
        self._mod_files_mod_name: str | None = None   # currently displayed mod
        self._mod_files_index_path: Path | None = None  # modindex.bin path
        self._mod_files_profile_dir: Path | None = None  # profile dir for excluded_mod_files in profile_state.json
        self._mod_files_excluded: dict[str, set[str]] = {}  # mod_name → set of excluded rel_keys
        self._mod_files_on_change: callable | None = None  # called when exclusions change
        self._plugin_order_on_change: callable | None = None  # called after plugin load order changes

        # Archive tab state (BSA contents viewer, Bethesda-only)
        self._archive_mod_name: str | None = None
        self._bsa_index_path: Path | None = None
        self._arc_tree: ttk.Treeview | None = None
        self._arc_tree_expanded: bool = False
        self._arc_expand_btn: tk.Button | None = None
        self._archive_label: tk.Label | None = None
        self._bsa_conflict_cache: tuple | None = None
        self._arc_search_var: tk.StringVar | None = None

        # "Show only conflicts" filter state (per-tab)
        self._data_only_conflicts_var: tk.BooleanVar | None = None
        self._arc_only_conflicts_var: tk.BooleanVar | None = None

        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Executable toolbar — use design sizes; CTk scales widgets, scaled() would double-scale
        exe_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0)
        exe_bar.grid(row=0, column=0, sticky="ew", padx=scaled(4), pady=(scaled(4), 0))

        self._exe_var = tk.StringVar(value="")
        # Stores full Path objects in display-name order, parallel to dropdown values
        self._exe_paths: list[Path] = []
        # Parallel to _exe_paths; the game's launch exe is shown as the game name.
        self._exe_labels: list[str] = []
        self._game_exe_path: Path | None = None
        self._exe_menu = ctk.CTkOptionMenu(
            exe_bar, values=["(no executables)"], variable=self._exe_var,
            width=175, font=_theme.FONT_SMALL,
            fg_color=BG_PANEL, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_exe_selected,
        )
        # Pack fixed-width buttons on the right first so they're never squeezed out
        self._exe_args_var = tk.StringVar(value="")

        ctk.CTkButton(
            exe_bar, text="⊘", width=30, height=30, font=_theme.FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_exe_filter,
        ).pack(side="right", padx=(0, scaled(4)), pady=scaled(6))

        ctk.CTkButton(
            exe_bar, text="📂", width=30, height=30, font=_theme.FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._open_applications_folder,
        ).pack(side="right", padx=(0, scaled(4)), pady=scaled(6))

        refresh_icon = _load_icon("refresh.png", size=(16, 16))
        ctk.CTkButton(
            exe_bar, text="" if refresh_icon else "↺", image=refresh_icon,
            width=30, height=30, font=_theme.FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self.refresh_exe_list,
        ).pack(side="right", padx=(0, scaled(4)), pady=scaled(6))

        settings_icon = _load_icon("settings.png", size=(16, 16))
        ctk.CTkButton(
            exe_bar, text="" if settings_icon else "⚙", image=settings_icon,
            width=30, height=30, font=_theme.FONT_SMALL,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_configure_exe,
        ).pack(side="right", padx=(0, scaled(4)), pady=scaled(6))

        self._run_exe_btn = ctk.CTkButton(
            exe_bar, text="▶ Run EXE", width=90, height=28, font=_theme.FONT_SMALL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_run_exe,
        )
        self._run_exe_btn.pack(side="right", padx=(0, scaled(4)), pady=scaled(6))

        # Dropdown fills remaining space on the left
        self._exe_menu.pack(side="left", padx=(scaled(8), scaled(4)), pady=scaled(6), expand=True, fill="x")

        self._tabs = ctk.CTkTabview(
            self, fg_color=BG_PANEL, corner_radius=4,
            segmented_button_fg_color=BG_HEADER,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOV,
            segmented_button_unselected_color=BG_HEADER,
            segmented_button_unselected_hover_color=BG_HOVER,
            text_color=TEXT_MAIN,
            command=self._on_tab_changed,
        )
        self._tabs.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)

        for name in ("Plugins", "Mod Files", "Ini Files", "Data", "Downloads"):
            self._tabs.add(name)

        # The "Ini Files" tab shows far more than .ini files (json, txt, cfg,
        # yaml, xml, …), so present it as "Text Files" cosmetically while keeping
        # "Ini Files" as the internal tab key used everywhere else.
        try:
            self._tabs._segmented_button._buttons_dict["Ini Files"].configure(
                text="Text Files"
            )
        except (KeyError, AttributeError):
            pass

        # Lazy-refresh flags: these tabs are expensive to rebuild on every
        # filemap change, so they are only rebuilt when their tab is selected.
        self._data_tab_dirty: bool = False
        self._ini_files_tab_dirty: bool = False
        self._data_filemap_entries: list[tuple[str, str]] = []
        self._data_filemap_casefold: list[tuple[str, str]] = []
        self._data_search_prev_query: str = ""
        self._data_search_prev_indices: list[int] | None = None
        self._data_search_after_id: str | None = None
        self._archive_tab_dirty: bool = False
        # Cache for _resolve_data_entries — keyed on (filemap_path, mtime, game_id).
        self._data_resolved_cache: tuple | None = None
        # Lazy folder realisation: maps node iid -> subtree dict (unrealised).
        self._data_lazy_subtrees: dict[str, dict] = {}
        # Track folder iid -> (mod_name_for_highlight) so re-highlight works
        # without re-realising the whole tree.
        self._data_realised_nodes: set[str] = set()
        # Data tab filter side panel state
        self._data_filter_filetypes: frozenset[str] = frozenset()
        self._data_filter_filetypes_exclude: frozenset[str] = frozenset()
        self._data_filter_panel_open: bool = False

        self._build_plugins_tab()
        self._build_mod_files_tab()
        self._build_ini_files_tab()
        self._build_data_tab()
        self._build_downloads_tab()
        # Archive tab is gated on game.archive_extensions — added lazily by
        # _update_archive_tab_visibility() when a BSA-using game loads.

    # ------------------------------------------------------------------
    # Thread-safe after() — skips scheduling if the widget/Tk root is gone.
    # Worker threads can race app shutdown; calling self.after() on a torn-down
    # interpreter raises "main thread is not in main loop".
    # ------------------------------------------------------------------

    def _safe_after(self, delay, func):
        try:
            if not self.winfo_exists():
                return
            return self.after(delay, func)
        except (RuntimeError, tk.TclError):
            return None

    # ------------------------------------------------------------------
    # Tab change handler — lazy refresh for expensive tabs
    # ------------------------------------------------------------------

    def _on_tab_changed(self):
        """Called when the user switches tabs.  Rebuilds dirty tabs on demand."""
        current = self._tabs.get()
        if current == "Data" and self._data_tab_dirty:
            self._data_tab_dirty = False
            self._refresh_data_tab()
        elif current == "Ini Files" and self._ini_files_tab_dirty:
            self._ini_files_tab_dirty = False
            self._refresh_ini_files_tab()
        elif current == "Archive" and self._archive_tab_dirty:
            self._archive_tab_dirty = False
            self._render_archive_tree(self._archive_mod_name)

    @property
    def _plugins_star_prefix(self) -> bool:
        """Return whether plugins.txt for the current game uses '*' prefixes."""
        return getattr(self._game, "plugins_use_star_prefix", True)

    @property
    def _plugins_include_vanilla(self) -> bool:
        """Return whether vanilla plugins should be written into plugins.txt."""
        return getattr(self._game, "plugins_include_vanilla", False)

    def _archive_kind_for_current_game(self) -> str | None:
        """Return ``"bsa"`` for games that pack into BSA v104/v105,
        ``"ba2"`` for games that pack into FO4-family BA2, and ``None``
        for games that don't support archive packing yet (Starfield,
        FO76, Morrowind, non-Bethesda).

        Used to drive button labels and pack/unpack dispatch."""
        if self._game is None:
            return None
        from Utils.ba2_writer import ba2_version_for_game
        from Utils.bsa_writer import bsa_version_for_game
        game_id = getattr(self._game, "game_id", None) or type(self._game).__name__
        archive_exts = getattr(self._game, "archive_extensions", None) or frozenset()
        if bsa_version_for_game(game_id) is not None and ".bsa" in archive_exts:
            return "bsa"
        if ba2_version_for_game(game_id) is not None and ".ba2" in archive_exts:
            return "ba2"
        return None

    def _update_pack_bsa_button_state(self) -> None:
        """Enable Pack/Unpack archive buttons only when a normal mod is
        selected and the current game uses BSA *or* BA2 archives. Hides
        them entirely on games we can't pack for. Button labels switch
        between "Pack BSA" / "Pack BA2" depending on the active game.
        Unpack is further gated on the mod containing at least one
        archive of the matching kind."""
        pack_btn = getattr(self, "_mf_pack_bsa_btn", None)
        unpack_btn = getattr(self, "_mf_unpack_bsa_btn", None)
        if pack_btn is None or unpack_btn is None:
            return
        kind = self._archive_kind_for_current_game()
        if kind is None:
            for btn in (pack_btn, unpack_btn):
                try:
                    btn.pack_forget()
                except Exception:
                    pass
            return
        # Game supports archive packing — make sure both buttons are
        # visible and labelled for the right kind.
        upper = kind.upper()
        pack_btn.configure(text=f"Pack {upper}")
        unpack_btn.configure(text=f"Unpack {upper}")
        try:
            pack_btn.pack_info()
        except Exception:
            pack_btn.pack(side="left", padx=(8, 4), pady=4)
        try:
            unpack_btn.pack_info()
        except Exception:
            unpack_btn.pack(side="left", padx=(0, 4), pady=4)
        mod_name = self._mod_files_mod_name
        is_normal_mod = (
            mod_name is not None
            and mod_name != _OVERWRITE_NAME
            and mod_name != "Root_Folder"
        )
        pack_btn.configure(state="normal" if is_normal_mod else "disabled")

        # Unpack: also requires the mod to have at least one archive of
        # the matching kind on disk.
        archive_suffix = "." + kind
        has_archive = False
        if is_normal_mod and self._staging_root is not None:
            mod_dir = self._staging_root / mod_name
            try:
                has_archive = any(
                    p.is_file() and p.suffix.lower() == archive_suffix
                    for p in mod_dir.iterdir()
                )
            except OSError:
                has_archive = False
        unpack_btn.configure(state="normal" if has_archive else "disabled")

    def _is_current_profile_deployed(self) -> bool:
        """Return True iff the profile this Mod Files tab is showing is the
        one currently deployed to the game folder.

        Pack/Unpack mutate mod-folder contents.  When that profile has
        files hard-linked into game_root, the deploy log + snapshot go
        stale and the next restore moves what should be tracked files
        into ``overwrite/`` as if they were runtime-generated.  Forcing a
        Restore first sidesteps the whole class of failure.
        """
        if self._game is None or not getattr(self._game, "is_configured", lambda: False)():
            return False
        if self._mod_files_profile_dir is None:
            return False
        try:
            if not self._game.get_deploy_active():
                return False
            return self._game.get_last_deployed_profile() == self._mod_files_profile_dir.name
        except Exception:
            return False

    def _find_pack_trigger_plugin(self, mod_dir: Path, mod_name: str) -> Path | None:
        """Return the real plugin in ``mod_dir`` (root) that an archive
        packed for this mod should be named after, or None if the mod
        ships no real plugin.

        An archive only auto-loads when a same-stem plugin sits in the
        load order.  Naming the archive after an existing plugin avoids
        generating a redundant stub.  When the mod has several plugins
        we prefer the one whose stem matches the mod folder, then fall
        back to the first sorted by name (an archive can only be
        triggered by ONE plugin name, so we must pick deterministically).

        Stubs we generated ourselves are ignored — they aren't a real
        plugin, and treating them as the trigger would defeat the point
        of regenerating them.  Only the mod-folder root is scanned, to
        match where the archive is written (the trigger plugin and the
        archive must land in the same Data folder after deploy).
        """
        from Utils.bsa_writer import is_our_stub_plugin

        exts = {e.lower() for e in (self._plugin_extensions or [".esp", ".esm", ".esl"])}
        plugins: list[Path] = []
        try:
            for p in mod_dir.iterdir():
                if not p.is_file() or p.suffix.lower() not in exts:
                    continue
                if is_our_stub_plugin(p):
                    continue
                plugins.append(p)
        except OSError:
            return None
        if not plugins:
            return None
        # Prefer a plugin whose stem matches the mod folder name (case-
        # insensitive); otherwise the first by name for determinism.
        mod_lower = mod_name.lower()
        plugins.sort(key=lambda p: p.name.lower())
        for p in plugins:
            if p.stem.lower() == mod_lower:
                return p
        return plugins[0]

    def _on_pack_bsa_click(self) -> None:
        """Pack the currently-selected mod's loose files into a BSA in that
        mod's folder. Runs on a background thread with a progress popup."""
        import threading
        from Utils.ba2_writer import Ba2WriteError, write_ba2, write_ba2_textures
        from Utils.bsa_writer import (
            BsaWriteError, bsa_version_for_game, write_bsa, write_stub_plugin,
        )
        from gui.ctk_components import CTkAlert, CTkProgressPopup

        mod_name = self._mod_files_mod_name
        if not mod_name or mod_name in (_OVERWRITE_NAME, "Root_Folder"):
            return
        if self._staging_root is None or self._game is None:
            return

        # Pick BSA or BA2 based on the active game.  Returns None for
        # games we can't pack for (Starfield, FO76, Morrowind, …).
        kind = self._archive_kind_for_current_game()
        if kind is None:
            return
        kind_upper = kind.upper()

        # Refuse to mutate mod contents while the profile is deployed:
        # the existing hard-links in game_root would become stale and
        # the next Restore would mis-classify them as runtime files.
        if self._is_current_profile_deployed():
            CTkAlert(
                state="warning", title=f"Pack {kind_upper}",
                body_text=(
                    "This profile is currently deployed.\n\n"
                    f"Run Restore first, then pack the {kind_upper}."
                ),
                btn1="OK", btn2="", parent=self.winfo_toplevel(),
            )
            return

        game_id = getattr(self._game, "game_id", None) or type(self._game).__name__
        # BSA path needs the format version (104 vs 105); BA2 currently
        # always emits v1 GNRL so we don't care about the value.
        bsa_version = bsa_version_for_game(game_id) if kind == "bsa" else None

        mod_dir = self._staging_root / mod_name
        if not mod_dir.is_dir():
            CTkAlert(
                state="warning", title=f"Pack {kind_upper}",
                body_text=f"Mod folder not found:\n{mod_dir}",
                btn1="OK", btn2="", parent=self.winfo_toplevel(),
            )
            return

        archive_suffix = "." + kind

        # The archive only auto-loads when a same-stem plugin sits in the
        # load order.  If the mod already ships a real plugin we name the
        # archive after *that* plugin's stem (not the mod folder) so the
        # game mounts it without us having to stamp out a redundant stub.
        # `archive_stem` falls back to the mod-folder name when the mod
        # has no real plugin — in that case a stub is generated below.
        existing_plugin = self._find_pack_trigger_plugin(mod_dir, mod_name)
        archive_stem = existing_plugin.stem if existing_plugin is not None else mod_name

        # FO4 / FO4 VR's auto-loader only mounts a BA2 when its filename
        # follows the "<plugin_stem> - Main.ba2" / " - Textures.ba2"
        # convention.  Vanilla and every community mod ships with that
        # naming; <stem>.ba2 silently doesn't load.  Skyrim's BSA loader
        # is more permissive — <stem>.bsa works on its own — so we keep
        # the bare name there.
        #
        # On FO4 we always write the GNRL "<stem> - Main.ba2", and if
        # the mod has any .dds files we also write a sibling DX10
        # "<stem> - Textures.ba2".  We don't know yet whether the mod
        # contains textures, so we prepare both paths and let the
        # writer raise "no DX10-eligible texture files found" when
        # there's nothing to do.
        # On FO4 we always write the GNRL "<stem> - Main.ba2", and if
        # the mod has any .dds files we also write a sibling DX10
        # "<stem> - Textures.ba2".  Skyrim's BSA loader is more
        # permissive — <stem>.bsa works on its own — so the textures
        # sibling there is OPTIONAL: a checkbox in _PackOptionsDialog
        # below decides.
        if kind == "ba2":
            archive_path = mod_dir / f"{archive_stem} - Main.ba2"
            # archive_textures_path is set unconditionally for BA2; the
            # writer drops it if the mod has no DDS files.
            archive_textures_path: Path | None = mod_dir / f"{archive_stem} - Textures.ba2"
        else:
            archive_path = mod_dir / f"{archive_stem}{archive_suffix}"
            archive_textures_path = None  # may be set after the dialog
        # Show the options dialog: confirms the pack, surfaces the
        # overwrite warning if applicable, and lets the user opt into
        # deleting the loose files that get packed and (BSA only) the
        # split-textures sibling.  Returns None on cancel.
        existing_any = archive_path.exists() or (
            archive_textures_path is not None and archive_textures_path.exists()
        )
        # Host the overlay on the modlist panel so it sits over the
        # left-hand panel (where the user just clicked Pack BSA from
        # the modlist's adjacent Mod Files tab).  Falling back to the
        # plugin panel keeps the overlay in-app even if the modlist
        # panel hasn't been wired up for some reason.
        overlay_host = (
            getattr(self.winfo_toplevel(), "_mod_panel", None) or self
        )
        opts = _PackOptionsDialog.ask(
            overlay_host,
            bsa_filename=archive_path.name,
            existing=existing_any,
            kind=kind,
        )
        if opts is None:
            return
        delete_loose: bool = opts["delete_loose"]
        split_textures: bool = opts.get("split_textures", False)
        skip_winners: bool = opts.get("skip_winners", False)
        # For BSA, only allocate the textures sibling path if the user
        # ticked the "Separate textures archive" checkbox.
        if kind == "bsa" and split_textures:
            archive_textures_path = mod_dir / f"{archive_stem} - Textures.bsa"

        # An archive only auto-loads if a same-named plugin sits in the
        # load order.  `existing_plugin` (resolved above, used to pick
        # `archive_stem`) is the mod's real trigger plugin when one
        # exists — we leave it alone.  It already excludes stubs we've
        # generated, so `None` covers both "no plugin at all" and "only
        # a previous stub": either way we (re)stamp a minimal stub named
        # after the mod folder (its format may have evolved between packs).
        stub_plugin_path: Path | None = None
        if existing_plugin is None:
            stub_plugin_path = mod_dir / f"{mod_name}.esp"

        # Files the user has disabled in the Mod Files tab — skip them
        # in the pack so they aren't archived, and aren't re-enabled
        # implicitly when we auto-disable everything that was packed.
        excluded_set: set[str] = set(self._mod_files_excluded.get(mod_name, set()))

        # If the user opted to keep their winning conflict files loose,
        # add every rel_key this mod currently *wins a real conflict on*
        # to the exclusion set.  A "real" winner needs both:
        #   * filemap_winner[rk] == this mod (this mod's copy deploys)
        #   * rk is in contested_keys (more than one mod ships that path)
        # Otherwise filemap_winner contains every uncontested file too,
        # which would exclude almost the whole mod.
        if skip_winners:
            contested_keys, filemap_winner = self._get_conflict_cache(None)
            winners = {
                rk for rk, owner in filemap_winner.items()
                if owner == mod_name and rk in contested_keys
            }
            excluded_set |= winners

        excluded_now = frozenset(excluded_set)

        popup = CTkProgressPopup(
            self.winfo_toplevel(),
            title=f"Pack {kind_upper}",
            label=f"Packing {mod_name}…",
            message="Scanning files…",
        )

        # Result shape from the worker:
        #   ("ok", main_count, main_size, tex_count, tex_size, all_packed_keys)
        # tex_count == 0 means no textures archive was written.
        def _on_done(
            result: tuple | None,
            error: str | None,
        ) -> None:
            if popup.winfo_exists():
                popup.close_progress_popup()
            if error is not None:
                CTkAlert(
                    state="warning", title=f"Pack {kind_upper} failed",
                    body_text=error, btn1="OK", btn2="",
                    parent=self.winfo_toplevel(),
                )
                return
            assert result is not None
            main_count, main_size, tex_count, tex_size, packed_keys = result
            # Either physically delete the loose files, or just mark them
            # disabled in the Mod Files tab so deploy skips them.  Both
            # outcomes mean only the archive reaches the game's Data
            # folder; delete is destructive, disable is reversible.
            deleted_count = 0
            if delete_loose:
                deleted_count = self._delete_loose_files(mod_dir, packed_keys)
            else:
                self._auto_disable_packed_files(mod_name, packed_keys)
            # Trigger a filemap rebuild — the new archive needs to enter
            # the mod-index cache so conflict detection picks it up, and
            # the auto-disable / delete changed what's on disk.
            mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
            if mod_panel is not None:
                try:
                    mod_panel._filemap_rescan_index = True
                    mod_panel._rebuild_filemap()
                except Exception:
                    pass
            # Re-render the Mod Files tree so the new archive shows up
            # and the auto-disabled / removed rows reflect the new state.
            try:
                self.show_mod_files(mod_name)
            except Exception:
                pass
            stub_msg = (
                f"\n\nGenerated {stub_plugin_path.name} so the game will "
                "auto-load the archive."
                if stub_plugin_path is not None else ""
            )
            delete_msg = (
                f"\n\nDeleted {deleted_count} loose file(s) from the mod folder."
                if delete_loose else ""
            )
            # Build the success body — one line per archive emitted.
            lines = []
            if main_count > 0:
                lines.append(
                    f"Packed {main_count} file(s) into {archive_path.name} "
                    f"({main_size / (1024 * 1024):.1f} MiB)."
                )
            if tex_count > 0 and archive_textures_path is not None:
                lines.append(
                    f"Packed {tex_count} texture file(s) into "
                    f"{archive_textures_path.name} "
                    f"({tex_size / (1024 * 1024):.1f} MiB)."
                )
            CTkAlert(
                state="info", title=f"Pack {kind_upper}",
                body_text="\n".join(lines) + stub_msg + delete_msg,
                btn1="OK", btn2="",
                parent=self.winfo_toplevel(),
            )

        def _worker() -> None:
            def _progress(done: int, total: int, current: str) -> None:
                # Marshal to the main thread.
                def _ui() -> None:
                    if not popup.winfo_exists():
                        return
                    popup.update_progress(done / max(total, 1))
                    popup.update_message(f"{done} / {total}  —  {current[-50:]}")
                try:
                    self.after(0, _ui)
                except Exception:
                    pass

            def _cancel() -> bool:
                return bool(getattr(popup, "cancelled", False))

            main_count = 0
            main_size = 0
            tex_count = 0
            tex_size = 0
            all_packed: list[str] = []

            try:
                if kind == "ba2":
                    # GNRL pass — everything except .dds.  May raise
                    # "no packable files found" if the mod is *only*
                    # textures; we treat that as non-fatal and continue
                    # to the textures pass.
                    try:
                        main_count, main_size, packed_main = write_ba2(
                            archive_path, mod_dir,
                            game_id=game_id,
                            compress=True,
                            excluded_keys=excluded_now,
                            exclude_textures=True,
                            progress=_progress,
                            cancel=_cancel,
                        )
                        all_packed.extend(packed_main)
                    except Ba2WriteError as exc:
                        if "no packable" not in str(exc).lower():
                            raise
                    # DX10 pass — only .dds files.  May raise
                    # "no packable texture files found" / "no DX10-
                    # eligible" if the mod has no DDS or only legacy
                    # (non-DX10) DDS.  Non-fatal.
                    try:
                        tex_count, tex_size, packed_tex = write_ba2_textures(
                            archive_textures_path, mod_dir,
                            game_id=game_id,
                            compress=True,
                            excluded_keys=excluded_now,
                            progress=_progress,
                            cancel=_cancel,
                        )
                        all_packed.extend(packed_tex)
                    except Ba2WriteError as exc:
                        msg = str(exc).lower()
                        if "no packable" not in msg and "no dx10" not in msg:
                            raise
                    if main_count == 0 and tex_count == 0:
                        raise Ba2WriteError("no packable files found")
                else:
                    # BSA path.  When the user opted into the split
                    # ("Separate textures archive"), we run two passes:
                    # the base archive excludes textures, the sibling
                    # contains only textures.  Either pass can raise
                    # "no packable files found" without aborting the
                    # other (a textures-only mod still produces a
                    # ` - Textures.bsa`; a no-textures mod still
                    # produces the base BSA).
                    if split_textures and archive_textures_path is not None:
                        try:
                            main_count, main_size, packed_main = write_bsa(
                                archive_path, mod_dir,
                                version=bsa_version,
                                game_id=game_id,
                                compress=True,
                                excluded_keys=excluded_now,
                                texture_mode="exclude",
                                progress=_progress,
                                cancel=_cancel,
                            )
                            all_packed.extend(packed_main)
                        except BsaWriteError as exc:
                            if "no packable" not in str(exc).lower():
                                raise
                        try:
                            tex_count, tex_size, packed_tex = write_bsa(
                                archive_textures_path, mod_dir,
                                version=bsa_version,
                                game_id=game_id,
                                compress=True,
                                excluded_keys=excluded_now,
                                texture_mode="only",
                                progress=_progress,
                                cancel=_cancel,
                            )
                            all_packed.extend(packed_tex)
                        except BsaWriteError as exc:
                            if "no packable" not in str(exc).lower():
                                raise
                        if main_count == 0 and tex_count == 0:
                            raise BsaWriteError("no packable files found")
                    else:
                        main_count, main_size, packed_main = write_bsa(
                            archive_path, mod_dir,
                            version=bsa_version,
                            game_id=game_id,
                            compress=True,
                            excluded_keys=excluded_now,
                            progress=_progress,
                            cancel=_cancel,
                        )
                        all_packed.extend(packed_main)
                if stub_plugin_path is not None:
                    write_stub_plugin(stub_plugin_path, game_id=game_id)
            except (BsaWriteError, Ba2WriteError) as exc:
                msg = str(exc)
                if msg == "cancelled":
                    self.after(0, lambda: popup.close_progress_popup() if popup.winfo_exists() else None)
                    return
                self.after(0, lambda m=msg: _on_done(None, m))
                return
            except Exception as exc:  # last-resort safety net
                self.after(0, lambda m=str(exc): _on_done(None, m))
                return
            result = (main_count, main_size, tex_count, tex_size, all_packed)
            self.after(0, lambda r=result: _on_done(r, None))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_unpack_bsa_click(self) -> None:
        """Open the Unpack archive overlay over the plugin panel.

        Despite the legacy method / attribute names ('bsa'), this drives
        unpacking for both BSA and BA2 archives — dispatch happens in
        :meth:`_do_unpack_bsa` based on the chosen file's suffix."""
        from gui.bsa_unpack_overlay import BsaUnpackOverlay
        from gui.ctk_components import CTkAlert

        mod_name = self._mod_files_mod_name
        if not mod_name or mod_name in (_OVERWRITE_NAME, "Root_Folder"):
            return
        if self._staging_root is None:
            return
        mod_dir = self._staging_root / mod_name
        if not mod_dir.is_dir():
            return

        # Same gate as pack: deleting the archive + plugin from the mod
        # folder while the profile is deployed leaves stale hard-links
        # in game_root that the next Restore would misroute to overwrite.
        kind = self._archive_kind_for_current_game()
        kind_upper = kind.upper() if kind else "Archive"
        if self._is_current_profile_deployed():
            CTkAlert(
                state="warning", title=f"Unpack {kind_upper}",
                body_text=(
                    "This profile is currently deployed.\n\n"
                    f"Run Restore first, then unpack the {kind_upper}."
                ),
                btn1="OK", btn2="", parent=self.winfo_toplevel(),
            )
            return

        # Close any prior overlay first.
        self._close_bsa_unpack_overlay()

        overlay = BsaUnpackOverlay(
            self,
            mod_name=mod_name,
            mod_dir=mod_dir,
            on_unpack=self._do_unpack_bsa,
            on_close=self._close_bsa_unpack_overlay,
        )
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._bsa_unpack_overlay = overlay

    def _close_bsa_unpack_overlay(self) -> None:
        overlay = getattr(self, "_bsa_unpack_overlay", None)
        if overlay is not None:
            try:
                overlay.destroy()
            except Exception:
                pass
            self._bsa_unpack_overlay = None

    def _do_unpack_bsa(self, archive_paths: list[Path]) -> None:
        """Extract every archive in *archive_paths* into the mod folder
        as a single operation, then delete each archive, remove the
        same-stem stub plugin if it's one we generated, clear
        ``excluded_mod_files`` for the union of unpacked rel_keys,
        rebuild the filemap and refresh the Mod Files tab.

        *archive_paths* always shares a single plugin stem (the overlay
        groups by it); there's only one stub plugin to consider per
        invocation.

        Runs on a background thread with one shared progress popup so a
        multi-archive plugin (e.g. ``- Main.ba2`` + ``- Textures.ba2``)
        looks like one operation to the user."""
        import threading
        from Utils.ba2_extract import Ba2ExtractError, extract_ba2
        from Utils.bsa_extract import BsaExtractError, extract_bsa
        from Utils.bsa_writer import is_our_stub_plugin
        from gui.ctk_components import CTkAlert, CTkProgressPopup

        if not archive_paths:
            return
        mod_name = self._mod_files_mod_name
        if not mod_name or self._staging_root is None:
            return
        mod_dir = self._staging_root / mod_name
        if not mod_dir.is_dir():
            return

        self._close_bsa_unpack_overlay()

        # Determine the plugin stem these archives share, by stripping
        # the FO4 ` - Main` / ` - Textures` sidecar suffixes.  All paths
        # in *archive_paths* should resolve to the same stem (the
        # overlay grouped them); we trust the first one.
        first = archive_paths[0]
        archive_stem = first.stem
        for suffix in (" - Main", " - Textures"):
            if archive_stem.endswith(suffix):
                archive_stem = archive_stem[: -len(suffix)]
                break

        # Determine whether this is a BSA or BA2 group for dialog text.
        suffixes = {p.suffix.lower() for p in archive_paths}
        if suffixes == {".ba2"}:
            kind_upper = "BA2"
        elif suffixes == {".bsa"}:
            kind_upper = "BSA"
        else:
            kind_upper = "Archive"

        popup_label = (
            f"Unpacking {first.name}…" if len(archive_paths) == 1
            else f"Unpacking {len(archive_paths)} archives for {archive_stem}…"
        )
        popup = CTkProgressPopup(
            self.winfo_toplevel(),
            title=f"Unpack {kind_upper}",
            label=popup_label,
            message="Reading archive…",
        )

        def _on_done(
            result: tuple[int, list[str]] | None,
            error: str | None,
        ) -> None:
            if popup.winfo_exists():
                popup.close_progress_popup()
            if error is not None:
                CTkAlert(
                    state="warning", title=f"Unpack {kind_upper} failed",
                    body_text=error, btn1="OK", btn2="",
                    parent=self.winfo_toplevel(),
                )
                return
            assert result is not None
            total_count, all_written_rels = result

            # Delete every archive we successfully extracted.
            for ap in archive_paths:
                try:
                    ap.unlink()
                except OSError as exc:
                    self._log(
                        f"Unpack {kind_upper}: could not delete {ap.name}: {exc}"
                    )

            # Delete the shared stub plugin if it's one we generated
            # (don't touch a real authored plugin).
            stub = mod_dir / f"{archive_stem}.esp"
            stub_msg = ""
            if stub.is_file() and is_our_stub_plugin(stub):
                try:
                    stub.unlink()
                    stub_msg = f"\n\nRemoved generated stub {stub.name}."
                except OSError as exc:
                    self._log(
                        f"Unpack {kind_upper}: could not delete {stub.name}: {exc}"
                    )
            elif stub.is_file():
                stub_msg = (
                    f"\n\nLeft {stub.name} in place — it doesn't look like "
                    "one of our generated stubs."
                )

            # Clear those rel_keys from excluded_mod_files so the freshly
            # unpacked loose files show as enabled in the Mod Files tab.
            self._clear_excluded_for_unpack(mod_name, all_written_rels)

            # Rebuild filemap so deploy picks up the new loose files
            # (and forgets the now-deleted archives).
            mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
            if mod_panel is not None:
                try:
                    mod_panel._filemap_rescan_index = True
                    mod_panel._rebuild_filemap()
                except Exception:
                    pass

            try:
                self.show_mod_files(mod_name)
            except Exception:
                pass

            archives_label = (
                archive_paths[0].name if len(archive_paths) == 1
                else f"{len(archive_paths)} archives"
            )
            CTkAlert(
                state="info", title=f"Unpack {kind_upper}",
                body_text=(
                    f"Unpacked {total_count} file(s) from {archives_label} "
                    f"into the mod folder." + stub_msg
                ),
                btn1="OK", btn2="",
                parent=self.winfo_toplevel(),
            )

        def _worker() -> None:
            def _cancel() -> bool:
                return bool(getattr(popup, "cancelled", False))

            total_count = 0
            all_written: list[str] = []

            try:
                # Loop through every archive in the group.  Progress for
                # each is independent (per-archive %, with the popup
                # label updated to show which one is in flight).
                for i, ap in enumerate(archive_paths):
                    if _cancel():
                        self.after(0, lambda: popup.close_progress_popup() if popup.winfo_exists() else None)
                        return
                    label_text = (
                        f"Unpacking {ap.name}"
                        if len(archive_paths) == 1
                        else f"Unpacking {ap.name} ({i + 1} / {len(archive_paths)})"
                    )
                    self.after(0, lambda lbl=label_text: popup.update_label(lbl) if popup.winfo_exists() else None)

                    def _progress(done: int, total: int, current: str,
                                  _ap=ap) -> None:
                        def _ui() -> None:
                            if not popup.winfo_exists():
                                return
                            popup.update_progress(done / max(total, 1))
                            popup.update_message(
                                f"{done} / {total}  —  {current[-50:]}"
                            )
                        try:
                            self.after(0, _ui)
                        except Exception:
                            pass

                    is_ba2 = ap.suffix.lower() == ".ba2"
                    if is_ba2:
                        count, written = extract_ba2(
                            ap, mod_dir,
                            overwrite=True,
                            progress=_progress,
                            cancel=_cancel,
                        )
                    else:
                        count, written = extract_bsa(
                            ap, mod_dir,
                            overwrite=True,
                            progress=_progress,
                            cancel=_cancel,
                        )
                    total_count += count
                    all_written.extend(written)
            except (BsaExtractError, Ba2ExtractError) as exc:
                msg = str(exc)
                if "cancel" in msg.lower():
                    self.after(
                        0,
                        lambda: popup.close_progress_popup()
                        if popup.winfo_exists() else None,
                    )
                    return
                self.after(0, lambda m=msg: _on_done(None, m))
                return
            except Exception as exc:
                self.after(0, lambda m=str(exc): _on_done(None, m))
                return
            self.after(0, lambda r=(total_count, all_written): _on_done(r, None))

        threading.Thread(target=_worker, daemon=True).start()

    def _clear_excluded_for_unpack(
        self, mod_name: str, unpacked_rel_keys: list[str],
    ) -> None:
        """Remove *unpacked_rel_keys* from this mod's excluded_mod_files
        entry, so files that were auto-disabled by a previous Pack BSA
        come back as enabled in the Mod Files tab.

        No-op for pseudo-mods or when profile_state isn't wired up."""
        if not unpacked_rel_keys:
            return
        if mod_name in (_OVERWRITE_NAME, "Root_Folder"):
            return
        if self._mod_files_profile_dir is None:
            return
        all_excluded = read_excluded_mod_files(self._mod_files_profile_dir, None)
        current = set(all_excluded.get(mod_name, ()))
        if not current:
            return
        new_set = current - set(unpacked_rel_keys)
        if new_set == current:
            return  # nothing to clear
        if new_set:
            all_excluded[mod_name] = sorted(new_set)
        else:
            all_excluded.pop(mod_name, None)
        write_excluded_mod_files(self._mod_files_profile_dir, all_excluded)
        self._mod_files_excluded = {k: set(v) for k, v in all_excluded.items()}
        self._log(
            f"Unpack BSA: re-enabled {len(current) - len(new_set)} file(s) in "
            f"'{mod_name}' (now {len(new_set)} excluded)"
        )

    def _delete_loose_files(
        self, mod_dir: Path, packed_rel_keys: list[str],
    ) -> int:
        """Delete every file referenced by *packed_rel_keys* from *mod_dir*
        and remove any folders that become empty as a result.

        rel_keys are lowercase forward-slash, but on case-sensitive
        filesystems (Linux) the on-disk path can have any casing — mods
        ship with folders like ``Scripts/`` and ``Interface/``.  We
        resolve each rel_key one path segment at a time against the
        actual directory listing so wrong-cased *parent directories*
        are matched too, not just wrong-cased filenames.

        A per-directory listing cache keeps this O(files + dirs) rather
        than O(files × depth × siblings).

        Returns the number of files actually deleted."""
        if not packed_rel_keys:
            return 0

        # Cache of dir → {lowercase_name: actual_name} for case-insensitive
        # segment lookups.  Populated lazily.
        listing_cache: dict[Path, dict[str, str]] = {}

        def _list_lower(d: Path) -> dict[str, str]:
            cached = listing_cache.get(d)
            if cached is not None:
                return cached
            mapping: dict[str, str] = {}
            try:
                for entry in d.iterdir():
                    mapping[entry.name.lower()] = entry.name
            except OSError:
                pass
            listing_cache[d] = mapping
            return mapping

        def _resolve_ci(rel: str) -> Path | None:
            """Walk *rel* segment-by-segment from mod_dir, matching each
            segment case-insensitively against the real directory listing.
            Returns the actual on-disk Path or None if any segment can't
            be matched."""
            cur = mod_dir
            segments = rel.split("/")
            for i, seg in enumerate(segments):
                names = _list_lower(cur)
                actual = names.get(seg.lower())
                if actual is None:
                    return None
                cur = cur / actual
                # Last segment is the file; intermediates must be dirs.
                if i < len(segments) - 1 and not cur.is_dir():
                    return None
            return cur

        deleted = 0
        empty_candidate_dirs: set[Path] = set()
        for rel in packed_rel_keys:
            # Fast path first — exact lowercase match (works on
            # case-insensitive filesystems and on already-lowercase mods).
            target = mod_dir / rel
            if not target.is_file():
                resolved = _resolve_ci(rel)
                if resolved is None or not resolved.is_file():
                    continue
                target = resolved
            try:
                target.unlink()
                deleted += 1
                empty_candidate_dirs.add(target.parent)
                # Invalidate parent's listing cache so the empty-dir
                # walk below sees an accurate post-delete view.
                listing_cache.pop(target.parent, None)
            except OSError as exc:
                self._log(f"Pack BSA: could not delete {target}: {exc}")
        # Remove any folders we may have just emptied, walking up to the
        # mod root (but never deleting the mod root itself).
        for d in sorted(empty_candidate_dirs, key=lambda p: -len(p.parts)):
            cur = d
            while cur != mod_dir and cur.is_dir():
                try:
                    next(cur.iterdir())
                    break  # not empty
                except StopIteration:
                    try:
                        cur.rmdir()
                    except OSError:
                        break
                    cur = cur.parent
                except OSError:
                    break
        if deleted:
            self._log(
                f"Pack BSA: deleted {deleted} loose file(s) from "
                f"'{mod_dir.name}' after packing"
            )
        return deleted

    def _auto_disable_packed_files(
        self, mod_name: str, packed_rel_keys: list[str],
    ) -> None:
        """Add *packed_rel_keys* to this mod's excluded_mod_files entry so
        every loose file that was just packed is hidden from deploy.
        Existing exclusions are preserved (union, not replace).

        No-op for the overwrite / Root_Folder pseudo-mods, or when the
        profile_state path isn't wired up."""
        if not packed_rel_keys:
            return
        if mod_name in (_OVERWRITE_NAME, "Root_Folder"):
            return
        if self._mod_files_profile_dir is None:
            return
        all_excluded = read_excluded_mod_files(self._mod_files_profile_dir, None)
        merged = set(all_excluded.get(mod_name, ())) | set(packed_rel_keys)
        all_excluded[mod_name] = sorted(merged)
        write_excluded_mod_files(self._mod_files_profile_dir, all_excluded)
        self._mod_files_excluded = {k: set(v) for k, v in all_excluded.items()}
        self._log(
            f"Pack BSA: auto-disabled {len(packed_rel_keys)} file(s) in '{mod_name}' "
            f"(now {len(merged)} total excluded)"
        )

    def _get_conflict_cache(self, full_index):
        """Return (contested_keys, filemap_winner), cached by index+filemap mtime.

        Pass ``full_index`` if the caller already has it loaded; otherwise
        ``None`` and it will be read on a cache miss.  Both call sites
        (Mod Files and Data tabs) share the same cache.
        """
        idx_path = self._mod_files_index_path
        if idx_path is None:
            return set(), {}
        fm_path = idx_path.parent / "filemap.txt"
        try:
            idx_mtime = idx_path.stat().st_mtime if idx_path.is_file() else 0.0
        except OSError:
            idx_mtime = 0.0
        try:
            fm_mtime = fm_path.stat().st_mtime if fm_path.is_file() else 0.0
        except OSError:
            fm_mtime = 0.0
        ml_path = (self._mod_files_profile_dir / "modlist.txt"
                   if self._mod_files_profile_dir is not None else None)
        try:
            ml_mtime = ml_path.stat().st_mtime if ml_path and ml_path.is_file() else 0.0
        except OSError:
            ml_mtime = 0.0
        sig = (str(idx_path), idx_mtime, fm_mtime, ml_mtime)
        cached = getattr(self, "_conflict_cache", None)
        if cached is not None and cached[0] == sig:
            return cached[1], cached[2]

        filemap_winner: dict[str, str] = {}
        if fm_path.is_file():
            try:
                for _line in fm_path.read_text(encoding="utf-8").splitlines():
                    if "\t" in _line:
                        _rk, _mn = _line.split("\t", 1)
                        filemap_winner[_rk.lower()] = _mn
            except Exception:
                pass

        contested_keys: set[str] = set()
        if full_index is None:
            try:
                from Utils.filemap import read_mod_index
                full_index = read_mod_index(idx_path)
            except Exception:
                full_index = None
        if full_index:
            # Disabled mods can't conflict — leave them out of the count so a
            # file shared only with a toggled-off mod isn't tagged as contested.
            disabled: set[str] = set()
            if ml_path is not None and ml_path.is_file():
                try:
                    from Utils.modlist import read_modlist
                    disabled = {e.name for e in read_modlist(ml_path)
                                if not e.is_separator and not e.enabled}
                except Exception:
                    disabled = set()
            _key_count: dict[str, int] = {}
            for _mn, (_norm, _root) in full_index.items():
                if _mn in disabled:
                    continue
                for _k in _norm:
                    _key_count[_k] = _key_count.get(_k, 0) + 1
                for _k in _root:
                    _key_count[_k] = _key_count.get(_k, 0) + 1
            contested_keys = {_k for _k, _c in _key_count.items() if _c > 1}

        self._conflict_cache = (sig, contested_keys, filemap_winner)
        return contested_keys, filemap_winner

    # ------------------------------------------------------------------
    # Right-click / open-in-browser helpers
    # ------------------------------------------------------------------

    def _show_simple_context_menu(self, anchor_widget, x: int, y: int, items: list):
        """Show a minimal context menu using a Toplevel (avoids tk.Menu dismiss bug on Linux).

        *items* is a list of (label, command) tuples.
        """
        popup = tk.Toplevel(anchor_widget)
        popup.wm_overrideredirect(True)
        popup.wm_geometry(f"+{x}+{y}")
        popup.configure(bg=BORDER)

        _alive = [True]

        def _dismiss(_event=None):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()

        def _pick(cmd):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()
                cmd()

        inner = tk.Frame(popup, bg=BG_PANEL, bd=0)
        inner.pack(padx=1, pady=1)

        for label, cmd in items:
            btn = tk.Label(
                inner, text=label, anchor="w",
                bg=BG_PANEL, fg=TEXT_MAIN,
                font=(_theme.FONT_FAMILY, _theme.FS11),
                padx=12, pady=5, cursor="hand2",
            )
            btn.pack(fill="x")
            btn.bind("<ButtonRelease-1>", lambda _e, c=cmd: _pick(c))
            btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=BG_SELECT))
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=BG_PANEL))

        popup.update_idletasks()
        popup.bind("<Escape>", _dismiss)

        def _on_press(event):
            if not _alive[0]:
                return
            wx, wy = popup.winfo_rootx(), popup.winfo_rooty()
            ww, wh = popup.winfo_width(), popup.winfo_height()
            if not (wx <= event.x_root <= wx + ww and wy <= event.y_root <= wy + wh):
                _dismiss()
        popup.bind_all("<ButtonPress-1>", _on_press)
        popup.bind_all("<ButtonPress-3>", _on_press)

    def _get_staging_path(self) -> "Path | None":
        """Return the mod staging directory, or None if not available."""
        if self._game is not None and hasattr(self._game, "get_effective_mod_staging_path"):
            try:
                p = self._game.get_effective_mod_staging_path()
                return Path(p) if p else None
            except Exception:
                pass
        if self._mod_files_index_path is not None:
            return self._mod_files_index_path.parent
        return None

    def _open_folder_in_browser(self, path: "Path"):
        """Open *path* (a directory) in the system file browser, creating it if needed."""
        try:
            path = Path(path)
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
            xdg_open(path)
        except Exception as e:
            self._log(f"Could not open folder: {e}")

    def _build_downloads_tab(self):
        from gui.downloads_panel import build_downloads_tab
        build_downloads_tab(self, self._tabs.tab("Downloads"))

    def _on_open_download_locations(self):
        from gui.downloads_panel import _open_download_locations_overlay
        _open_download_locations_overlay(self)

    # ------------------------------------------------------------------
    # Plugin filter side panel — open / close
    # ------------------------------------------------------------------

    def _toggle_plugin_filter_panel(self):
        """Toggle the plugin filter side panel open/closed."""
        if self._plugin_filter_panel_open:
            self._close_plugin_filter_panel()
        else:
            self._open_plugin_filter_panel()

    def _open_plugin_filter_panel(self):
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        if mod_panel is None:
            return
        # Close modlist/data filter if open (they share the same column)
        if getattr(mod_panel, "_filter_panel_open", False):
            mod_panel._close_filter_side_panel()
        if getattr(self, "_data_filter_panel_open", False):
            self._close_data_filter_panel()
        if getattr(self, "_ini_filter_panel_open", False):
            self._close_ini_filter_panel()
        if getattr(self, "_mf_filter_panel_open", False):
            self._close_mf_filter_panel()
        self._plugin_filter_panel_open = True
        mod_panel.grid_columnconfigure(0, minsize=scaled(380))
        self._plugin_filter_side_panel.grid()
        # Sync checkbox vars to current live filter state
        for key, cb in self._pfsp_checkboxes.items():
            cb.set_state(int(self._plugin_filter_state.get(key, 0) or 0))
        self._bind_plugin_filter_panel_scroll()
        self._update_plugin_filter_btn_color()

    def _close_plugin_filter_panel(self):
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        self._plugin_filter_panel_open = False
        if mod_panel is not None:
            mod_panel.grid_columnconfigure(0, minsize=0)
        self._plugin_filter_side_panel.grid_remove()
        self._update_plugin_filter_btn_color()

    def _update_plugin_filter_btn_color(self) -> None:
        btn = getattr(self, "_plugin_filter_btn", None)
        if btn is None:
            return
        any_active = any(self._plugin_filter_state.values()) if self._plugin_filter_state else False
        btn.configure(fg_color=ACCENT if any_active else BTN_INFO)

    def _close_download_locations_overlay(self):
        from gui.downloads_panel import _close_download_locations_overlay
        _close_download_locations_overlay(self)

    def _install_from_downloads(self, archive_path: str):
        from gui.downloads_panel import _install_from_downloads
        _install_from_downloads(self, archive_path)

    def _build_plugins_tab(self):
        tab = self._tabs.tab("Plugins")
        tab.grid_rowconfigure(2, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        # Use design size 28: CTk applies its own widget scaling, so scaled() would double-scale
        self._pheader = ctk.CTkFrame(tab, fg_color=BG_HEADER, corner_radius=0, height=28)
        self._pheader.grid(row=0, column=0, sticky="ew")
        self._pheader.grid_propagate(False)
        self._pheader_labels: list[ctk.CTkLabel] = []

        # Framework status banners (populated by _refresh_framework_banners)
        self._framework_banners_frame = ctk.CTkFrame(
            tab, fg_color=BG_PANEL, corner_radius=0
        )
        self._framework_banners_frame.grid(row=1, column=0, sticky="ew")
        self._framework_banners_frame.grid_columnconfigure(0, weight=1)

        canvas_frame = tk.Frame(tab, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_frame.grid(row=2, column=0, sticky="nsew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        self._pcanvas = tk.Canvas(canvas_frame, bg=BG_DEEP, bd=0,
                                  highlightthickness=0, yscrollincrement=1, takefocus=0)
        # Custom combined scrollbar + marker strip — see modlist_panel._build_canvas
        # for the rationale.
        self._PSCROLL_W = 16
        self._pmarker_strip = tk.Canvas(canvas_frame, bg=BG_DEEP, bd=0, highlightthickness=0,
                                        width=self._PSCROLL_W, takefocus=0)
        self._pvsb = self._pmarker_strip  # alias for any external refs
        self._pcanvas.configure(yscrollcommand=self._pscroll_set)
        self._pcanvas.grid(row=0, column=0, sticky="nsew")
        self._pmarker_strip.grid(row=0, column=1, sticky="ns")
        self._pscroll_first = 0.0
        self._pscroll_last = 1.0
        self._pthumb_drag_offset: float | None = None
        self._pmarker_strip.bind("<Configure>",      self._on_pmarker_strip_resize)
        self._pmarker_strip.bind("<ButtonPress-1>",  self._on_pscrollbar_press)
        self._pmarker_strip.bind("<B1-Motion>",      self._on_pscrollbar_drag)
        self._pmarker_strip.bind("<ButtonRelease-1>",self._on_pscrollbar_release)
        self._pmarker_strip.bind("<Button-4>",       self._on_pscroll_up)
        self._pmarker_strip.bind("<Button-5>",       self._on_pscroll_down)
        self._pmarker_strip.bind("<MouseWheel>",     self._on_pmousewheel)

        self._pcanvas.bind("<Configure>",       self._on_pcanvas_resize)
        self._pcanvas.bind("<Button-4>",        self._on_pscroll_up)
        self._pcanvas.bind("<Button-5>",        self._on_pscroll_down)
        self._pcanvas.bind("<MouseWheel>",      self._on_pmousewheel)
        self._pcanvas.bind("<ButtonPress-1>",   self._on_pmouse_press)
        self._pcanvas.bind("<B1-Motion>",       self._on_pmouse_drag)
        self._pcanvas.bind("<ButtonRelease-1>", self._on_pmouse_release)
        self._pcanvas.bind("<Motion>",          self._on_pmouse_motion)
        self._pcanvas.bind("<Leave>",           self._on_pmouse_leave)
        self._pcanvas.bind("<ButtonRelease-3>", self._on_plugin_right_click)

        toolbar = ctk.CTkFrame(tab, fg_color=BG_PANEL, corner_radius=0, height=58)
        toolbar.grid(row=3, column=0, sticky="ew")
        toolbar.grid_propagate(False)
        self._loot_toolbar = toolbar

        # Row 1: action buttons
        btn_row = tk.Frame(toolbar, bg=BG_PANEL)
        btn_row.pack(side="top", fill="x", padx=0, pady=(4, 0))

        ctk.CTkButton(
            btn_row, text="Sort Plugins", width=110, height=30,
            fg_color=BTN_SUCCESS_ALT, hover_color=BTN_SUCCESS_ALT_HOV,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._sort_plugins_loot,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            btn_row, text="Groups", width=80, height=30,
            fg_color=BTN_INFO, hover_color=BTN_INFO_HOV,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._open_loot_groups_overlay,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_row, text="Plugin Rules", width=100, height=30,
            fg_color=BTN_INFO, hover_color=BTN_INFO_HOV,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._open_loot_plugin_rules_overlay,
        ).pack(side="left", padx=(0, 8))

        self._plugin_filter_btn = ctk.CTkButton(
            btn_row, text="Filters", width=80, height=30,
            fg_color=BTN_INFO, hover_color=BTN_INFO_HOV,
            text_color=TEXT_MAIN, font=_theme.FONT_SMALL,
            command=self._toggle_plugin_filter_panel,
        )
        self._plugin_filter_btn.pack(side="left", padx=(0, 8))

        # Row 2: active plugin counter
        counter_row = tk.Frame(toolbar, bg=BG_PANEL)
        counter_row.pack(side="top", fill="x", padx=0, pady=(2, 0))

        self._plugin_counter_label = ctk.CTkLabel(
            counter_row, text="", font=_theme.FONT_SMALL, text_color=TEXT_MAIN,
        )
        self._plugin_counter_label.pack(side="left", padx=8)

        self._plugin_esl_counter_label = ctk.CTkLabel(
            counter_row, text="", font=_theme.FONT_SMALL, text_color=TEXT_MAIN,
        )
        self._plugin_esl_counter_label.pack(side="left", padx=(0, 8))

        self._plugin_non_esl_counter_label = ctk.CTkLabel(
            counter_row, text="", font=_theme.FONT_SMALL, text_color=TEXT_MAIN,
        )
        self._plugin_non_esl_counter_label.pack(side="left", padx=(0, 8))

        # Search bar
        search_bar = tk.Frame(tab, bg=BG_HEADER, highlightthickness=0)
        search_bar.grid(row=4, column=0, sticky="ew")
        tk.Label(
            search_bar, text="Search:", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, _theme.FS10),
        ).pack(side="left", padx=(8, 4), pady=3)
        self._plugin_search_var = tk.StringVar()
        self._plugin_search_var.trace_add("write", self._on_plugin_search_changed)
        self._plugin_search_entry = tk.Entry(
            search_bar, textvariable=self._plugin_search_var,
            bg=BG_DEEP, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=(_theme.FONT_FAMILY, _theme.FS10),
            highlightthickness=0, highlightbackground=BG_DEEP,
        )
        self._plugin_search_entry.pack(side="left", padx=(0, 8), pady=3, fill="x", expand=True)
        self._plugin_search_entry.bind("<Escape>", lambda e: self._plugin_search_var.set(""))
        def _psearch_select_all(evt):
            evt.widget.select_range(0, tk.END)
            evt.widget.icursor(tk.END)
            return "break"
        self._plugin_search_entry.bind("<Control-a>", _psearch_select_all)

        # Userlist inline panel (hidden until triggered)
        self._userlist_panel_plugin: str = ""
        self._userlist_panel_idx: int = -1
        ul_panel = tk.Frame(tab, bg=BG_HEADER, highlightthickness=0)
        ul_panel.grid(row=5, column=0, sticky="ew")
        ul_panel.grid_remove()  # hidden by default
        self._ul_panel = ul_panel

        tk.Frame(ul_panel, bg=BORDER, height=1).pack(side="top", fill="x")

        ul_inner = tk.Frame(ul_panel, bg=BG_HEADER)
        ul_inner.pack(fill="x", padx=8, pady=(6, 2))
        ul_inner.grid_columnconfigure(1, weight=1)

        _lkw = dict(bg=BG_HEADER, fg=TEXT_DIM, font=(_theme.FONT_FAMILY, _theme.FS10))
        _ekw = dict(bg=BG_DEEP, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
                    relief="flat", font=(_theme.FONT_FAMILY, _theme.FS10),
                    highlightthickness=1, highlightbackground=BORDER)

        tk.Label(ul_inner, text="After:", **_lkw).grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        self._ul_after_var = tk.StringVar()
        tk.Entry(ul_inner, textvariable=self._ul_after_var, **_ekw).grid(
            row=0, column=1, sticky="ew", pady=2)

        tk.Label(ul_inner, text="Before:", **_lkw).grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
        self._ul_before_var = tk.StringVar()
        tk.Entry(ul_inner, textvariable=self._ul_before_var, **_ekw).grid(
            row=1, column=1, sticky="ew", pady=2)

        tk.Label(ul_inner, text="Separate multiple plugins with  |", **_lkw).grid(
            row=2, column=1, sticky="w", pady=(0, 2))

        btn_frame = tk.Frame(ul_panel, bg=BG_HEADER)
        btn_frame.pack(fill="x", padx=8, pady=(2, 6))

        self._ul_name_label = tk.Label(
            btn_frame, text="", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, _theme.FS10), anchor="w",
        )
        self._ul_name_label.pack(side="left")

        ctk.CTkButton(
            btn_frame, text="Cancel", width=70, height=24,
            font=_theme.FONT_SMALL, fg_color=BG_DEEP,
            hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._ul_cancel,
        ).pack(side="right", padx=(4, 0))
        ctk.CTkButton(
            btn_frame, text="Save", width=70, height=24,
            font=_theme.FONT_SMALL, fg_color=ACCENT,
            hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._ul_save,
        ).pack(side="right")

        # Group assignment inline panel (hidden until triggered)
        self._group_panel_plugins: list[str] = []
        grp_panel = tk.Frame(tab, bg=BG_HEADER, highlightthickness=0)
        grp_panel.grid(row=6, column=0, sticky="ew")
        grp_panel.grid_remove()
        self._grp_panel = grp_panel

        tk.Frame(grp_panel, bg=BORDER, height=1).pack(side="top", fill="x")

        grp_inner = tk.Frame(grp_panel, bg=BG_HEADER)
        grp_inner.pack(fill="x", padx=8, pady=(6, 2))
        grp_inner.grid_columnconfigure(1, weight=1)

        tk.Label(grp_inner, text="Group:", **_lkw).grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        self._grp_var = tk.StringVar(value="default")
        self._grp_menu = ctk.CTkOptionMenu(
            grp_inner, variable=self._grp_var, values=["default"],
            font=_theme.FONT_SMALL,
            fg_color=BG_DEEP, button_color=ACCENT, button_hover_color=ACCENT_HOV,
            dropdown_fg_color=BG_PANEL, dropdown_text_color=TEXT_MAIN,
            text_color=TEXT_MAIN, height=26,
        )
        self._grp_menu.grid(row=0, column=1, sticky="ew", pady=2)

        grp_btn_frame = tk.Frame(grp_panel, bg=BG_HEADER)
        grp_btn_frame.pack(fill="x", padx=8, pady=(2, 6))

        self._grp_name_label = tk.Label(
            grp_btn_frame, text="", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, _theme.FS10), anchor="w",
        )
        self._grp_name_label.pack(side="left")

        ctk.CTkButton(
            grp_btn_frame, text="Cancel", width=70, height=24,
            font=_theme.FONT_SMALL, fg_color=BG_DEEP,
            hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._grp_cancel,
        ).pack(side="right", padx=(4, 0))
        ctk.CTkButton(
            grp_btn_frame, text="Save", width=70, height=24,
            font=_theme.FONT_SMALL, fg_color=ACCENT,
            hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._grp_save,
        ).pack(side="right")

        self._build_plugin_filter_side_panel()
        self._create_pool()

    # ------------------------------------------------------------------
    # Plugin filter side panel
    # ------------------------------------------------------------------

    def _build_plugin_filter_side_panel(self) -> None:
        """Build the inline filter side panel as a child of ModListPanel at column 0."""
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        parent = mod_panel if mod_panel is not None else self
        panel = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=0, width=380)
        panel.grid(row=0, column=0, rowspan=5, sticky="nsew")
        panel.grid_propagate(False)
        panel.grid_remove()  # hidden by default
        self._plugin_filter_side_panel = panel

        # Header row
        header = tk.Frame(panel, bg=BG_HEADER, height=scaled(36))
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(
            header, text="Plugin Filters", bg=BG_HEADER, fg=TEXT_MAIN,
            font=_theme.TK_FONT_BOLD, anchor="w",
        ).pack(side="left", padx=10, pady=6)

        close_btn = tk.Label(
            header, text="\u00d7", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, 16, "bold"), cursor="hand2",
        )
        close_btn.pack(side="right", padx=8)
        close_btn.bind("<Button-1>", lambda _e: self._close_plugin_filter_panel())
        close_btn.bind("<Enter>",    lambda _e: close_btn.configure(fg=TEXT_MAIN))
        close_btn.bind("<Leave>",    lambda _e: close_btn.configure(fg=TEXT_DIM))

        clear_btn = tk.Label(
            header, text="Clear all", bg=BG_HEADER, fg=TEXT_DIM,
            font=_theme.TK_FONT_SMALL, cursor="hand2",
        )
        clear_btn.pack(side="right", padx=(0, 4))
        clear_btn.bind("<Button-1>", lambda _e: self._clear_all_plugin_filters())
        clear_btn.bind("<Enter>",    lambda _e: clear_btn.configure(fg=TEXT_MAIN))
        clear_btn.bind("<Leave>",    lambda _e: clear_btn.configure(fg=TEXT_DIM))

        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x")

        scroll_frame = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0,
        )
        scroll_frame.pack(fill="both", expand=True, padx=8, pady=6)

        opts = [
            ("filter_enabled",         "Enabled plugins"),
            ("filter_disabled",        "Disabled plugins"),
            ("filter_missing_masters", "Plugins with missing masters"),
            ("filter_esl_ext",         "ESL plugins (.esl extension)"),
            ("filter_esm_ext",         "ESM plugins (.esm extension)"),
            ("filter_esp_ext",         "ESP plugins (.esp extension)"),
            ("filter_esl_flagged",     "ESL-flagged (light) plugins"),
            ("filter_esl_not_flagged", "Plugins not flagged as ESL"),
            ("filter_esl_safe",        "ESL-safe plugins"),
            ("filter_esl_unsafe",      "ESL-unsafe plugins"),
            ("filter_userlist",        "Plugins managed by userlist.yaml"),
            ("filter_dirty",           "Dirty plugins (need cleaning)"),
            ("filter_bash_tags",       "Bash-tagged plugins"),
            ("filter_bos_sp",          "BOS/SP-patched plugins [B/S badge]"),
            ("filter_bos_only",        "BOS-patched plugins [B badge]"),
            ("filter_sp_only",         "SkyPatcher-patched plugins [S badge]"),
        ]

        from gui.tri_state_checkbox import TriStateCheckBox
        self._pfsp_vars: dict[str, tk.IntVar] = {}
        self._pfsp_checkboxes: dict[str, TriStateCheckBox] = {}
        for key, label in opts:
            var = tk.IntVar(value=0)
            self._pfsp_vars[key] = var
            cb = TriStateCheckBox(
                scroll_frame,
                text=label,
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_plugin_filter_panel_change,
            )
            cb.pack(anchor="w", fill="x", pady=3)
            self._pfsp_checkboxes[key] = cb

        self._plugin_filter_scroll_frame = scroll_frame
        self._bind_plugin_filter_panel_scroll()

    def _bind_plugin_filter_panel_scroll(self) -> None:
        scroll_frame = getattr(self, "_plugin_filter_scroll_frame", None)
        if not scroll_frame or not hasattr(scroll_frame, "_parent_canvas"):
            return
        def _on_wheel(evt):
            num = getattr(evt, "num", None)
            delta = getattr(evt, "delta", 0) or 0
            if num == 4 or delta > 0:
                scroll_frame._parent_canvas.yview_scroll(-3, "units")
            elif num == 5 or delta < 0:
                scroll_frame._parent_canvas.yview_scroll(3, "units")
        def _bind_recursive(w):
            # On Tk >= 8.7 CTkScrollableFrame handles <MouseWheel> via bind_all;
            # only supplement Button-4/5 for Tk 8.6.
            if not LEGACY_WHEEL_REDUNDANT:
                w.bind("<Button-4>", _on_wheel)
                w.bind("<Button-5>", _on_wheel)
            for child in w.winfo_children():
                _bind_recursive(child)
        _bind_recursive(scroll_frame)

    def _clear_all_plugin_filters(self) -> None:
        for cb in self._pfsp_checkboxes.values():
            cb.set_state(0)
        self._on_plugin_filter_panel_change()

    def _on_plugin_filter_panel_change(self) -> None:
        state = {k: v.get() for k, v in self._pfsp_vars.items()}
        self._plugin_filter_state = state
        self._update_plugin_filter_btn_color()
        self._apply_plugin_search_filter()
        self._pcanvas.yview_moveto(0)
        self._predraw()

    # ------------------------------------------------------------------
    # Virtual-list pool
    # ------------------------------------------------------------------

    def _create_pool(self) -> None:
        """Pre-allocate a fixed set of canvas items and checkbutton widgets."""
        for s in range(self._pool_size):
            self._create_pool_slot(s)

    def _ensure_pool_capacity(self, needed: int) -> None:
        """Grow the slot pool when the viewport holds more rows than slots."""
        if needed <= self._pool_size:
            return
        for s in range(self._pool_size, needed):
            self._create_pool_slot(s)
        self._pool_size = needed

    def _create_pool_slot(self, s: int) -> None:
        c = self._pcanvas
        self._pool_data_idx.append(-1)
        self._pool_last_state.append(None)

        bg_id = c.create_rectangle(0, -200, 0, -200, fill="", outline="", state="hidden")
        missing_strip_id = c.create_rectangle(0, -200, 3, -200,
                                               fill=BTN_CANCEL, outline="", state="hidden")
        name_id = c.create_text(0, -200, text="", anchor="w", fill="",
                                font=(_theme.FONT_FAMILY, _theme.FS11), state="hidden")
        idx_id = c.create_text(0, -200, text="", anchor="center", fill="",
                               font=(_theme.FONT_FAMILY, _theme.FS10), state="hidden")
        warn_id: int | None = None
        if self._warning_icon:
            warn_id = c.create_image(0, -200, image=self._warning_icon,
                                     anchor="center", state="hidden")

        late_warn_id: int | None = None
        if self._late_warn_icon:
            late_warn_id = c.create_image(0, -200, image=self._late_warn_icon,
                                          anchor="center", state="hidden")

        self._pool_bg.append(bg_id)
        self._pool_missing_strip.append(missing_strip_id)
        self._pool_name.append(name_id)
        self._pool_idx_text.append(idx_id)
        vmm_warn_id: int | None = None
        if self._version_mismatch_icon:
            vmm_warn_id = c.create_image(0, -200, image=self._version_mismatch_icon,
                                         anchor="center", state="hidden")

        self._pool_warn.append(warn_id)
        self._pool_late_warn.append(late_warn_id)
        self._pool_vmm_warn.append(vmm_warn_id)

        cb_tag = f"pcb_{s}"
        cb_rect = c.create_rectangle(
            0, -200, 0, -200, outline=BORDER, width=1, state="hidden",
            tags=(cb_tag, "pcb"),
        )
        cb_mark = c.create_text(
            0, -200, text="✓", anchor="center", fill=ACCENT,
            font=(_theme.FONT_FAMILY, int(_theme.FS13 * 1.25), "bold"), state="hidden",
            tags=(cb_tag, "pcb"),
        )
        self._pool_check_rects.append(cb_rect)
        self._pool_check_marks.append(cb_mark)
        def _cb_release(e, slot=s):
            self._on_pool_check_toggle(slot)
            return "break"
        def _cb_enter(e):
            c.config(cursor="hand2")
        def _cb_leave(e):
            c.config(cursor="")
        c.tag_bind(cb_tag, "<ButtonRelease-1>", _cb_release)
        c.tag_bind(cb_tag, "<Enter>", _cb_enter)
        c.tag_bind(cb_tag, "<Leave>", _cb_leave)

        lk_tag = f"plk_{s}"
        lk_rect = c.create_rectangle(
            0, -200, 0, -200, outline=BORDER, width=1, state="hidden",
            tags=(lk_tag, "plk"),
        )
        if self._icon_lock:
            lk_mark = c.create_image(
                0, -200, anchor="center",
                image=self._icon_lock,
                state="hidden",
                tags=(lk_tag, "plk"),
            )
        else:
            lk_mark = c.create_text(
                0, -200, text="🔒", anchor="center", fill=TEXT_MAIN,
                font=(_theme.FONT_FAMILY, _theme.FS9), state="hidden",
                tags=(lk_tag, "plk"),
            )
        self._pool_lock_rects.append(lk_rect)
        self._pool_lock_marks.append(lk_mark)

        ul_dot = c.create_oval(0, -200, 0, -200,
                               fill="white", outline="", state="hidden")
        self._pool_ul_dot.append(ul_dot)

        esl_badge = c.create_text(0, -200, text="L", anchor="center",
                                  fill=TONE_CYAN,
                                  font=(_theme.FONT_FAMILY, _theme.FS11, "bold"),
                                  state="hidden")
        self._pool_esl_badge.append(esl_badge)

        loot_info_id: int | None = None
        if self._loot_info_icon:
            loot_info_id = c.create_image(0, -200, image=self._loot_info_icon,
                                          anchor="center", state="hidden")
        self._pool_loot_info.append(loot_info_id)

        loot_dirty_id: int | None = None
        if self._loot_dirty_icon:
            loot_dirty_id = c.create_image(0, -200, image=self._loot_dirty_icon,
                                           anchor="center", state="hidden")
        self._pool_loot_dirty.append(loot_dirty_id)

        loot_tag_id: int | None = None
        if self._loot_tag_icon:
            loot_tag_id = c.create_image(0, -200, image=self._loot_tag_icon,
                                         anchor="center", state="hidden")
        self._pool_loot_tag.append(loot_tag_id)

        bos_sp_badge = c.create_text(0, -200, text="", anchor="center",
                                     fill="#c084fc",
                                     font=(_theme.FONT_FAMILY, _theme.FS11, "bold"),
                                     state="hidden")
        self._pool_bos_sp_badge.append(bos_sp_badge)

        def _lk_release(e, slot=s):
            self._on_pool_lock_toggle(slot)
            return "break"
        c.tag_bind(lk_tag, "<ButtonRelease-1>", _lk_release)
        c.tag_bind(lk_tag, "<Enter>", _cb_enter)
        c.tag_bind(lk_tag, "<Leave>", _cb_leave)

    def _on_pool_check_toggle(self, slot: int) -> None:
        """A pooled enable-checkbox was clicked — map back to data row."""
        data_idx = self._pool_data_idx[slot] if slot < len(self._pool_data_idx) else -1
        if data_idx < 0 or data_idx >= len(self._plugin_entries):
            return
        entry = self._plugin_entries[data_idx]
        if entry.name.lower() in self._vanilla_plugins:
            return
        entry.enabled = not entry.enabled
        self._save_plugins()
        self._check_all_masters()
        self._predraw()

    def _on_pool_lock_toggle(self, slot: int) -> None:
        """A pooled lock-checkbox was clicked — map back to data row."""
        data_idx = self._pool_data_idx[slot] if slot < len(self._pool_data_idx) else -1
        if data_idx < 0 or data_idx >= len(self._plugin_entries):
            return
        name = self._plugin_entries[data_idx].name
        locked = not bool(self._plugin_locks.get(name, False))
        if locked:
            self._plugin_locks[name] = True
        else:
            self._plugin_locks.pop(name, None)
        self._save_plugin_locks()
        self._predraw()

    # ------------------------------------------------------------------
    # LOOT sorting
    # ------------------------------------------------------------------

    def _on_plugin_search_changed(self, *_) -> None:
        self._apply_plugin_search_filter()
        self._pcanvas.yview_moveto(0)
        self._predraw()

    def _apply_plugin_search_filter(self) -> None:
        query = ""
        if self._plugin_search_var is not None:
            query = self._plugin_search_var.get().strip().casefold()

        fs = self._plugin_filter_state
        any_filter = query or (fs and any(fs.values()))

        if not any_filter:
            self._plugin_filtered_indices = None
            return

        # Tri-state predicate helper: returns True if entry should be kept.
        # state 1 (include) keeps matches; state 2 (exclude) keeps non-matches.
        def _tri(state, matches: bool) -> bool:
            if not state:
                return True
            if state == 1:
                return matches
            return not matches

        missing_lower = {k.lower() for k in self._missing_masters.keys()}
        esl_flagged = self._esl_flagged_plugins
        esl_safe = self._esl_safe_plugins
        esl_unsafe = self._esl_unsafe_plugins
        userlist = self._userlist_plugins
        bos_sp = self._bos_sp_plugins

        result = []
        for i, entry in enumerate(self._plugin_entries):
            name_lower = entry.name.lower()

            # --- search query ---
            if query:
                name_match = query in name_lower
                mod_name = self._plugin_mod_map.get(name_lower, "")
                if not name_match and not (mod_name and query in mod_name.casefold()):
                    continue

            if not fs:
                result.append(i)
                continue

            checks = (
                (fs.get("filter_enabled"),         entry.enabled),
                (fs.get("filter_disabled"),        not entry.enabled),
                (fs.get("filter_missing_masters"), name_lower in missing_lower),
                (fs.get("filter_esl_ext"),         name_lower.endswith(".esl")),
                (fs.get("filter_esm_ext"),         name_lower.endswith(".esm")),
                (fs.get("filter_esp_ext"),         name_lower.endswith(".esp")),
                (fs.get("filter_esl_flagged"),     name_lower in esl_flagged),
                (fs.get("filter_esl_not_flagged"), name_lower not in esl_flagged),
                (fs.get("filter_esl_safe"),        name_lower in esl_safe),
                (fs.get("filter_esl_unsafe"),      name_lower in esl_unsafe),
                (fs.get("filter_userlist"),        name_lower in userlist),
                (fs.get("filter_dirty"),           self._has_dirty_edits(entry.name)),
                (fs.get("filter_bash_tags"),       self._has_bash_tags(entry.name)),
                (fs.get("filter_bos_sp"),          name_lower in bos_sp),
            )
            if not all(_tri(s, m) for s, m in checks):
                continue

            # BOS/SP have a union case when both are INCLUDE-checked, so keep
            # bespoke handling for those two flags.
            bos_state = fs.get("filter_bos_only") or 0
            sp_state = fs.get("filter_sp_only") or 0
            _bos_kind = bos_sp.get(name_lower, "")
            if bos_state == 1 and sp_state == 1:
                if not _bos_kind:
                    continue
            else:
                if bos_state and not _tri(bos_state, _bos_kind in ("bos", "both")):
                    continue
                if sp_state and not _tri(sp_state, _bos_kind in ("sp", "both")):
                    continue

            result.append(i)

        self._plugin_filtered_indices = result

    # ------------------------------------------------------------------
    # Plugin column layout
    # ------------------------------------------------------------------

    def _layout_plugin_cols(self, w: int):
        """Compute column x positions given the canvas width."""
        # col 0: checkbox   28px
        # col 1: name       fills
        # col 2: flags      50px
        # col 3: lock       28px
        # col 4: index      50px + 14px scrollbar gap
        idx_w = scaled(50) + scaled(14)
        lock_w = scaled(32)
        flags_w = scaled(110)
        cb_col_w = scaled(32)
        flags_x = max(scaled(80), w - idx_w - lock_w - flags_w)
        self._pcol_x = [scaled(4), scaled(4) + cb_col_w, flags_x, flags_x + flags_w, flags_x + flags_w + lock_w]

    def _update_plugin_header(self, w: int):
        """Update header labels to match current column positions; reuse existing labels to avoid flicker."""
        try:
            if not self._pheader.winfo_exists():
                return
        except Exception:
            return
        self._pheader.configure(width=w)

        col_x = self._pcol_x
        titles = self.PLUGIN_HEADERS
        widths = [col_x[1] - col_x[0],
                  col_x[2] - col_x[1],
                  col_x[3] - col_x[2],
                  col_x[4] - col_x[3],
                  w - col_x[4]]

        for i, (title, cw) in enumerate(zip(titles, widths)):
            anchor = "w" if i == 1 else "center"
            # Column 3 is the lock column — prefer the PNG over the emoji fallback
            use_img = (i == 3 and self._icon_lock is not None)
            if i < len(self._pheader_labels):
                lbl = self._pheader_labels[i]
                if use_img:
                    lbl.configure(text="", image=self._icon_lock)
                else:
                    lbl.configure(text=title)
                lbl.place(x=col_x[i], y=0, width=cw, height=scaled(28))
            else:
                lbl = tk.Label(
                    self._pheader, anchor=anchor,
                    font=(_theme.FONT_FAMILY, _theme.FS11, "bold"), fg=TEXT_SEP, bg=BG_HEADER,
                    **({"image": self._icon_lock, "text": ""} if use_img else {"text": title}),
                )
                lbl.place(x=col_x[i], y=0, width=cw, height=scaled(28))
                self._pheader_labels.append(lbl)

    # ------------------------------------------------------------------
    # Plugin lock persistence
    # ------------------------------------------------------------------

    def _load_plugin_locks(self) -> None:
        if self._plugins_path is None:
            self._plugin_locks = {}
            return
        self._plugin_locks = read_plugin_locks(self._plugins_path.parent)

    def _save_plugin_locks(self) -> None:
        if self._plugins_path is None:
            return
        write_plugin_locks(self._plugins_path.parent, self._plugin_locks)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def load_plugins(self, plugins_path: Path, plugin_extensions: list[str]) -> None:
        """Load plugins.txt for the given path and extension list."""
        self._plugins_path = plugins_path
        self._plugin_extensions = plugin_extensions
        self._refresh_plugins_tab()

    def clear_plugin_selection(self):
        """Clear the plugin list selection, e.g. when a mod is selected."""
        if self._sel_idx >= 0 or self._psel_set or self._master_highlights:
            self._sel_idx = -1
            self._psel_set = set()
            self._master_highlights = set()
            self._predraw()

    def set_highlighted_plugins(
        self,
        mod_name: str | None,
        mod_names: set[str] | None = None,
        bsa_higher_mods: set[str] | None = None,
        bsa_lower_mods: set[str] | None = None,
    ):
        """Highlight plugins belonging to the given mod(s) (orange), e.g. when a mod is selected.

        *mod_names* — when provided, highlight plugins belonging to **any** of
        the given mods (used for multi-selection).  Falls back to *mod_name*
        for single-selection compatibility.
        *bsa_higher_mods* / *bsa_lower_mods* — mods in a BSA conflict with the
        selection. Their plugins get painted green (higher = selection beats
        them) / red (lower = they beat selection), matching the modlist panel
        row-colour convention.
        """
        names = mod_names if mod_names else ({mod_name} if mod_name else set())
        if not names:
            new_highlighted = set()
        else:
            new_highlighted = {p for p, m in self._plugin_mod_map.items() if m in names}
        # Only plugins that actually own a BSA participate in BSA conflict
        # colouring — a standalone plugin with no matching BSA doesn't load
        # any archive contents, so it shouldn't be painted red/green.
        bsa_plugin_filter = self._bsa_owning_plugin_set(
            (bsa_higher_mods or set()) | (bsa_lower_mods or set())
        )
        new_bsa_higher = (
            {p for p, m in self._plugin_mod_map.items()
             if m in bsa_higher_mods and p.lower() in bsa_plugin_filter}
            if bsa_higher_mods else set()
        )
        new_bsa_lower = (
            {p for p, m in self._plugin_mod_map.items()
             if m in bsa_lower_mods and p.lower() in bsa_plugin_filter}
            if bsa_lower_mods else set()
        )
        changed = (
            new_highlighted != self._highlighted_plugins
            or bool(self._master_highlights)
            or new_bsa_higher != self._bsa_conflict_higher_plugins
            or new_bsa_lower != self._bsa_conflict_lower_plugins
        )
        self._highlighted_plugins = new_highlighted
        self._master_highlights = set()
        self._bsa_conflict_higher_plugins = new_bsa_higher
        self._bsa_conflict_lower_plugins = new_bsa_lower
        if changed:
            self._predraw()
        # Also update Ini Files tab: marker strip and row highlight
        # For multi-select, use the primary mod_name for the ini tab
        if getattr(self, "_highlighted_ini_mod", None) != mod_name:
            self._highlighted_ini_mod = mod_name
            self._apply_ini_row_highlight()
            self._draw_ini_marker_strip()
        # Same treatment for the Data tab.
        if getattr(self, "_highlighted_data_mod", None) != mod_name:
            self._highlighted_data_mod = mod_name
            if hasattr(self, "_data_tree"):
                self._apply_data_row_highlight()
                self._draw_data_marker_strip()

    def refresh_theme(self) -> None:
        """Re-apply theme-dependent tags and force a redraw after a colour change.

        Several trees cache theme colours in their tag config; this re-runs
        those `tag_configure` calls so edits take effect without a restart.
        """
        try:
            self._ini_files_tree.tag_configure(
                "mod_highlight", background=_theme.plugin_mod, foreground=TEXT_MAIN,
            )
        except Exception:
            pass
        try:
            self._data_tree.tag_configure(
                "mod_highlight", background=_theme.plugin_mod, foreground=TEXT_MAIN,
            )
        except Exception:
            pass
        # Mod Files, Archive, Data tabs use conflict_win / conflict_lose tags.
        for tree_attr in ("_mf_tree", "_arc_tree", "_data_tree"):
            tree = getattr(self, tree_attr, None)
            if tree is None:
                continue
            try:
                tree.tag_configure("conflict_win",  foreground=_theme.conflict_higher)
            except Exception:
                pass
            try:
                tree.tag_configure("conflict_lose", foreground=_theme.conflict_lower)
            except Exception:
                pass
        try:
            self._predraw()
        except Exception:
            pass
        try:
            self._draw_ini_marker_strip()
        except Exception:
            pass
        try:
            self._apply_ini_row_highlight()
        except Exception:
            pass
        try:
            self._draw_data_marker_strip()
        except Exception:
            pass
        try:
            self._apply_data_row_highlight()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Plugins tab refresh (canvas-based)
    # ------------------------------------------------------------------

    # Colours for framework status banners
    _FW_GREEN_BG   = BG_GREEN_DEEP
    _FW_GREEN_TEXT = BG_GREEN_TEXT
    _FW_RED_BG     = BG_RED_DEEP
    _FW_RED_TEXT   = BG_RED_TEXT
    _FW_ORANGE_BG  = BG_ORANGE_DEEP
    _FW_ORANGE_TEXT = BG_ORANGE_TEXT
    _FW_BLUE_BG    = BG_BLUE_DEEP
    _FW_BLUE_TEXT  = BG_BLUE_TEXT

    def _refresh_framework_banners(self) -> None:
        """Rebuild the framework status banners at the top of the Plugins tab.

        Reuses existing banner widgets when the framework list hasn't changed,
        only reconfiguring colours and text — avoids the visible destroy/create
        flicker on every filemap rebuild.
        """
        if self._game is None:
            for widget in self._framework_banner_widgets:
                widget.destroy()
            self._framework_banner_widgets.clear()
            self._framework_banners_frame.grid_remove()
            return

        frameworks: dict[str, str] = {}
        try:
            frameworks = self._game.frameworks
        except Exception:
            pass

        if not frameworks:
            for widget in self._framework_banner_widgets:
                widget.destroy()
            self._framework_banner_widgets.clear()
            self._framework_banners_frame.grid_remove()
            return

        game_root: "Path | None" = None
        try:
            game_root = self._game.get_game_path() if hasattr(self._game, "get_game_path") else None
        except Exception:
            pass

        root_folder: "Path | None" = None
        try:
            root_folder = self._game.get_effective_root_folder_path()
        except Exception:
            pass

        # Show the container now that we know there's at least one banner to display
        self._framework_banners_frame.grid(row=1, column=0, sticky="ew")

        # Set of filemap keys (deploy-relative paths, lowercased) — lets us tell
        # "not deployed but staged in the modlist" apart from "not present at all".
        staged_keys: set[str] = set()
        filemap_path_str = self._get_filemap_path()
        if filemap_path_str:
            fm_path = Path(filemap_path_str)
            # Root-flagged mods land in filemap_root.txt, not filemap.txt.
            for fm in (fm_path, fm_path.parent / "filemap_root.txt"):
                if not fm.is_file():
                    continue
                try:
                    with fm.open(encoding="utf-8") as f:
                        for line in f:
                            if "\t" not in line:
                                continue
                            rel_path = line.split("\t", 1)[0].replace("\\", "/")
                            staged_keys.add(rel_path.lower())
                except OSError:
                    pass

        # Basenames of every file living in a *disabled* mod — lets us flag a
        # framework that's installed but toggled off (it won't be in the
        # enabled-only filemap, so it would otherwise read as "Not Present").
        disabled_basenames = self._framework_disabled_basenames(filemap_path_str)

        # Root_Folder staging only reaches the game root on deploy, and only
        # while the Mod List panel's Root_Folder toggle is on — so a staging
        # hit maps to the orange/blue states, never to "Installed".
        rf_allowed = getattr(self._game, "root_folder_deploy_enabled", True)
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        rf_enabled = bool(getattr(mod_panel, "_root_folder_enabled", True))

        # Build the desired banner states
        banner_data: list[tuple[str, str, str]] = []  # (msg, bg, fg)
        for label, exe in frameworks.items():
            exe_path = Path(exe)
            present = False
            if game_root is not None:
                if _file_exists_ci(game_root, exe_path):
                    present = True

            in_root_staging = False
            if not present and rf_allowed and root_folder is not None:
                in_root_staging = _file_exists_ci(root_folder, exe_path)

            exe_basename = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()

            if present:
                banner_data.append((
                    f"✔  {label} Installed",
                    self._FW_GREEN_BG,
                    self._FW_GREEN_TEXT,
                ))
            elif in_root_staging and not rf_enabled:
                banner_data.append((
                    f"●  {label} present in modlist but not enabled",
                    self._FW_BLUE_BG,
                    self._FW_BLUE_TEXT,
                ))
            elif in_root_staging or self._framework_exe_in_staged(exe, staged_keys):
                banner_data.append((
                    f"●  {label} present in modlist but not deployed",
                    self._FW_ORANGE_BG,
                    self._FW_ORANGE_TEXT,
                ))
            elif exe_basename and exe_basename in disabled_basenames:
                banner_data.append((
                    f"●  {label} present in modlist but not enabled",
                    self._FW_BLUE_BG,
                    self._FW_BLUE_TEXT,
                ))
            else:
                banner_data.append((
                    f"✘  {label} Not Present",
                    self._FW_RED_BG,
                    self._FW_RED_TEXT,
                ))

        # Reuse existing widgets where possible; only create/destroy on count change.
        existing = self._framework_banner_widgets
        for idx, (msg, bg, fg) in enumerate(banner_data):
            if idx < len(existing):
                # Reconfigure in place — no destroy/create
                row_frame = existing[idx]
                row_frame.configure(fg_color=bg)
                children = row_frame.winfo_children()
                if children:
                    children[0].configure(text=msg, text_color=fg)
            else:
                # Need a new widget
                row_frame = ctk.CTkFrame(
                    self._framework_banners_frame,
                    fg_color=bg,
                    corner_radius=0,
                    height=22,
                )
                row_frame.grid(row=idx, column=0, sticky="ew", padx=0, pady=(1, 0))
                row_frame.grid_propagate(False)

                ctk.CTkLabel(
                    row_frame,
                    text=msg,
                    font=_theme.FONT_SMALL,
                    text_color=fg,
                    fg_color="transparent",
                    anchor="w",
                ).pack(side="left", padx=10, fill="y", expand=False)

                existing.append(row_frame)

        # Remove excess widgets if framework count decreased
        while len(existing) > len(banner_data):
            existing.pop().destroy()

    def _framework_exe_in_staged(self, exe: str, staged_keys: set[str]) -> bool:
        key = exe.replace("\\", "/").lower().lstrip("/")
        if key in staged_keys:
            return True
        # Framework paths are written relative to the game root (e.g.
        # "Mods/ContentPatcher/ContentPatcher.dll"), but filemap keys are
        # relative to the mods deploy dir — strip the leading mods_dir so the
        # comparison lines up for games whose frameworks live inside Mods/.
        mods_dir = (getattr(self._game, "mods_dir", "") or "").strip("/\\").lower()
        if mods_dir:
            prefix = mods_dir + "/"
            if key.startswith(prefix) and key[len(prefix):] in staged_keys:
                return True
        # Loose framework files (e.g. BG3 ships DWrite.dll at the mod root) are
        # relocated by a custom routing rule at deploy time, so the filemap key
        # is the bare filename while the framework path includes the dest dir
        # (bin/DWrite.dll). Fall back to a basename match in that case.
        basename = key.rsplit("/", 1)[-1]
        if basename and any(k.rsplit("/", 1)[-1] == basename for k in staged_keys):
            return True
        return False

    def _framework_disabled_basenames(self, filemap_path_str: "str | None") -> set[str]:
        """Lowercased basenames of every file belonging to a disabled mod.

        Lets us flag a framework that's installed but toggled off. modindex.bin
        sits beside filemap.txt, but modlist.txt lives in the profile dir, which
        is a *different* directory for some profiles (e.g. the "default"
        profile's filemap is in the staging root, not the profile dir) — so the
        modlist path comes from the mod panel rather than the filemap dir.
        """
        if not filemap_path_str:
            return set()
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        modlist_path = getattr(mod_panel, "_modlist_path", None) if mod_panel else None
        index_path = Path(filemap_path_str).parent / "modindex.bin"
        if not modlist_path or not Path(modlist_path).is_file() or not index_path.is_file():
            return set()
        try:
            from Utils.modlist import read_modlist
            from Utils.filemap import read_mod_index
            disabled = {e.name for e in read_modlist(Path(modlist_path))
                        if not e.is_separator and not e.enabled}
            if not disabled:
                return set()
            index = read_mod_index(index_path) or {}
        except Exception:
            return set()
        basenames: set[str] = set()
        for mod_name in disabled:
            entry = index.get(mod_name)
            if not entry:
                continue
            normal, root = entry
            for k in (*normal.keys(), *root.keys()):
                basenames.add(k.rsplit("/", 1)[-1])
        return basenames

    def _script_extender_detected(self) -> bool:
        """True if a script-extender framework loader is present in this profile.

        Mirrors the framework-banner logic in `_refresh_framework_banners`: a
        loader counts as installed if it sits in the game root, the Root_Folder
        staging dir, the enabled filemap, or a disabled mod. Used by the LOOT
        tooltip to suppress a "Requires (missing): Script Extender" line when the
        extender is actually installed (e.g. deployed as a mod rather than
        dropped in the game root).
        """
        if self._game is None:
            return False
        try:
            frameworks: dict[str, str] = self._game.frameworks or {}
        except Exception:
            frameworks = {}
        if not frameworks:
            return False

        # Restrict to script-extender loaders (frameworks may also hold things
        # like ContentPatcher.dll that aren't script extenders).
        se_exes = [
            exe for label, exe in frameworks.items()
            if "script extender" in label.lower()
            or re.search(r"\b(sk?se(64|vr)?|f4se(vr)?|fose|nvse|obse|sfse|mwse)\b",
                         exe.rsplit("/", 1)[-1].lower())
        ]
        if not se_exes:
            return False

        game_root: "Path | None" = None
        try:
            game_root = self._game.get_game_path() if hasattr(self._game, "get_game_path") else None
        except Exception:
            pass
        root_folder: "Path | None" = None
        try:
            root_folder = self._game.get_effective_root_folder_path()
        except Exception:
            pass

        filemap_path_str = self._get_filemap_path()
        staged_keys: set[str] = set()
        if filemap_path_str:
            fm_path = Path(filemap_path_str)
            for fm in (fm_path, fm_path.parent / "filemap_root.txt"):
                if not fm.is_file():
                    continue
                try:
                    with fm.open(encoding="utf-8") as f:
                        for line in f:
                            if "\t" not in line:
                                continue
                            rel_path = line.split("\t", 1)[0].replace("\\", "/")
                            staged_keys.add(rel_path.lower())
                except OSError:
                    pass

        rf_allowed = getattr(self._game, "root_folder_deploy_enabled", True)
        disabled_basenames = self._framework_disabled_basenames(filemap_path_str)

        for exe in se_exes:
            exe_path = Path(exe)
            if game_root is not None and _file_exists_ci(game_root, exe_path):
                return True
            if rf_allowed and root_folder is not None and _file_exists_ci(root_folder, exe_path):
                return True
            if self._framework_exe_in_staged(exe, staged_keys):
                return True
            exe_basename = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if exe_basename and exe_basename in disabled_basenames:
                return True
        return False

    def _get_parsed_filemap(self, filemap_path: "Path | None" = None):
        """Return a cached parse of filemap.txt as list[(rel_path, mod_name)].

        Keyed on (path, mtime), so every main-thread consumer within one
        refresh (plugin sync, master checking, staged paths, Data/Ini tabs)
        shares a single read+parse of the ~74k-line file instead of repeating
        it. Backslashes in rel_path are left as-is — callers normalise as
        needed (the Data tab wants raw, master checking wants forward slashes).
        Returns [] when the path is missing/unreadable.
        """
        if filemap_path is None:
            fp_str = self._get_filemap_path()
            filemap_path = Path(fp_str) if fp_str else None
        if filemap_path is None:
            return []
        try:
            mtime = filemap_path.stat().st_mtime
        except OSError:
            return []
        sig = (str(filemap_path), mtime)
        if self._filemap_parse_cache is not None and self._filemap_parse_cache_sig == sig:
            return self._filemap_parse_cache
        entries: list[tuple[str, str]] = []
        try:
            with filemap_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if "\t" not in line:
                        continue
                    rel_path, mod_name = line.split("\t", 1)
                    entries.append((rel_path, mod_name))
        except OSError:
            return []
        self._filemap_parse_cache = entries
        self._filemap_parse_cache_sig = sig
        return entries

    def _refresh_plugins_tab(self) -> None:
        """Reload plugin entries from plugins.txt and redraw."""
        try:
            topbar = self.winfo_toplevel()._topbar
            game = _GAMES.get(topbar._game_var.get())
            loot_enabled = getattr(game, "loot_sort_enabled", False) if game else False
        except Exception:
            loot_enabled = False
        if hasattr(self, "_loot_toolbar"):
            if loot_enabled:
                self._loot_toolbar.grid()
            else:
                self._loot_toolbar.grid_remove()
        self._refresh_framework_banners()
        self._sel_idx = -1
        self._psel_set = set()
        self._master_highlights = set()
        self._drag_idx = -1
        self._highlighted_plugins = set()
        self._bsa_conflict_higher_plugins = set()
        self._bsa_conflict_lower_plugins = set()
        self._highlighted_ini_mod = None
        if hasattr(self, "_ini_marker_strip"):
            self._apply_ini_row_highlight()
            self._draw_ini_marker_strip()

        if self._plugins_path is None or not self._plugin_extensions:
            self._plugin_entries = []
            self._loot_info = {}
            self._apply_plugin_search_filter()
            self._predraw()
            return

        self._load_plugin_locks()
        self._load_loot_messages()
        self._invalidate_enabled_mod_ids()
        mod_entries = read_plugins(self._plugins_path, star_prefix=self._plugins_star_prefix)
        mod_map = {e.name.lower(): e for e in mod_entries}

        loadorder_path = self._plugins_path.parent / "loadorder.txt"
        saved_order = read_loadorder(loadorder_path)

        # Orphan plugins: files sitting in Data/ that the user installed manually
        # — not in plugins.txt, not in loadorder.txt, not vanilla. Surface them
        # so they can be toggled from the panel.
        #
        # When a deploy is active, Data/ contains mod hardlinks owned by the
        # mod manager. Anything NOT present in Data_Core/ (the vanilla snapshot)
        # came from a mod deploy and must not be treated as a manual orphan —
        # otherwise disabling a mod still leaves its plugin in the panel,
        # because the hardlink in Data/ outlives its plugins.txt entry until
        # the next deploy.
        # Plugin filenames that actually exist in Data/ this refresh. Used both
        # to surface orphans and to prune loadorder.txt entries whose source
        # plugin has since disappeared (e.g. a mod packed into a BSA so its
        # loose .esp no longer deploys, or the mod was removed). Left as None
        # when Data/ can't be scanned, so we don't prune on missing info.
        present_lower: set[str] | None = None
        data_dir = self._game.get_vanilla_plugins_path() if self._game and hasattr(self._game, "get_vanilla_plugins_path") else None
        if data_dir and data_dir.is_dir() and self._plugin_extensions:
            exts_lower = {e.lower() for e in self._plugin_extensions}
            saved_lower = {n.lower() for n in saved_order}
            present_lower = set()
            foreign = foreign_deployed_plugin_basenames(self._game)
            core_dir = data_dir.parent / (data_dir.name + "_Core")
            core_names: set[str] | None = None
            if core_dir.is_dir():
                core_names = set()
                try:
                    for centry in core_dir.iterdir():
                        if centry.is_file() and centry.suffix.lower() in exts_lower:
                            core_names.add(centry.name.lower())
                except OSError:
                    core_names = None
            try:
                for entry in data_dir.iterdir():
                    if not entry.is_file() or entry.suffix.lower() not in exts_lower:
                        continue
                    low = entry.name.lower()
                    present_lower.add(low)
                    if low in mod_map or low in self._vanilla_plugins or low in saved_lower:
                        continue
                    if low in foreign:
                        continue
                    # Deploy active → only vanilla files (present in Data_Core/)
                    # are legitimate. Mod-deployed plugins that no longer have
                    # a plugins.txt entry are stale hardlinks, not orphans.
                    if core_names is not None and low not in core_names:
                        continue
                    orphan = PluginEntry(name=entry.name, enabled=True)
                    mod_map[low] = orphan
                    mod_entries.append(orphan)
            except OSError:
                pass

        # For legacy (non-star) games plugins.txt contains only *enabled*
        # plugins; disabled ones are recovered by subtracting plugins.txt
        # from loadorder.txt. Synthesize disabled entries so the panel can
        # show and re-enable them.
        if not self._plugins_star_prefix and saved_order:
            for name in saved_order:
                low = name.lower()
                if low in mod_map:
                    continue
                if low in self._vanilla_plugins:
                    continue
                # Prune stale loadorder.txt entries: a disabled plugin only
                # survives if its file still exists in Data/. If it's gone
                # (mod removed, or packed into a BSA so the loose .esp no longer
                # deploys), skip it — it then drops out of loadorder.txt via the
                # sync below instead of lingering as a phantom panel row.
                if present_lower is not None and low not in present_lower:
                    continue
                disabled_entry = PluginEntry(name=name, enabled=False)
                mod_map[low] = disabled_entry
                mod_entries.append(disabled_entry)

        if saved_order:
            ordered: list[PluginEntry] = []
            seen: set[str] = set()
            _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}

            if self._plugins_include_vanilla:
                # Vanilla plugins must load first. Build vanilla bucket first
                # (preserving saved_order within vanilla, with unseen ones sorted
                # at the end of the vanilla block), then append mods.
                vanilla_ordered: list[PluginEntry] = []
                mod_ordered: list[PluginEntry] = []
                for name in saved_order:
                    low = name.lower()
                    if low in seen:
                        continue
                    seen.add(low)
                    if low in self._vanilla_plugins:
                        vanilla_ordered.append(PluginEntry(
                            name=self._vanilla_plugins[low], enabled=True,
                        ))
                    elif low in mod_map:
                        mod_ordered.append(mod_map[low])
                for low, orig in sorted(
                    self._vanilla_plugins.items(),
                    key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]),
                ):
                    if low not in seen:
                        vanilla_ordered.append(PluginEntry(name=orig, enabled=True))
                        seen.add(low)
                for e in mod_entries:
                    if e.name.lower() not in seen:
                        mod_ordered.append(e)
                        seen.add(e.name.lower())
                ordered = vanilla_ordered + mod_ordered
            else:
                # Standard Bethesda: vanilla pinned to the top, mods follow in
                # saved_order (LOOT manages mod positions).
                vanilla_ordered: list[PluginEntry] = []
                mod_ordered: list[PluginEntry] = []
                for name in saved_order:
                    low = name.lower()
                    if low in seen:
                        continue
                    seen.add(low)
                    if low in self._vanilla_plugins:
                        vanilla_ordered.append(PluginEntry(
                            name=self._vanilla_plugins[low], enabled=True,
                        ))
                    elif low in mod_map:
                        mod_ordered.append(mod_map[low])
                for low, orig in sorted(
                    self._vanilla_plugins.items(),
                    key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]),
                ):
                    if low not in seen:
                        vanilla_ordered.append(PluginEntry(name=orig, enabled=True))
                        seen.add(low)
                for e in mod_entries:
                    if e.name.lower() not in seen:
                        mod_ordered.append(e)
                        seen.add(e.name.lower())
                ordered = vanilla_ordered + mod_ordered

            self._plugin_entries = ordered
        else:
            existing_lower = {e.name.lower() for e in mod_entries}
            _ext_order = {".esm": 0, ".esp": 1, ".esl": 2}
            vanilla_prepend = [
                PluginEntry(name=original, enabled=True)
                for lower, original in sorted(
                    self._vanilla_plugins.items(),
                    key=lambda kv: (_ext_order.get(Path(kv[0]).suffix, 9), kv[0]),
                )
                if lower not in existing_lower
            ]
            self._plugin_entries = vanilla_prepend + mod_entries

        self._check_all_masters()

        # Engine parity (MO2/Wrye Bash): master-flagged plugins always load
        # before non-masters — keep the panel showing that real order.
        _hoisted = self._enforce_master_block()

        # Sync loadorder.txt: use _plugin_entries order as the source of truth,
        # so any vanilla plugins prepended above are written at the top.
        final_lo = [e.name for e in self._plugin_entries]
        _order_changed = final_lo != saved_order
        if _order_changed:
            write_loadorder(loadorder_path, [PluginEntry(name=n, enabled=True) for n in final_lo])
            if _hoisted:
                # The hoist moved plugins relative to the saved order —
                # late-master warnings are order-sensitive, recompute.
                self._check_all_masters()

        # Sync plugins.txt so vanilla plugins are included/excluded and ordered
        # correctly (e.g. Oblivion Remastered requires all plugins in plugins.txt).
        # A persisted master-block hoist also needs plugins.txt to match.
        if self._plugins_include_vanilla or (_hoisted and _order_changed):
            self._save_plugins()
        elif self._plugin_order_on_change is not None:
            # _save_plugins() fires the order-change hook; when it isn't called
            # (non-vanilla-plugins games), still notify so BSA conflicts can
            # recompute from the freshly-loaded plugin order.
            self._plugin_order_on_change()

        self._apply_plugin_search_filter()
        self._refresh_userlist_set()
        self._predraw()

    def _master_block_enabled(self) -> bool:
        return bool(getattr(self._game, "plugins_master_block", False))

    def _is_master_group(self, name: str) -> bool:
        """Engine rule (as in MO2/WB): .esm/.esl extension or master header flag."""
        low = name.lower()
        return low.endswith((".esm", ".esl")) or low in self._master_flag_plugins

    def _enforce_master_block(self) -> bool:
        """Stable-partition entries so master-group plugins precede non-masters.

        Mirrors MO2's fixPluginRelationships: the engine always loads the
        master block first, so the panel keeps showing that real order.
        Returns True when the order changed (selection indices are remapped).
        """
        if not self._master_block_enabled() or not self._plugin_entries:
            return False
        masters: list[PluginEntry] = []
        others: list[PluginEntry] = []
        for e in self._plugin_entries:
            (masters if self._is_master_group(e.name) else others).append(e)
        if not masters or not others:
            return False
        new_entries = masters + others
        if all(a is b for a, b in zip(new_entries, self._plugin_entries)):
            return False
        old = self._plugin_entries
        sel_ids = {id(old[i]) for i in self._psel_set if 0 <= i < len(old)}
        cur_id = id(old[self._sel_idx]) if 0 <= self._sel_idx < len(old) else None
        self._plugin_entries = new_entries
        if sel_ids or cur_id is not None:
            id_to_idx = {id(e): i for i, e in enumerate(new_entries)}
            self._psel_set = {id_to_idx[i] for i in sel_ids if i in id_to_idx}
            if cur_id is not None:
                self._sel_idx = id_to_idx.get(cur_id, self._sel_idx)
        return True

    def _save_plugins(self) -> None:
        """Write current plugin entries to plugins.txt and loadorder.txt.

        plugins.txt — mod plugins only (vanilla excluded, the game strips them).
        loadorder.txt — full order including vanilla, so their LOOT-sorted
        positions are preserved across refreshes.
        """
        if self._plugins_path is None:
            return
        self._enforce_master_block()
        include_vanilla = self._plugins_include_vanilla
        mod_entries: list[PluginEntry] = []
        for entry in self._plugin_entries:
            if include_vanilla or entry.name.lower() not in self._vanilla_plugins:
                mod_entries.append(entry)
        write_plugins(self._plugins_path, mod_entries, star_prefix=self._plugins_star_prefix)
        write_loadorder(self._plugins_path.parent / "loadorder.txt", self._plugin_entries)
        # Timestamp-ordered games (Oblivion/FO3/FNV): re-stamp deployed plugin
        # mtimes so the new order is the one the engine (and Wrye Bash) uses.
        game = self._game
        if game is not None and hasattr(game, "stamp_plugin_load_order"):
            profile = self._plugins_path.parent.name
            try:
                if (game.get_deploy_active()
                        and game.get_last_deployed_profile() == profile):
                    game.stamp_plugin_load_order(profile)
            except Exception as exc:
                self._log(f"Plugin mtime stamping failed: {exc}")
        if self._plugin_order_on_change is not None:
            self._plugin_order_on_change()

    # ------------------------------------------------------------------
    # Keyboard reorder
    # ------------------------------------------------------------------

    def _move_plugins_up(self) -> None:
        """Move selected plugins up one slot (keyboard shortcut)."""
        if not self._plugin_entries or self._plugin_filtered_indices is not None:
            return
        indices = sorted(self._psel_set) if self._psel_set else (
            [self._sel_idx] if self._sel_idx >= 0 else []
        )
        if not indices or indices[0] <= 0:
            return
        if any(self._is_plugin_locked(i) or self._is_plugin_locked(i - 1) for i in indices):
            return
        if self._master_block_enabled() and any(
            self._is_master_group(self._plugin_entries[i].name)
            != self._is_master_group(self._plugin_entries[i - 1].name)
            for i in indices
        ):
            return  # don't cross the master/plugin boundary
        for i in indices:
            self._plugin_entries[i], self._plugin_entries[i - 1] = (
                self._plugin_entries[i - 1], self._plugin_entries[i],
            )
        moved = set(indices)
        self._psel_set = {i - 1 for i in indices}
        if self._sel_idx in moved:
            self._sel_idx -= 1
        self._save_plugins()
        self._check_all_masters()
        self._predraw()

    def _move_plugins_down(self) -> None:
        """Move selected plugins down one slot (keyboard shortcut)."""
        if not self._plugin_entries or self._plugin_filtered_indices is not None:
            return
        indices = sorted(self._psel_set, reverse=True) if self._psel_set else (
            [self._sel_idx] if self._sel_idx >= 0 else []
        )
        if not indices or indices[0] >= len(self._plugin_entries) - 1:
            return
        if any(self._is_plugin_locked(i) or self._is_plugin_locked(i + 1) for i in indices):
            return
        if self._master_block_enabled() and any(
            self._is_master_group(self._plugin_entries[i].name)
            != self._is_master_group(self._plugin_entries[i + 1].name)
            for i in indices
        ):
            return  # don't cross the master/plugin boundary
        for i in indices:
            self._plugin_entries[i], self._plugin_entries[i + 1] = (
                self._plugin_entries[i + 1], self._plugin_entries[i],
            )
        moved = set(indices)
        self._psel_set = {i + 1 for i in indices}
        if self._sel_idx in moved:
            self._sel_idx += 1
        self._save_plugins()
        self._check_all_masters()
        self._predraw()

    # ------------------------------------------------------------------
    # Canvas drawing
    # ------------------------------------------------------------------

    def _master_names_lower(self) -> set[str]:
        """Lowercased copy of _master_highlights, cached until the set is replaced."""
        mh = self._master_highlights
        if self._masters_lower_src is not mh:
            self._masters_lower_src = mh
            self._masters_lower_cache = {m.lower() for m in mh}
        return self._masters_lower_cache

    def _update_plugin_counters(self) -> None:
        """Recompute the active/ESL counter labels; no-op when texts are unchanged."""
        all_entries = self._plugin_entries
        active = sum(1 for e in all_entries if e.enabled)
        esl_count = sum(
            1 for e in all_entries
            if e.name.lower() in self._esl_flagged_plugins
        )
        texts = (
            f"{active}/{len(all_entries)} active",
            f"{esl_count} ESL",
            f"{len(all_entries) - esl_count} non-ESL",
        )
        if texts == self._plugin_counter_texts:
            return
        self._plugin_counter_texts = texts
        self._plugin_counter_label.configure(text=texts[0])
        self._plugin_esl_counter_label.configure(text=texts[1])
        self._plugin_non_esl_counter_label.configure(text=texts[2])

    def _predraw(self, update_counters: bool = True):
        """Redraw by reconfiguring the pre-allocated pool of canvas items."""
        self._predraw_after_id = None
        c = self._pcanvas
        cw = self._pcanvas_w
        all_entries = self._plugin_entries
        filtered = self._plugin_filtered_indices
        # When a search filter is active use the filtered index list; disable drag-drop
        if filtered is not None:
            view_entries = [all_entries[i] for i in filtered]
            dragging = False
        else:
            view_entries = all_entries
            dragging = self._drag_idx >= 0 and self._drag_moved
        entries = view_entries
        n = len(entries)
        total_h = n * self.ROW_H

        if update_counters:
            self._update_plugin_counters()

        canvas_top = int(c.canvasy(0))
        canvas_h = c.winfo_height()
        # Tall viewports (4K monitors, low UI scale) can hold more rows than
        # the default pool — grow it so rows past the last slot still render.
        self._ensure_pool_capacity(canvas_h // self.ROW_H + 3)
        first_row = max(0, canvas_top // self.ROW_H)
        last_row = min(n, (canvas_top + canvas_h) // self.ROW_H + 2)
        vis_count = last_row - first_row
        master_names_lower = self._master_names_lower()

        _pool_last_state = self._pool_last_state

        # Row-independent constants hoisted out of the pool-slot loop.
        _theme_bgs = (_theme.conflict_higher, _theme.conflict_lower, _theme.plugin_mod)
        name_font = (_theme.FONT_FAMILY, _theme.FS11)
        name_max_px = self._pcol_x[2] - self._pcol_x[1] - scaled(4)
        strip_w = scaled(3)
        idx_text_x = self._pcol_x[4] + scaled(25)
        flags_x0 = self._pcol_x[2]
        flags_x1 = self._pcol_x[3]
        flag_gap = scaled(20)
        flags_center = (flags_x0 + flags_x1) // 2
        ul_dot_r = scaled(4)
        cb_cx = self._pcol_x[0] + scaled(12)
        cb_size = scaled(18)
        lk_cx = self._pcol_x[3] + scaled(12)

        for s in range(self._pool_size):
            row = first_row + s
            if s < vis_count and row < n:
                entry = entries[row]
                y_top = row * self.ROW_H
                y_bot = y_top + self.ROW_H
                y_mid = y_top + self.ROW_H // 2
                # actual_idx is the index into _plugin_entries (differs from row when filtered)
                actual_idx = filtered[row] if filtered is not None else row

                is_sel = (actual_idx in self._psel_set) or (actual_idx == self._drag_idx and self._drag_moved)
                name_lower = entry.name.lower()
                if is_sel:
                    bg = BG_SELECT
                elif name_lower in master_names_lower:
                    bg = BG_GREEN_ROW
                elif name_lower in self._highlighted_plugins:
                    bg = _theme.plugin_mod
                elif name_lower in self._bsa_conflict_higher_plugins:
                    bg = _theme.conflict_higher
                elif name_lower in self._bsa_conflict_lower_plugins:
                    bg = _theme.conflict_lower
                elif actual_idx == self._phover_idx:
                    bg = BG_HOVER_ROW
                else:
                    bg = BG_ROW if row % 2 == 0 else BG_ROW_ALT

                has_missing = entry.name in self._missing_masters
                has_late = entry.name in self._late_masters
                has_vmm = entry.name in self._version_mismatch_masters
                has_ul = name_lower in self._userlist_plugins
                has_ul_cycle = name_lower in self._userlist_cycle_plugins
                has_esl = name_lower in self._esl_flagged_plugins
                is_vanilla = name_lower in self._vanilla_plugins
                is_locked = bool(self._plugin_locks.get(entry.name, False))
                has_loot = self._has_loot_tooltip_content(entry.name)
                has_dirty = self._has_dirty_edits(entry.name)
                has_tags = self._has_bash_tags(entry.name)
                bos_sp_kind = self._bos_sp_plugins.get(name_lower, "")
                has_bos_sp = bool(bos_sp_kind)

                state_key = (
                    "v",
                    actual_idx,
                    row,
                    bg,
                    cw,
                    dragging,
                    entry.name,
                    entry.enabled,
                    has_missing,
                    has_late,
                    has_vmm,
                    has_ul,
                    has_ul_cycle,
                    has_esl,
                    is_vanilla,
                    is_locked,
                    has_loot,
                    has_dirty,
                    has_tags,
                    bos_sp_kind,
                )
                if _pool_last_state[s] == state_key and self._pool_data_idx[s] == actual_idx:
                    continue

                c.coords(self._pool_bg[s], 0, y_top, cw, y_bot)
                c.itemconfigure(self._pool_bg[s], fill=bg, state="normal")

                if has_missing:
                    c.coords(self._pool_missing_strip[s], 0, y_top, strip_w, y_bot)
                    c.itemconfigure(self._pool_missing_strip[s], state="normal")
                else:
                    c.itemconfigure(self._pool_missing_strip[s], state="hidden")

                if has_missing:
                    name_color = STATUS_BADGE_RED
                elif bg in _theme_bgs:
                    name_color = _theme.contrasting_text_color(bg)
                elif not entry.enabled:
                    name_color = TEXT_DIM
                else:
                    name_color = TEXT_MAIN
                display_name = _truncate_plugin_name(c, entry.name, name_font, name_max_px)
                c.coords(self._pool_name[s], self._pcol_x[1], y_mid)
                c.itemconfigure(self._pool_name[s], text=display_name,
                                fill=name_color, state="normal")

                c.coords(self._pool_idx_text[s], idx_text_x, y_mid)
                c.itemconfigure(self._pool_idx_text[s], text=f"{actual_idx:03d}",
                                fill=TEXT_DIM, state="normal")

                active_flags = [f for f in [has_missing, has_late, has_vmm, has_ul, has_esl, has_loot, has_dirty, has_tags, has_bos_sp] if f]
                n_flags = len(active_flags)
                # Pack flags tightly with a fixed gap, centered in the column.
                pack_start = flags_center - (flag_gap * (n_flags - 1)) // 2 if n_flags else flags_center
                _flag_pos = iter(pack_start + flag_gap * i for i in range(n_flags))

                warn_id = self._pool_warn[s]
                if warn_id is not None:
                    if has_missing:
                        c.coords(warn_id, next(_flag_pos), y_mid)
                        c.itemconfigure(warn_id, state="normal")
                    else:
                        c.itemconfigure(warn_id, state="hidden")

                late_warn_id = self._pool_late_warn[s]
                if late_warn_id is not None:
                    if has_late:
                        c.coords(late_warn_id, next(_flag_pos), y_mid)
                        c.itemconfigure(late_warn_id, state="normal")
                    else:
                        c.itemconfigure(late_warn_id, state="hidden")

                vmm_warn_id = self._pool_vmm_warn[s]
                if vmm_warn_id is not None:
                    if has_vmm:
                        c.coords(vmm_warn_id, next(_flag_pos), y_mid)
                        c.itemconfigure(vmm_warn_id, state="normal")
                    else:
                        c.itemconfigure(vmm_warn_id, state="hidden")

                ul_dot_id = self._pool_ul_dot[s] if s < len(self._pool_ul_dot) else None
                if ul_dot_id is not None:
                    if has_ul:
                        cx = next(_flag_pos)
                        r = ul_dot_r
                        c.coords(ul_dot_id, cx - r, y_mid - r, cx + r, y_mid + r)
                        c.itemconfigure(ul_dot_id,
                                        fill=STATUS_BADGE_RED if has_ul_cycle else TEXT_WHITE,
                                        state="normal")
                    else:
                        c.itemconfigure(ul_dot_id, state="hidden")

                esl_badge_id = self._pool_esl_badge[s] if s < len(self._pool_esl_badge) else None
                if esl_badge_id is not None:
                    if has_esl:
                        c.coords(esl_badge_id, next(_flag_pos), y_mid)
                        c.itemconfigure(esl_badge_id, state="normal")
                    else:
                        c.itemconfigure(esl_badge_id, state="hidden")

                loot_info_id = self._pool_loot_info[s] if s < len(self._pool_loot_info) else None
                if loot_info_id is not None:
                    if has_loot:
                        c.coords(loot_info_id, next(_flag_pos), y_mid)
                        c.itemconfigure(loot_info_id, state="normal")
                    else:
                        c.itemconfigure(loot_info_id, state="hidden")

                loot_dirty_id = self._pool_loot_dirty[s] if s < len(self._pool_loot_dirty) else None
                if loot_dirty_id is not None:
                    if has_dirty:
                        c.coords(loot_dirty_id, next(_flag_pos), y_mid)
                        c.itemconfigure(loot_dirty_id, state="normal")
                    else:
                        c.itemconfigure(loot_dirty_id, state="hidden")

                loot_tag_id = self._pool_loot_tag[s] if s < len(self._pool_loot_tag) else None
                if loot_tag_id is not None:
                    if has_tags:
                        c.coords(loot_tag_id, next(_flag_pos), y_mid)
                        c.itemconfigure(loot_tag_id, state="normal")
                    else:
                        c.itemconfigure(loot_tag_id, state="hidden")

                bos_sp_badge_id = self._pool_bos_sp_badge[s] if s < len(self._pool_bos_sp_badge) else None
                if bos_sp_badge_id is not None:
                    if bos_sp_kind:
                        _bos_sp_label = {"bos": "B", "sp": "S", "both": "B+S"}.get(bos_sp_kind, "P")
                        _bos_sp_color = {"bos": "#60a5fa", "sp": "#fbbf24", "both": "#c084fc"}.get(bos_sp_kind, "#c084fc")
                        c.coords(bos_sp_badge_id, next(_flag_pos), y_mid)
                        c.itemconfigure(bos_sp_badge_id, text=_bos_sp_label, fill=_bos_sp_color, state="normal")
                    else:
                        c.itemconfigure(bos_sp_badge_id, state="hidden")

                self._pool_data_idx[s] = actual_idx

                if not dragging:
                    cx1, cy1 = cb_cx - cb_size // 2, y_mid - cb_size // 2
                    cx2, cy2 = cb_cx + cb_size // 2, y_mid + cb_size // 2
                    c.coords(self._pool_check_rects[s], cx1, cy1, cx2, cy2)
                    c.itemconfigure(self._pool_check_rects[s],
                                    fill=BG_DEEP if entry.enabled else bg,
                                    state="normal")
                    c.coords(self._pool_check_marks[s], cb_cx, y_mid)
                    c.itemconfigure(self._pool_check_marks[s],
                                    fill=TEXT_DIM if is_vanilla else ACCENT,
                                    state="normal" if entry.enabled else "hidden")

                    c.coords(self._pool_lock_rects[s], lk_cx - cb_size // 2, cy1,
                             lk_cx + cb_size // 2, cy2)
                    c.itemconfigure(self._pool_lock_rects[s],
                                    fill=BG_DEEP if is_locked else bg,
                                    state="normal")
                    c.coords(self._pool_lock_marks[s], lk_cx, y_mid)
                    c.itemconfigure(self._pool_lock_marks[s],
                                    state="normal" if is_locked else "hidden")
                else:
                    c.itemconfigure(self._pool_check_rects[s], state="hidden")
                    c.itemconfigure(self._pool_check_marks[s], state="hidden")
                    c.itemconfigure(self._pool_lock_rects[s], state="hidden")
                    c.itemconfigure(self._pool_lock_marks[s], state="hidden")
                _pool_last_state[s] = state_key
            else:
                # Hidden branch: only issue hide calls if the slot wasn't already hidden.
                if _pool_last_state[s] == ("h",):
                    continue
                c.itemconfigure(self._pool_bg[s], state="hidden")
                c.itemconfigure(self._pool_missing_strip[s], state="hidden")
                c.itemconfigure(self._pool_name[s], state="hidden")
                c.itemconfigure(self._pool_idx_text[s], state="hidden")
                if self._pool_warn[s] is not None:
                    c.itemconfigure(self._pool_warn[s], state="hidden")
                if self._pool_late_warn[s] is not None:
                    c.itemconfigure(self._pool_late_warn[s], state="hidden")
                if self._pool_vmm_warn[s] is not None:
                    c.itemconfigure(self._pool_vmm_warn[s], state="hidden")
                if s < len(self._pool_ul_dot):
                    c.itemconfigure(self._pool_ul_dot[s], state="hidden")
                if s < len(self._pool_esl_badge):
                    c.itemconfigure(self._pool_esl_badge[s], state="hidden")
                if s < len(self._pool_loot_info) and self._pool_loot_info[s] is not None:
                    c.itemconfigure(self._pool_loot_info[s], state="hidden")
                if s < len(self._pool_loot_dirty) and self._pool_loot_dirty[s] is not None:
                    c.itemconfigure(self._pool_loot_dirty[s], state="hidden")
                if s < len(self._pool_loot_tag) and self._pool_loot_tag[s] is not None:
                    c.itemconfigure(self._pool_loot_tag[s], state="hidden")
                if s < len(self._pool_bos_sp_badge):
                    c.itemconfigure(self._pool_bos_sp_badge[s], state="hidden")
                c.itemconfigure(self._pool_check_rects[s], state="hidden")
                c.itemconfigure(self._pool_check_marks[s], state="hidden")
                c.itemconfigure(self._pool_lock_rects[s], state="hidden")
                c.itemconfigure(self._pool_lock_marks[s], state="hidden")
                self._pool_data_idx[s] = -1
                _pool_last_state[s] = ("h",)

        c.configure(scrollregion=(0, 0, cw, max(total_h, canvas_h)))
        self._draw_marker_strip()

    def _on_pmarker_strip_resize(self, _event):
        if self._marker_strip_after_id is not None:
            self.after_cancel(self._marker_strip_after_id)
        self._marker_strip_after_id = self.after(250, self._draw_marker_strip)

    def _draw_marker_strip(self):
        """Paint the combined scrollbar + marker strip.

        Layers (bottom → top):
          1. Trough background
          2. Tick marks (priority-merged by y-coord)
          3. Solid thumb (hides ticks underneath)
        """
        self._marker_strip_after_id = None
        c = self._pmarker_strip
        entries = self._plugin_entries
        n = len(entries)
        has_any = (self._highlighted_plugins or self._master_highlights
                   or self._missing_masters
                   or self._bsa_conflict_higher_plugins
                   or self._bsa_conflict_lower_plugins)
        strip_h = c.winfo_height()
        strip_w = c.winfo_width()

        cache_key = (
            n,
            strip_h,
            strip_w,
            tuple(e.name for e in entries),
            frozenset(self._missing_masters),
            frozenset(self._highlighted_plugins),
            frozenset(self._master_highlights),
            frozenset(self._bsa_conflict_higher_plugins),
            frozenset(self._bsa_conflict_lower_plugins),
        )
        if getattr(self, "_marker_strip_cache_key", None) == cache_key:
            # Trough + ticks unchanged; just re-paint thumb in case scroll moved.
            self._redraw_pthumb()
            return
        self._marker_strip_cache_key = cache_key

        # Fresh-create is faster than coords/itemconfigure for many items.
        c.delete("all")

        if strip_h <= 1 or strip_w <= 1:
            self._marker_strip_cache_key = None
            return

        # Trough
        c.create_rectangle(0, 0, strip_w, strip_h, fill=BG_DEEP, outline="", tags="trough")

        if n and has_any:
            master_names_lower = self._master_names_lower()
            plugin_mod_color = _theme.plugin_mod
            conflict_higher_color = _theme.conflict_higher
            conflict_lower_color = _theme.conflict_lower
            highlighted = self._highlighted_plugins
            missing = self._missing_masters
            higher = self._bsa_conflict_higher_plugins
            lower = self._bsa_conflict_lower_plugins
            strip_max = strip_h - 4

            # Priority: missing (red) > highlighted (mod) > master (green) > conflict_higher > conflict_lower.
            priority = {
                BTN_CANCEL: 5,
                plugin_mod_color: 4,
                STATUS_BADGE_GREEN: 3,
                conflict_higher_color: 2,
                conflict_lower_color: 1,
            }
            y_to_color: dict[int, str] = {}
            inv_n = 1.0 / n
            for i, e in enumerate(entries):
                name = e.name
                if name in missing:
                    color = BTN_CANCEL
                else:
                    name_lower = name.lower()
                    if name_lower in highlighted:
                        color = plugin_mod_color
                    elif name_lower in master_names_lower:
                        color = STATUS_BADGE_GREEN
                    elif name_lower in higher:
                        color = conflict_higher_color
                    elif name_lower in lower:
                        color = conflict_lower_color
                    else:
                        continue
                y = int(i * inv_n * strip_h)
                if y < 2:
                    y = 2
                elif y > strip_max:
                    y = strip_max
                existing = y_to_color.get(y)
                if existing is None or priority.get(color, 0) > priority.get(existing, 0):
                    y_to_color[y] = color
            ticks: list[tuple[int, str]] = sorted(y_to_color.items())

            create_rect = c.create_rectangle
            for y, color in ticks:
                create_rect(0, y, strip_w, y + 3, fill=color, outline="", tags="marker")

        self._redraw_pthumb()

    def _redraw_pthumb(self) -> None:
        c = self._pmarker_strip
        c.delete("thumb")
        strip_h = c.winfo_height()
        strip_w = c.winfo_width()
        if strip_h <= 1 or strip_w <= 1:
            return
        first = max(0.0, min(1.0, self._pscroll_first))
        last = max(first, min(1.0, self._pscroll_last))
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

    def _pscroll_set(self, first: str, last: str) -> None:
        try:
            f = float(first); l = float(last)
        except (TypeError, ValueError):
            return
        if f == self._pscroll_first and l == self._pscroll_last:
            return
        self._pscroll_first = f
        self._pscroll_last = l
        self._redraw_pthumb()

    def _on_pscrollbar_press(self, event):
        strip_h = self._pmarker_strip.winfo_height()
        if strip_h <= 1:
            return
        first = self._pscroll_first
        last = self._pscroll_last
        thumb_top = first * strip_h
        thumb_bot = last * strip_h
        if thumb_top <= event.y <= thumb_bot:
            self._pthumb_drag_offset = (event.y - thumb_top) / strip_h
        else:
            self._pthumb_drag_offset = (last - first) / 2.0
            self._pscroll_to_pointer(event.y)

    def _on_pscrollbar_drag(self, event):
        if self._pthumb_drag_offset is None:
            return
        self._pscroll_to_pointer(event.y)

    def _on_pscrollbar_release(self, _event):
        self._pthumb_drag_offset = None

    def _pscroll_to_pointer(self, py: int) -> None:
        strip_h = self._pmarker_strip.winfo_height()
        if strip_h <= 1 or self._pthumb_drag_offset is None:
            return
        frac = (py / strip_h) - self._pthumb_drag_offset
        frac = max(0.0, min(1.0, frac))
        self._pcanvas.yview_moveto(frac)
        self._schedule_predraw()

    def _schedule_predraw(self) -> None:
        """Debounced _predraw — coalesces rapid scroll/resize events."""
        if self._predraw_after_id is not None:
            self.after_cancel(self._predraw_after_id)
        # Scroll/resize never change the counts — skip the O(n) counter scans.
        self._predraw_after_id = self.after_idle(
            lambda: self._predraw(update_counters=False))

    # ------------------------------------------------------------------
    # Missing masters detection
    # ------------------------------------------------------------------

    def _find_plugin_in_mod_dir(self, mod_dir: "Path", filename: str) -> "Path | None":
        """Search mod_dir recursively (one level deep) for a file matching filename
        case-insensitively. Used when the filemap strips a prefix (e.g. 'Data Files')
        so the staging file lives in a subdirectory not reflected in rel_path."""
        from pathlib import Path as _Path
        name_lower = filename.lower()
        if not mod_dir.is_dir():
            return None
        for entry in mod_dir.iterdir():
            if entry.is_file() and entry.name.lower() == name_lower:
                return entry
            if entry.is_dir():
                candidate = entry / filename
                if candidate.is_file():
                    return candidate
                # case-insensitive check within subdir
                for sub in entry.iterdir():
                    if sub.is_file() and sub.name.lower() == name_lower:
                        return sub
        return None

    def _check_all_masters(self) -> None:
        """Build plugin_paths dict and check all plugins for missing/late masters.

        The expensive part is parsing each plugin file's header from disk
        (check_missing/late/version masters + ESL/master flags). That only needs
        to re-run when the *set of plugins* changes or a plugin *file* changes.

        Every mod toggle rewrites filemap.txt, so keying the cache on the filemap
        mtime (as before) meant a guaranteed miss on every toggle — re-parsing all
        plugin headers (150–1700 ms) even when no plugin was added/removed/edited.

        Now the cheap filemap scan that builds plugin_paths always runs (it must,
        to resolve current paths), but the header-parse phase is gated on
        (plugins_tuple, resolved-path mtimes, data_dir, staging). Toggling a
        plugin-less mod leaves all of those identical → the header parse is
        skipped. Adding/removing a plugin changes plugins_tuple; an xEdit save
        changes that file's mtime → both correctly invalidate.
        """
        if not self._plugin_entries or not self._plugin_extensions:
            self._missing_masters = {}
            self._late_masters = {}
            self._version_mismatch_masters = {}
            self._plugin_mod_map = {}
            self._master_flag_plugins = set()
            self._masters_cache_key = None
            return

        filemap_path_str = self._get_filemap_path()
        plugins_tuple = tuple((e.name, e.enabled) for e in self._plugin_entries)
        data_dir_str = str(self._data_dir) if self._data_dir else ""
        staging_str = str(self._staging_root) if self._staging_root else ""

        exts_lower = {ext.lower() for ext in self._plugin_extensions}
        plugin_paths: dict[str, Path] = {}
        plugin_mod_map: dict[str, str] = {}

        # 1. Map plugins from filemap.txt → staging mods (and overwrite)
        overwrite_dir = self._staging_root.parent / "overwrite" if self._staging_root else None
        if filemap_path_str and self._staging_root:
            for rel_path, mod_name in self._get_parsed_filemap(Path(filemap_path_str)):
                rel_path = rel_path.replace("\\", "/")
                if "/" in rel_path:
                    continue
                if Path(rel_path).suffix.lower() in exts_lower:
                    if mod_name == _OVERWRITE_NAME and overwrite_dir:
                        plugin_paths[rel_path.lower()] = overwrite_dir / rel_path
                    else:
                        direct = self._staging_root / mod_name / rel_path
                        if direct.is_file():
                            plugin_paths[rel_path.lower()] = direct
                        else:
                            # File may live under a strip-prefix subfolder in staging
                            # (e.g. staging/mod/Data Files/plugin.esp).
                            # Search the mod dir for a matching filename.
                            found = self._find_plugin_in_mod_dir(
                                self._staging_root / mod_name, rel_path
                            )
                            plugin_paths[rel_path.lower()] = found or direct
                    # Map plugin filename → mod folder name
                    plugin_mod_map[rel_path.lower()] = mod_name

        # 2. Plugins in overwrite that may not be in filemap yet (added by sync, index stale)
        if overwrite_dir and overwrite_dir.is_dir():
            for entry in overwrite_dir.iterdir():
                if entry.is_file() and entry.suffix.lower() in exts_lower:
                    low = entry.name.lower()
                    if low not in plugin_paths:
                        plugin_paths[low] = entry
                        plugin_mod_map[entry.name.lower()] = _OVERWRITE_NAME
            data_sub = overwrite_dir / "Data"
            if data_sub.is_dir():
                for entry in data_sub.iterdir():
                    if entry.is_file() and entry.suffix.lower() in exts_lower:
                        low = entry.name.lower()
                        if low not in plugin_paths:
                            plugin_paths[low] = entry
                            plugin_mod_map[entry.name.lower()] = _OVERWRITE_NAME

        # 3. Also map vanilla plugins from the game Data dir
        if self._data_dir and self._data_dir.is_dir():
            vanilla_dir = self._data_dir.parent / (self._data_dir.name + "_Core")
            scan_dir = vanilla_dir if vanilla_dir.is_dir() else self._data_dir
            for entry in scan_dir.iterdir():
                if entry.is_file() and entry.suffix.lower() in exts_lower:
                    plugin_paths.setdefault(entry.name.lower(), entry)

        self._plugin_mod_map = plugin_mod_map
        self._plugin_paths = plugin_paths

        # Gate the expensive header-parse phase on the inputs that actually
        # affect it: the enabled-plugin set/order, the resolved plugin-file
        # paths AND their mtimes (so an xEdit save invalidates), and the dirs.
        # The filemap mtime is deliberately NOT part of this key — it changes on
        # every mod toggle but does not imply any plugin file changed.
        path_fps: list[tuple[str, str, float]] = []
        for low, p in plugin_paths.items():
            try:
                path_fps.append((low, str(p), p.stat().st_mtime))
            except OSError:
                path_fps.append((low, str(p), -1.0))
        path_fps.sort()
        cache_key = (plugins_tuple, tuple(path_fps), data_dir_str, staging_str)
        if cache_key == self._masters_cache_key:
            return  # Plugin set + files unchanged — skip the header re-parse.

        plugin_names = [e.name for e in self._plugin_entries if e.enabled]
        self._missing_masters = check_missing_masters(plugin_names, plugin_paths)
        self._late_masters = check_late_masters(plugin_names, plugin_paths)
        if self._data_dir:
            self._version_mismatch_masters = check_version_mismatched_masters(
                plugin_names, plugin_paths, self._data_dir
            )
        else:
            self._version_mismatch_masters = {}
        self._load_esl_flags(plugin_paths)
        self._load_master_flags(plugin_paths)
        self._masters_cache_key = cache_key
        # BOS/SP scan: run off main thread to keep UI responsive
        # trigger a redraw via after().
        import threading as _threading
        _threading.Thread(
            target=self._scan_bos_sp_patches, daemon=True
        ).start()


    # ------------------------------------------------------------------
    # BOS / SkyPatcher patch detection
    # ------------------------------------------------------------------

    def _scan_bos_sp_patches(self) -> None:
        """Scan staging mods for BOS/SkyPatcher patches (background thread)."""
        try:
            self._do_scan_bos_sp()
        except Exception as exc:
            import traceback
            traceback.print_exc()

    def _do_scan_bos_sp(self) -> None:
        if self._staging_root is None:
            return

        staging_str = str(self._staging_root)
        try:
            total_mtime = sum(
                d.stat().st_mtime
                for d in self._staging_root.iterdir()
                if d.is_dir()
            )
        except OSError:
            total_mtime = 0.0
        cache_key = (total_mtime, staging_str)
        if cache_key == self._bos_sp_cache_key:
            return

        all_plugins: set[str] = set()   # all plugin basenames (lowercase) found in staging
        bos_stems: set[str] = set()     # plugin stems (lowercase) with a _SWAP.ini
        sp_plugins: set[str] = set()    # plugin names (lowercase) in filterByFormID entries

        try:
            for mod_dir in self._staging_root.iterdir():
                if not mod_dir.is_dir():
                    continue

                search_roots = [mod_dir]
                data_sub = mod_dir / "Data"
                if data_sub.is_dir():
                    search_roots.append(data_sub)

                # Collect plugin basenames
                for root in search_roots:
                    try:
                        for f in root.iterdir():
                            if f.is_file() and f.suffix.lower() in {".esp", ".esm", ".esl"}:
                                all_plugins.add(f.name.lower())
                    except OSError:
                        pass

                # BOS: any <stem>_SWAP.ini anywhere under the mod
                for root in search_roots:
                    try:
                        for f in root.rglob("*.ini"):
                            if f.is_file() and f.name.lower().endswith("_swap.ini"):
                                bos_stems.add(f.name.lower()[:-len("_swap.ini")])
                    except OSError:
                        pass

                # SP: parse filterByFormID lines in SkyPatcher/SkyPatcher2 INIs.
                # Each entry: filterByFormID = Plugin.esp|FormID
                # A patch mod targeting another mod's plugin lives here, so scan
                # all mods — not just the mod that owns the plugin.
                for sp_dir_name in ("SkyPatcher", "SkyPatcher2"):
                    sp_dir = mod_dir / "SKSE" / "Plugins" / sp_dir_name
                    if not sp_dir.is_dir():
                        continue
                    try:
                        for ini in sp_dir.rglob("*.ini"):
                            if not ini.is_file():
                                continue
                            try:
                                for line in ini.read_text(
                                    encoding="utf-8", errors="ignore"
                                ).splitlines():
                                    s = line.strip()
                                    if not s or s.startswith(";"):
                                        continue
                                    if s.lower().startswith("filterbyformid"):
                                        eq = s.find("=")
                                        if eq == -1:
                                            continue
                                        val = s[eq + 1:].strip()
                                        if "|" in val:
                                            ref = val.split("|")[0].strip().lower()
                                            if ref.endswith((".esp", ".esm", ".esl")):
                                                sp_plugins.add(ref)
                            except (OSError, UnicodeDecodeError):
                                pass
                    except OSError:
                        pass

        except OSError:
            pass

        result: dict[str, str] = {}
        for pname in all_plugins:
            is_bos = Path(pname).stem.lower() in bos_stems
            is_sp = pname in sp_plugins
            if is_bos and is_sp:
                result[pname] = "both"
            elif is_bos:
                result[pname] = "bos"
            elif is_sp:
                result[pname] = "sp"

        self._bos_sp_plugins = result
        self._bos_sp_cache_key = cache_key
        try:
            self.after(0, self._predraw)
        except Exception:
            pass


    # ------------------------------------------------------------------
    # ESL flag helpers
    # ------------------------------------------------------------------

    def _load_master_flags(self, plugin_paths: "dict[str, Path]") -> None:
        """Populate _master_flag_plugins (master header bit on non-.esm/.esl files)."""
        if not self._master_block_enabled():
            self._master_flag_plugins = set()
            return
        flagged: set[str] = set()
        cache: dict = getattr(self, "_master_flag_cache", {})
        self._master_flag_cache = cache
        for entry in self._plugin_entries:
            name_lower = entry.name.lower()
            if name_lower.endswith((".esm", ".esl")):
                continue  # master by extension — no header read needed
            path = plugin_paths.get(name_lower)
            if path is None:
                continue
            try:
                st = os.stat(str(path))
            except OSError:
                continue
            stat_key = (str(path), st.st_mtime_ns, st.st_size)
            flag_val = cache.get(stat_key)
            if flag_val is None:
                try:
                    flag_val = bool(is_master_flagged(path))
                except Exception:
                    flag_val = False
                cache[stat_key] = flag_val
            if flag_val:
                flagged.add(name_lower)
        self._master_flag_plugins = flagged

    def _load_esl_flags(self, plugin_paths: "dict[str, Path]") -> None:
        """Populate _esl_flagged_plugins / _esl_safe_plugins / _esl_unsafe_plugins
        from the plugin files on disk.

        Only .esp and .esm files are checked for the ESL flag — .esl files are
        always light by extension and handled separately by the engine.
        ESL eligibility (safe/unsafe) is only checked for .esp/.esm files.

        This runs on every _check_all_masters call (plugin toggle, reorder,
        etc.), so two optimisations matter:

        * Skip entirely for games that don't support the ESL flag.
        * Cache eligibility results by (path, mtime_ns, size).  The flag-bit
          check is cheap (12-byte read); the full-file record scan for
          eligibility is not, and its result only changes when the plugin
          file itself is rewritten.
        """
        # Gate on the game capability — no point scanning Fallout 3 /
        # Oblivion / Morrowind plugins for an ESL flag that doesn't exist.
        if not getattr(self._game, "supports_esl_flag", False):
            self._esl_flagged_plugins = set()
            self._esl_safe_plugins = set()
            self._esl_unsafe_plugins = set()
            return

        game_type_attr = getattr(self._game, "loot_game_type", "") or ""

        flagged: set[str] = set()
        safe: set[str] = set()
        unsafe: set[str] = set()
        cache = self._esl_eligible_cache
        flag_cache: dict = getattr(self, "_esl_flag_cache", {})
        self._esl_flag_cache = flag_cache
        for entry in self._plugin_entries:
            name_lower = entry.name.lower()
            # .esl files are always treated as light by the game engine
            if name_lower.endswith(".esl"):
                flagged.add(name_lower)
                continue
            path = plugin_paths.get(name_lower)
            if path is None:
                continue
            try:
                st = os.stat(str(path))
            except OSError:
                continue
            stat_key = (str(path), st.st_mtime_ns, st.st_size)
            flag_val = flag_cache.get(stat_key)
            if flag_val is None:
                try:
                    flag_val = bool(is_esl_flagged(path))
                except Exception:
                    flag_val = False
                flag_cache[stat_key] = flag_val
            if flag_val:
                flagged.add(name_lower)
            # Only check ESL eligibility for ESP/ESM files (not already ESL).
            # Cache key includes game_type + a version tag so switching games or
            # upgrading the eligibility algorithm invalidates old entries.
            if not name_lower.endswith((".esp", ".esm")):
                continue
            elig_key = (stat_key, game_type_attr, _ESL_ELIG_CACHE_VERSION)
            cached = cache.get(elig_key)
            if cached is None:
                try:
                    cached = check_esl_eligible(path, game_type_attr)
                except Exception:
                    cached = False
                cache[elig_key] = cached
            if cached:
                safe.add(name_lower)
            else:
                unsafe.add(name_lower)
        self._esl_flagged_plugins = flagged
        self._esl_safe_plugins = safe
        self._esl_unsafe_plugins = unsafe

    def _update_row_bg(self, data_row: int) -> None:
        """Update just the background colour of a single data row's pool slot."""
        fi = self._plugin_filtered_indices
        # Determine visual (view) row for alternating colour
        if fi is not None:
            if self._fi_reverse_src is not fi:
                self._fi_reverse_src = fi
                self._fi_reverse_cache = {di: vr for vr, di in enumerate(fi)}
            view_row = self._fi_reverse_cache.get(data_row, data_row)
        else:
            view_row = data_row
        for s in range(self._pool_size):
            if self._pool_data_idx[s] == data_row:
                entry = self._plugin_entries[data_row] if data_row < len(self._plugin_entries) else None
                is_sel = data_row in self._psel_set
                name_lower = entry.name.lower() if entry else ""
                if is_sel:
                    bg = BG_SELECT
                elif entry and name_lower in self._master_names_lower():
                    bg = BG_GREEN_ROW
                elif entry and name_lower in self._highlighted_plugins:
                    bg = _theme.plugin_mod
                elif entry and name_lower in self._bsa_conflict_higher_plugins:
                    bg = _theme.conflict_higher
                elif entry and name_lower in self._bsa_conflict_lower_plugins:
                    bg = _theme.conflict_lower
                elif data_row == self._phover_idx:
                    bg = BG_HOVER_ROW
                else:
                    bg = BG_ROW if view_row % 2 == 0 else BG_ROW_ALT
                self._pcanvas.itemconfigure(self._pool_bg[s], fill=bg)
                if entry is not None:
                    _theme_bgs = (_theme.conflict_higher, _theme.conflict_lower, _theme.plugin_mod)
                    if entry.name in self._missing_masters:
                        name_color = STATUS_BADGE_RED
                    elif bg in _theme_bgs:
                        name_color = _theme.contrasting_text_color(bg)
                    elif not entry.enabled:
                        name_color = TEXT_DIM
                    else:
                        name_color = TEXT_MAIN
                    self._pcanvas.itemconfigure(self._pool_name[s], fill=name_color)
                    # Re-tint the unchecked checkbox/lock backgrounds too, or the
                    # old row colour lingers as a square under the new bg.
                    if not entry.enabled:
                        self._pcanvas.itemconfigure(self._pool_check_rects[s], fill=bg)
                    if not self._plugin_locks.get(entry.name, False):
                        self._pcanvas.itemconfigure(self._pool_lock_rects[s], fill=bg)
                # Partial repaint — force a full refresh of this slot next _predraw.
                self._pool_last_state[s] = None
                break

    def _on_pmouse_motion(self, event) -> None:
        """Show tooltip when hovering over a warning icon in the Flags column, and update hover highlight."""
        canvas_y = int(self._pcanvas.canvasy(event.y))
        row = canvas_y // self.ROW_H
        fi = self._plugin_filtered_indices
        view_len = len(fi) if fi is not None else len(self._plugin_entries)
        if row < 0 or row >= view_len:
            self._tooltip.hide()
            if self._phover_idx != -1:
                old = self._phover_idx
                self._phover_idx = -1
                self._update_row_bg(old)
            return

        actual_idx = fi[row] if fi is not None else row
        if actual_idx != self._phover_idx:
            old = self._phover_idx
            self._phover_idx = actual_idx
            if old >= 0:
                self._update_row_bg(old)
            self._update_row_bg(actual_idx)

        x = event.x
        if len(self._pcol_x) >= 5 and self._pcol_x[2] <= x < self._pcol_x[3]:
            entry = self._plugin_entries[actual_idx]
            missing = self._missing_masters.get(entry.name)
            late = self._late_masters.get(entry.name)
            vmm = self._version_mismatch_masters.get(entry.name)
            parts: list[str] = []
            if missing:
                parts.append("Missing masters:\n" + "\n".join(f"  - {m}" for m in missing))
            if late:
                parts.append("Masters loaded after this plugin:\n" + "\n".join(f"  - {m}" for m in late))
            if vmm:
                parts.append("Version mismatched masters:\n" + "\n".join(f"  - {m}" for m in vmm))
            if entry.name.lower() in self._userlist_plugins:
                if entry.name.lower() in self._userlist_cycle_plugins:
                    ul_msg = "This plugin has a broken cycle, Right click > Show cycle for info"
                else:
                    ul_msg = "This plugin is managed by userlist.yaml"
                    grp = self._plugin_group_map.get(entry.name.lower())
                    if grp:
                        ul_msg += f"\nGroup: {grp}"
                parts.append(ul_msg)
            if entry.name.lower() in self._esl_flagged_plugins:
                parts.append("This plugin is marked as Light (ESL)")
            loot_info = self._loot_info.get(entry.name.lower())
            if loot_info:
                loot_text = self._format_loot_tooltip(loot_info)
                if loot_text:
                    parts.append(loot_text)
            if parts:
                text = "\n\n".join(parts)
                # TkTooltip.show() is idempotent for the same text — no stale check needed.
                self._tooltip.show(event.x_root, event.y_root, text)
                return

        self._tooltip.hide()

    def _on_pmouse_leave(self, event) -> None:
        self._tooltip.hide()
        if self._phover_idx != -1:
            old = self._phover_idx
            self._phover_idx = -1
            self._update_row_bg(old)

    # ------------------------------------------------------------------
    # Scroll events
    # ------------------------------------------------------------------

    def _on_pcanvas_resize(self, event):
        self._pcanvas_w = event.width
        if hasattr(self, '_pcanvas_resize_after_id') and self._pcanvas_resize_after_id:
            self.after_cancel(self._pcanvas_resize_after_id)
        self._pcanvas_resize_after_id = self.after_idle(lambda w=event.width: self._apply_pcanvas_resize(w))

    def _apply_pcanvas_resize(self, width: int):
        self._layout_plugin_cols(width)
        self._update_plugin_header(width)
        _clear_truncate_cache()
        self._schedule_predraw()

    def _on_pscroll_up(self, _event):
        if LEGACY_WHEEL_REDUNDANT:
            return
        self._pcanvas.yview("scroll", -50, "units")
        self._schedule_predraw()

    def _on_pscroll_down(self, _event):
        if LEGACY_WHEEL_REDUNDANT:
            return
        self._pcanvas.yview("scroll", 50, "units")
        self._schedule_predraw()

    def _on_pmousewheel(self, event):
        self._pcanvas.yview("scroll", -50 if event.delta > 0 else 50, "units")
        self._schedule_predraw()

    # ------------------------------------------------------------------
    # Mouse events (select + drag)
    # ------------------------------------------------------------------

    def _pevent_canvas_y(self, event) -> int:
        return int(self._pcanvas.canvasy(event.y))

    def _pcanvas_y_to_index(self, canvas_y: int) -> int:
        """Return the _plugin_entries index for the given canvas y position."""
        if not self._plugin_entries:
            return 0
        row = int(canvas_y // self.ROW_H)
        if self._plugin_filtered_indices is not None:
            fi = self._plugin_filtered_indices
            row = max(0, min(row, len(fi) - 1))
            return fi[row] if fi else 0
        return max(0, min(row, len(self._plugin_entries) - 1))

    def _is_plugin_locked(self, idx: int) -> bool:
        """Return True if the plugin at idx is vanilla or user-locked (immovable)."""
        if 0 <= idx < len(self._plugin_entries):
            entry = self._plugin_entries[idx]
            if entry.name.lower() in self._vanilla_plugins:
                return True
            return bool(self._plugin_locks.get(entry.name, False))
        return False

    def _on_pmouse_press(self, event):
        try:
            self.winfo_toplevel()._last_list_panel = "plugin"
        except Exception:
            pass
        if not self._plugin_entries:
            return
        cy = self._pevent_canvas_y(event)
        idx = self._pcanvas_y_to_index(cy)
        shift = bool(event.state & 0x1)

        # Shift+click: extend selection from anchor
        if shift and self._sel_idx >= 0:
            lo, hi = sorted((self._sel_idx, idx))
            if self._plugin_filtered_indices is not None:
                self._psel_set = {i for i in self._plugin_filtered_indices if lo <= i <= hi}
            else:
                self._psel_set = set(range(lo, hi + 1))
            self._predraw()
            return

        # If clicking inside an existing multi-selection, preserve it so the
        # user can drag the whole group — collapse to single only on release.
        # Don't initiate drag if the clicked entry is locked.
        if idx in self._psel_set and len(self._psel_set) > 1:
            if not self._is_plugin_locked(idx):
                self._drag_idx = idx
                self._drag_start_y = cy
                self._drag_moved = False
                self._drag_slot = -1
            return

        self._sel_idx = idx
        self._psel_set = {idx}
        if self._on_plugin_row_selected_cb is not None and 0 <= idx < len(self._plugin_entries):
            self._on_plugin_row_selected_cb(self._plugin_entries[idx].name)
        # Only allow drag start if not locked and no search filter active
        if not self._is_plugin_locked(idx) and self._plugin_filtered_indices is None:
            self._drag_idx = idx
            self._drag_start_y = cy
        else:
            self._drag_idx = -1
            self._drag_start_y = 0
        self._drag_moved = False
        self._drag_slot = -1
        self._highlighted_plugins = set()  # clear mod→plugin highlight when selecting a plugin
        self._bsa_conflict_higher_plugins = set()
        self._bsa_conflict_lower_plugins = set()
        self._highlighted_ini_mod = None
        self._apply_ini_row_highlight()
        self._draw_ini_marker_strip()
        plugin_name = self._plugin_entries[idx].name
        # Highlight masters of the selected plugin in green
        plugin_key = plugin_name.lower()
        plugin_path = self._plugin_paths.get(plugin_key)
        if plugin_path is not None:
            masters = read_masters(plugin_path)
            plugin_names_lower = {e.name.lower() for e in self._plugin_entries}
            self._master_highlights = {m for m in masters if m.lower() in plugin_names_lower}
        else:
            self._master_highlights = set()
        self._predraw()
        if self._on_mod_selected_cb is not None:
            self._on_mod_selected_cb()
        if self._on_plugin_selected_cb is not None:
            mod_name = self._plugin_mod_map.get(plugin_name.lower())
            self._on_plugin_selected_cb(mod_name)

    _PDRAG_SCROLL_ZONE = 40     # pixels from edge to trigger auto-scroll
    _PDRAG_SCROLL_INTERVAL = 50  # ms between scroll ticks

    def _maybe_start_pdrag_autoscroll(self):
        """Start or continue auto-scrolling if cursor is near the canvas edge."""
        if self._drag_idx < 0:
            self._cancel_pdrag_autoscroll()
            return
        h = self._pcanvas.winfo_height()
        y = self._pdrag_last_event_y
        zone = self._PDRAG_SCROLL_ZONE
        if y < zone:
            speed = max(1, int(6 * (1.0 - y / zone)))
            self._pcanvas.yview("scroll", -speed, "units")
            self._predraw()
            self._cancel_pdrag_autoscroll()
            self._pdrag_scroll_after = self._pcanvas.after(
                self._PDRAG_SCROLL_INTERVAL, self._maybe_start_pdrag_autoscroll)
        elif y > h - zone:
            speed = max(1, int(6 * (1.0 - (h - y) / zone)))
            self._pcanvas.yview("scroll", speed, "units")
            self._predraw()
            self._cancel_pdrag_autoscroll()
            self._pdrag_scroll_after = self._pcanvas.after(
                self._PDRAG_SCROLL_INTERVAL, self._maybe_start_pdrag_autoscroll)
        else:
            self._cancel_pdrag_autoscroll()

    def _cancel_pdrag_autoscroll(self):
        if self._pdrag_scroll_after is not None:
            self._pcanvas.after_cancel(self._pdrag_scroll_after)
            self._pdrag_scroll_after = None

    def _clamp_drag_slot(self, slot: int, indices: "list[int]", blk_size: int) -> int:
        """Keep a drag inside its master/non-master block (engine parity)."""
        if not self._master_block_enabled():
            return slot
        groups = {self._is_master_group(self._plugin_entries[i].name) for i in indices}
        if len(groups) != 1:
            return slot  # mixed selection — _save_plugins re-partitions on release
        boundary = sum(1 for e in self._plugin_entries if self._is_master_group(e.name))
        if True in groups:
            return min(slot, max(boundary - blk_size, 0))
        return max(slot, boundary)

    def _on_pmouse_drag(self, event):
        if self._drag_idx < 0 or not self._plugin_entries:
            return

        # Auto-scroll near edges (with repeating timer)
        self._pdrag_last_event_y = event.y
        self._maybe_start_pdrag_autoscroll()

        cy = self._pevent_canvas_y(event)
        n = len(self._plugin_entries)

        if len(self._psel_set) > 1 and self._drag_idx in self._psel_set:
            sorted_sel = sorted(
                i for i in self._psel_set if not self._is_plugin_locked(i)
            )
            if not sorted_sel:
                return
            blk_size = len(sorted_sel)
            slot = max(0, min(int(cy // self.ROW_H), n - blk_size))
            slot = self._clamp_drag_slot(slot, sorted_sel, blk_size)

            if slot == self._drag_slot:
                self._predraw()
                return
            self._drag_slot = slot
            self._drag_moved = True

            extracted = []
            for i in sorted(sorted_sel, reverse=True):
                extracted.insert(0, self._plugin_entries.pop(i))

            insert_at = max(0, min(slot, len(self._plugin_entries)))
            for j, entry in enumerate(extracted):
                self._plugin_entries.insert(insert_at + j, entry)

            self._drag_idx = insert_at
            self._sel_idx = insert_at
            self._psel_set = set(range(insert_at, insert_at + blk_size))
        else:
            slot = max(0, min(int(cy // self.ROW_H), n - 1))
            slot = self._clamp_drag_slot(slot, [self._drag_idx], 1)

            if slot == self._drag_slot:
                return
            self._drag_slot = slot
            self._drag_moved = True

            entry = self._plugin_entries.pop(self._drag_idx)
            insert_at = max(0, min(slot, len(self._plugin_entries)))
            self._plugin_entries.insert(insert_at, entry)

            self._drag_idx = insert_at
            self._sel_idx = insert_at
            self._psel_set = {insert_at}

        self._predraw()

    def _on_plugin_right_click(self, event):
        """Show context menu for plugin panel."""
        if not self._plugin_entries:
            return
        cy = self._pevent_canvas_y(event)
        idx = self._pcanvas_y_to_index(cy)

        # If right-clicking outside current selection, select the clicked item
        if idx not in self._psel_set:
            self._sel_idx = idx
            self._psel_set = {idx}
            self._predraw()

        # Collect toggleable plugins in selection (non-vanilla)
        toggleable = [
            i for i in sorted(self._psel_set)
            if 0 <= i < len(self._plugin_entries)
            and self._plugin_entries[i].name.lower() not in self._vanilla_plugins
        ]
        if not toggleable:
            return

        self._show_plugin_context_menu(event.x_root, event.y_root, toggleable)

    def _show_plugin_context_menu(self, x: int, y: int, toggleable: list[int]):
        """Custom popup context menu for the plugin panel."""
        popup = tk.Toplevel(self._pcanvas)
        popup.wm_overrideredirect(True)
        popup.wm_geometry(f"+{x}+{y}")
        popup.configure(bg=BORDER)

        _alive = [True]

        def _dismiss(_event=None):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()

        def _pick(cmd):
            if _alive[0]:
                _alive[0] = False
                popup.destroy()
                cmd()

        inner = tk.Frame(popup, bg=BG_PANEL, bd=0)
        inner.pack(padx=1, pady=1)

        count = len(toggleable)
        items = []

        # Check if the current game supports the ESL flag
        _game_supports_esl = getattr(self._game, "supports_esl_flag", False)

        if count == 1:
            items.append(("Enable plugin",
                           lambda idxs=toggleable: self._enable_selected_plugins(idxs)))
            items.append(("Disable plugin",
                           lambda idxs=toggleable: self._disable_selected_plugins(idxs)))
            plugin_name = self._plugin_entries[toggleable[0]].name
            plugin_idx = toggleable[0]
            # BOS/SP quick-disable
            _bos_sp_kind = self._bos_sp_plugins.get(plugin_name.lower(), "")
            if _bos_sp_kind:
                _bos_sp_desc = {"bos": "BOS", "sp": "SkyPatcher", "both": "BOS+SkyPatcher"}.get(_bos_sp_kind, "BOS/SP")
                items.append((f"Disable — {_bos_sp_desc} patch replaces it",
                               lambda idxs=[plugin_idx]: self._disable_selected_plugins(idxs)))
            # ESL flag toggle — only for .esp/.esm; .esl files are always light
            if _game_supports_esl and not plugin_name.lower().endswith(".esl"):
                is_esl = plugin_name.lower() in self._esl_flagged_plugins
                if is_esl:
                    items.append(("Remove ESL flag (un-light)",
                                   lambda idxs=toggleable: self._toggle_esl_flag(idxs, False)))
                else:
                    path = self._plugin_paths.get(plugin_name.lower())
                    _game_type = getattr(self._game, "loot_game_type", "") or ""
                    if path and path.is_file():
                        _eligible = check_esl_eligible(path, _game_type)
                    else:
                        _eligible = False
                    if _eligible:
                        items.append(("Mark as Light (ESL)",
                                       lambda idxs=toggleable: self._toggle_esl_flag(idxs, True)))
                    else:
                        items.append(("Not ESL-safe (per LOOT — compact in xEdit first)", None))
            if plugin_name.lower() not in self._userlist_plugins:
                items.append(("Add to userlist...",
                               lambda n=plugin_name, i=plugin_idx: self._add_plugin_to_userlist(n, i)))
            items.append(("Add to group...",
                           lambda n=plugin_name: self._add_plugins_to_group([n])))
            if plugin_name.lower() in self._userlist_plugins:
                items.append(("Remove from userlist",
                               lambda n=plugin_name: self._remove_plugin_from_userlist(n)))
            if plugin_name.lower() in self._userlist_cycle_plugins:
                items.append(("Show cycle...",
                               lambda n=plugin_name: self._open_plugin_cycle_overlay(n)))
            elif plugin_name.lower() in self._userlist_plugins:
                items.append(("Show userlist rules...",
                               lambda n=plugin_name: self._open_plugin_cycle_overlay(n)))

            # Record overlap — full libloot load, runs in the background.
            if getattr(self._game, "loot_sort_enabled", False):
                items.append(("Show overlapping plugins...",
                               lambda n=plugin_name: self._show_overlapping_plugins(n)))

            # LOOT locations (mod page / author links from the masterlist)
            loot_locs = (self._loot_info.get(plugin_name.lower(), {}).get("locations") or [])
            for loc in loot_locs:
                url = loc.get("url", "")
                if not url:
                    continue
                label_name = loc.get("name") or url
                items.append((f"Open: {label_name}",
                               lambda u=url: open_url(u)))
        else:
            items.append((f"Enable selected ({count})",
                           lambda idxs=toggleable: self._enable_selected_plugins(idxs)))
            items.append((f"Disable selected ({count})",
                           lambda idxs=toggleable: self._disable_selected_plugins(idxs)))
            names = [self._plugin_entries[i].name for i in toggleable]
            # BOS/SP quick-disable for multi-select
            _bos_sp_idxs = [i for i in toggleable
                            if self._bos_sp_plugins.get(self._plugin_entries[i].name.lower(), "")]
            if _bos_sp_idxs:
                items.append((f"Disable {len(_bos_sp_idxs)} BOS/SP-patched (safe to disable)",
                               lambda idxs=_bos_sp_idxs: self._disable_selected_plugins(idxs)))
            # ESL flag toggle for multiple plugins — skip pure .esl files
            if _game_supports_esl:
                esl_eligible_idxs = [
                    i for i in toggleable
                    if not self._plugin_entries[i].name.lower().endswith(".esl")
                ]
                if esl_eligible_idxs:
                    already_esl = [
                        i for i in esl_eligible_idxs
                        if self._plugin_entries[i].name.lower() in self._esl_flagged_plugins
                    ]
                    not_esl_raw = [
                        i for i in esl_eligible_idxs
                        if self._plugin_entries[i].name.lower() not in self._esl_flagged_plugins
                    ]
                    not_esl = []
                    ineligible_count = 0
                    _game_type = getattr(self._game, "loot_game_type", "") or ""
                    for _i in not_esl_raw:
                        _p = self._plugin_paths.get(self._plugin_entries[_i].name.lower())
                        if _p and _p.is_file():
                            if check_esl_eligible(_p, _game_type):
                                not_esl.append(_i)
                            else:
                                ineligible_count += 1
                        else:
                            ineligible_count += 1
                    if not_esl:
                        _suffix = f" ({ineligible_count} ineligible skipped)" if ineligible_count else ""
                        items.append((f"Mark selected as Light (ESL) ({len(not_esl)}){_suffix}",
                                       lambda idxs=not_esl: self._toggle_esl_flag(idxs, True)))
                    elif ineligible_count:
                        items.append((f"Mark as Light (ESL) — none eligible ({ineligible_count} need xEdit compact)", None))
                    if already_esl:
                        items.append((f"Remove ESL flag from selected ({len(already_esl)})",
                                       lambda idxs=already_esl: self._toggle_esl_flag(idxs, False)))
            items.append(("Add selected to group...",
                           lambda ns=names: self._add_plugins_to_group(ns)))
            if any(n.lower() in self._userlist_plugins for n in names):
                items.append(("Remove selected from userlist",
                               lambda ns=names: self._remove_plugins_from_userlist(ns)))

        for label, cmd in items:
            if cmd is None:
                btn = tk.Label(
                    inner, text=label, anchor="w",
                    bg=BG_PANEL, fg=TEXT_DIM,
                    font=(_theme.FONT_FAMILY, _theme.FS11),
                    padx=12, pady=5,
                )
                btn.pack(fill="x")
            else:
                btn = tk.Label(
                    inner, text=label, anchor="w",
                    bg=BG_PANEL, fg=TEXT_MAIN,
                    font=(_theme.FONT_FAMILY, _theme.FS11),
                    padx=12, pady=5, cursor="hand2",
                )
                btn.pack(fill="x")
                btn.bind("<ButtonRelease-1>", lambda _e, c=cmd: _pick(c))
                btn.bind("<Enter>", lambda _e, b=btn: b.configure(bg=BG_SELECT))
                btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=BG_PANEL))

        popup.update_idletasks()
        popup.bind("<Escape>", _dismiss)

        def _on_press(event):
            if not _alive[0]:
                return
            wx, wy = popup.winfo_rootx(), popup.winfo_rooty()
            ww, wh = popup.winfo_width(), popup.winfo_height()
            if not (wx <= event.x_root <= wx + ww and wy <= event.y_root <= wy + wh):
                _dismiss()
        popup.bind_all("<ButtonPress-1>", _on_press)
        popup.bind_all("<ButtonPress-3>", _on_press)

    def _enable_selected_plugins(self, indices: list[int]):
        """Enable all plugins at the given indices."""
        for i in indices:
            if 0 <= i < len(self._plugin_entries):
                self._plugin_entries[i].enabled = True
        self._save_plugins()
        self._check_all_masters()
        self._predraw()

    def _disable_selected_plugins(self, indices: list[int]):
        """Disable all plugins at the given indices."""
        for i in indices:
            if 0 <= i < len(self._plugin_entries):
                self._plugin_entries[i].enabled = False
        self._save_plugins()
        self._check_all_masters()
        self._predraw()

    def _toggle_esl_flag(self, indices: list[int], enable: bool) -> None:
        """Set or clear the ESL (light plugin) flag for the plugins at *indices*.

        For each plugin:
        * Skips .esl files (always light by extension — no header change needed).
        * Skips plugins whose file path is unknown.
        * When *enable* is True, first asks libloot whether the plugin is
          ESL-eligible; skips any plugin libloot rejects.
        """
        changed = 0
        game_type_attr = getattr(self._game, "loot_game_type", "") or ""
        for i in indices:
            if not (0 <= i < len(self._plugin_entries)):
                continue
            name = self._plugin_entries[i].name
            name_lower = name.lower()
            if name_lower.endswith(".esl"):
                continue  # .esl files are always light — nothing to toggle
            path = self._plugin_paths.get(name_lower)
            if path is None or not path.is_file():
                self._log(f"  ESL: cannot find file for {name} — skipped.")
                continue
            if enable:
                if not check_esl_eligible(path, game_type_attr):
                    self._log(f"  ESL: skipped {name} — not eligible per LOOT (compact in xEdit first).")
                    continue
            ok = set_esl_flag(path, enable)
            if ok:
                changed += 1
            else:
                self._log(f"  ESL: failed to write {name} — file may be read-only.")

        action = "set" if enable else "cleared"
        if changed:
            self._log(f"ESL flag {action} for {changed} plugin(s).")
        if changed:
            # set_esl_flag rewrote the plugin file — invalidate the _check_all_masters
            # cache so _load_esl_flags re-runs (the cache key doesn't include plugin
            # file mtimes, only filemap mtime + plugin list).
            self._masters_cache_key = None
            self._check_all_masters()
            self._predraw()

    def _on_pmouse_release(self, event):
        self._cancel_pdrag_autoscroll()
        if self._drag_idx >= 0 and self._drag_moved:
            self._save_plugins()
            self._check_all_masters()
        elif self._drag_idx >= 0 and not self._drag_moved and len(self._psel_set) > 1:
            # Click (no drag) inside multi-selection — collapse to the clicked item
            cy = self._pevent_canvas_y(event)
            clicked = self._pcanvas_y_to_index(cy)
            if clicked in self._psel_set:
                self._sel_idx = clicked
                self._psel_set = {clicked}
        self._drag_idx = -1
        self._drag_moved = False
        self._drag_slot = -1
        self._predraw()

    # ------------------------------------------------------------------
    # Mod note editor overlay
    # ------------------------------------------------------------------

    def show_notes_editor(self, mod_name: str, initial_text: str,
                          on_save, on_remove) -> None:
        """Show the mod-note editor over this panel. Replaces any existing overlay."""
        from gui.mod_note_overlay import ModNoteOverlay

        if self._notes_overlay is not None:
            try:
                self._notes_overlay.destroy()
            except tk.TclError:
                pass
            self._notes_overlay = None

        def _close():
            if self._notes_overlay is not None:
                try:
                    self._notes_overlay.destroy()
                except tk.TclError:
                    pass
                self._notes_overlay = None

        self._notes_overlay = ModNoteOverlay(
            self,
            mod_name=mod_name,
            initial_text=initial_text,
            on_save=on_save,
            on_remove=on_remove,
            on_close=_close,
        )
        self._notes_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._notes_overlay.lift()

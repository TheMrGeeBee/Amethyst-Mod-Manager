"""
Downloads panel — scans the user's Downloads folder for archive files
(.zip, .7z, .rar, .tar.gz, .tar) and displays them in a canvas list.

Right-clicking an entry triggers the standard mod-install flow as if the
user had clicked "Install Mod" and selected that file manually.

Users can add extra scan locations via the Locations button; paths are
saved to ~/.config/AmethystModManager/download_locations.json.
"""

import os
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
import gui.theme as _theme
from gui.ctk_components import CTkPopupMenu
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.tri_state_checkbox import TriStateCheckBox
from gui.download_locations_overlay import (
    DownloadLocationsOverlay,
    is_cache_default_disabled,
    is_default_downloads_disabled,
    load_extra_download_locations,
)
from gui.theme import (
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    BG_ROW,
    BG_ROW_ALT,
    BG_HOVER,
    BG_SEP,
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    TEXT_MAIN,
    TEXT_DIM,
    BORDER,
    FONT_HEADER,
    FONT_FAMILY,
    FS10,
    FS11,
    scaled,
    CTK_FS10,
    CTK_FS11,
 TK_FONT_NORMAL, TK_FONT_SMALL, TK_FONT_HEADER,
)

# Smaller, denser fonts to match the Plugins tab. Filenames use FS11
# (same as plugin names); size + install button use FS10 (same as plugin
# columns). Header label keeps FONT_HEADER.
FONT_NORMAL = (FONT_FAMILY, FS11)
FONT_SMALL  = (FONT_FAMILY, FS10)

ROW_H      = scaled(30)   # plugins-tab row density
BTN_COL_W  = scaled(80)   # px reserved on the right for the Install button
SIZE_COL_W = scaled(70)   # px reserved for the file-size text (left of button col)
CB_COL_W   = scaled(24)   # px reserved on the left for the per-row checkbox
CB_SIZE    = scaled(14)   # checkbox square edge length (fits the shorter row)
NAME_PAD_L = CB_COL_W + scaled(6)  # left padding for the filename text (after checkbox)
NAME_PAD_R = scaled(8)    # gap between filename text and size column
HEADER_ACTION_W = scaled(90)  # px reserved on the right of a section header for
                              # the "Select all" / "Deselect all" click target

_POOL_SIZE = 40  # pre-allocated canvas slots (covers ~40 visible rows)

# Archive extensions we care about (lowercase, with dot).
# .dazip / .override are Dragon Age package formats (renamed zips) — listed so
# they show up in the Downloads tab and can be installed like any other archive.
_ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tar.bz2", ".tar.xz",
                 ".dazip", ".override", ".fomod"}

from gui.text_utils import truncate_text_tk_call as _truncate_text_cached


@dataclass
class InstalledIndex:
    """Snapshot of every installed mod, used to mark archives in the
    Downloads tab as already-installed.

    Two parallel keys are kept:
      * ``names`` — exact match on the archive filename recorded in
        ``meta.ini`` ``installationFile``. Most reliable when the user
        installed via the Downloads tab itself.
      * ``mod_file_ids`` — ``(mod_id, file_id)`` pairs from ``meta.ini``.
        Used as a fallback when the on-disk filename differs from what
        was recorded (common with collection installs, which store the
        canonical Nexus ``file_name`` rather than the download name).
    """
    names: set[str] = field(default_factory=set)
    mod_file_ids: set[tuple[int, int]] = field(default_factory=set)

    def is_archive_installed(self, archive_name: str) -> bool:
        """True if this archive matches an installed mod by either key."""
        if archive_name in self.names:
            return True
        ids = _parse_archive_mod_file_ids(archive_name)
        if ids is not None and ids in self.mod_file_ids:
            return True
        return False


def _parse_archive_mod_file_ids(name: str) -> Optional[tuple[int, int]]:
    """Return ``(mod_id, file_id)`` parsed from a Nexus-style archive
    filename, or ``None`` if the name doesn't match the pattern.

    Nexus downloads end with ``-<mod_id>-<version_parts>-<file_id>``
    where every segment is numeric. We delegate to
    :func:`Nexus.nexus_meta.parse_nexus_filename` which strips the
    suffix off the filename stem and returns the mod_id plus the
    trailing numeric parts; the *last* of those parts is the file_id.
    """
    try:
        from Nexus.nexus_meta import parse_nexus_filename
    except Exception:
        return None
    # parse_nexus_filename takes the filename *stem* (no extension).
    stem = name
    for ext in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if stem.lower().endswith(ext):
            stem = stem[: -len(ext)]
            break
    else:
        i = stem.rfind(".")
        if i >= 0:
            stem = stem[:i]
    info = parse_nexus_filename(stem)
    if info is None or not info.version_parts:
        return None
    return (int(info.mod_id), int(info.version_parts[-1]))


@dataclass
class DownloadEntry:
    """One row in the Downloads list — either an archive or a section header.

    Section headers are synthetic dummy rows used to group archives by their
    source directory (default Downloads vs additional locations). They are
    rendered differently (centred, bold, no checkbox / size / install button)
    and ignored by all interaction handlers (motion, click, right-click).
    """
    is_section_header: bool = False
    section_name: str = ""
    path: Optional[Path] = None
    size_str: str = ""
    src_dir: Optional[Path] = None


class _RemoveArchivesConfirmDialog(ctk.CTkToplevel):
    """Modal CTk yes/no confirmation listing every archive being removed."""

    WIDTH  = 520
    HEIGHT = 460

    def __init__(self, parent, paths: list[Path]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Remove archives")
        # CTkToplevel scales geometry/minsize by window scaling itself.
        self.geometry(f"{self.WIDTH}x{self.HEIGHT}")
        self.minsize(self.WIDTH, self.HEIGHT)
        self.transient(parent)
        self.confirmed = False

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color="#a02d2d", corner_radius=0, height=40)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        ctk.CTkLabel(
            header, text=f"Remove {len(paths)} archive(s)?",
            font=(FONT_FAMILY, CTK_FS11, "bold"),
            text_color="white", anchor="w",
        ).pack(side="left", padx=12, pady=8)

        # Body
        body = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            body,
            text="The following archive(s) will be permanently deleted "
                 "from disk. This cannot be undone.",
            font=(FONT_FAMILY, CTK_FS11),
            text_color=TEXT_MAIN,
            anchor="nw", justify="left",
            wraplength=self.WIDTH - 40,
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))

        # Scrollable list of every archive
        list_frame = ctk.CTkScrollableFrame(
            body, fg_color=BG_DEEP, corner_radius=4,
        )
        list_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 12))

        for p in paths:
            ctk.CTkLabel(
                list_frame,
                text=p.name,
                font=(FONT_FAMILY, CTK_FS10),
                text_color=TEXT_MAIN,
                anchor="w", justify="left",
            ).pack(anchor="w", fill="x", padx=8, pady=1)

        # Button bar
        btn_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        btn_bar.grid(row=2, column=0, sticky="ew")
        btn_bar.grid_propagate(False)
        ctk.CTkFrame(
            btn_bar, fg_color=BORDER, height=1, corner_radius=0,
        ).pack(side="top", fill="x")

        ctk.CTkButton(
            btn_bar, text="Cancel",
            width=100, height=30,
            font=(FONT_FAMILY, CTK_FS11),
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._cancel,
        ).pack(side="right", padx=(4, 12), pady=10)

        ctk.CTkButton(
            btn_bar, text="Remove",
            width=110, height=30,
            font=(FONT_FAMILY, CTK_FS11, "bold"),
            fg_color="#a02d2d", hover_color="#c33a3a", text_color="white",
            command=self._confirm,
        ).pack(side="right", padx=4, pady=10)

        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(100, self._make_modal)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _confirm(self):
        self.confirmed = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _cancel(self):
        self.confirmed = False
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


def _is_archive(name: str) -> bool:
    """Return True if *name* looks like a supported archive file."""
    low = name.lower()
    for ext in _ARCHIVE_EXTS:
        if low.endswith(ext):
            return True
    return False


def _get_downloads_dir() -> Path:
    """Return the user's Downloads directory."""
    xdg = os.environ.get("XDG_DOWNLOAD_DIR")
    if xdg:
        return Path(xdg)
    return Path.home() / "Downloads"


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"




class DownloadsPanel:
    """
    Canvas-based panel that lists archive files found in ~/Downloads.

    Uses pool-based virtual rendering: a fixed number of canvas items are
    pre-allocated and repositioned/reconfigured on scroll rather than
    being destroyed and recreated.

    This is *not* a standalone widget — it builds its widgets inside an
    existing parent frame (the "Downloads" tab of PluginPanel).
    """

    def __init__(
        self,
        parent_tab: tk.Widget,
        log_fn=None,
        install_fn=None,
        on_open_locations: Optional[Callable[[], None]] = None,
        get_installed_filenames: Optional[Callable[[], "InstalledIndex"]] = None,
    ):
        self._parent = parent_tab
        self._log = log_fn or (lambda msg: None)
        self._install_fn = install_fn or (lambda path: None)
        self._on_open_locations = on_open_locations or (lambda: None)
        self._get_installed_filenames = get_installed_filenames or (lambda: InstalledIndex())

        # Data: ordered list of DownloadEntry. Section headers are
        # interleaved between archives from the same source directory.
        self._files: list[DownloadEntry] = []
        self._sel_idx: int = -1
        self._canvas_w: int = 400
        self._resize_after_id: str | None = None
        self._context_menu: CTkPopupMenu | None = None
        # Archive filename (basename) currently highlighted because the
        # corresponding mod is selected in the modlist. None when no
        # single mod is selected, or the selected mod has no archive.
        self._highlighted_archive: str | None = None
        self._installed_filenames: "InstalledIndex" = InstalledIndex()

        # Pool state
        self._pool_bg: list[int] = []
        self._pool_name: list[int] = []
        self._pool_size: list[int] = []
        self._pool_slot: list[int] = []  # data index mapped to each slot, -1 = free
        self._pool_btns: list[tk.Button] = []
        self._pool_btn_ids: list[int] = []  # canvas window item ids
        self._pool_cb_rect: list[int] = []
        self._pool_cb_mark: list[int] = []
        # Per-slot state cache. Each entry is a tuple identifying the
        # current visual state of that slot. _render_slot compares the
        # newly-computed key to the stored one and skips canvas calls
        # when nothing visible has changed.
        self._pool_last_state: list[Optional[tuple]] = [None] * _POOL_SIZE

        # Per-archive checkbox state — keyed by Path so it survives pool reuse.
        self._checked: set[Path] = set()
        self._checked_change_cb: Optional[Callable[[], None]] = None
        # Anchor for shift-click range selection — the archive Path of the
        # last plainly-clicked row. Keyed by Path so it survives rescans /
        # scroll; resolved back to a visible index at shift-click time.
        self._range_anchor: Optional[Path] = None
        # Whether the last plain click selected (True) or deselected (False)
        # the anchor row. A following shift-click applies the same action to
        # the whole range, so shift-click can extend either a selection or a
        # deselection.
        self._range_select: bool = True

        # Filter side panel state
        self._filter_panel_open: bool = False
        self._fsp_vars: dict[str, tk.IntVar] = {}
        self._fsp_ext_vars: dict[str, tk.IntVar] = {}
        # Per-location filter — keyed by the resolved source-dir string so
        # the section header label shown in the panel can change without
        # losing the user's selection.
        self._fsp_loc_vars: dict[str, tk.IntVar] = {}
        # Show-only-installed / Show-only-not-installed toggles — tri-state.
        # Both can be set independently (include and exclude don't conflict).
        self._fsp_only_installed_var: Optional[tk.IntVar] = None
        self._fsp_only_not_installed_var: Optional[tk.IntVar] = None
        self._filter_state: dict[str, int] = {}
        self._filter_extensions: frozenset[str] = frozenset()
        self._filter_extensions_exclude: frozenset[str] = frozenset()
        # Set of resolved-path strings whose section is allowed through.
        # Empty = no location filter (all locations allowed).
        self._filter_locations: frozenset[str] = frozenset()
        self._filter_locations_exclude: frozenset[str] = frozenset()
        self._filter_only_installed: int = 0
        self._filter_only_not_installed: int = 0
        self._filter_side_panel = None
        self._filter_scroll_frame = None
        self._filter_ext_frame = None
        self._filter_loc_frame = None
        self._filter_btn = None
        # Search filter (case-insensitive substring on archive filename).
        self._search_query: str = ""
        # Filtered visible view of self._files (same shape).
        # When no filter is active this is just self._files itself.
        self._visible_files: list[DownloadEntry] = []

        self._build(parent_tab)
        self._create_pool()
        self.refresh()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build(self, tab):
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        toolbar = tk.Frame(tab, bg=BG_HEADER, height=scaled(28))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)
        ctk.CTkButton(
            toolbar, text="\u21ba Refresh", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            font=FONT_HEADER, command=self.refresh,
        ).pack(side="left", padx=8, pady=2)
        ctk.CTkButton(
            toolbar, text="Locations", width=85, height=26,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            font=FONT_HEADER, command=self._on_open_locations,
        ).pack(side="left", padx=(0, 8), pady=2)
        self._filter_btn = ctk.CTkButton(
            toolbar, text="Filters", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            font=FONT_HEADER, command=self._toggle_filter_side_panel,
        )
        self._filter_btn.pack(side="left", padx=(0, 8), pady=2)

        self._dir_label = tk.Label(
            toolbar, text="", anchor="w",
            font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER,
        )
        self._dir_label.pack(side="left", padx=4)

        canvas_frame = tk.Frame(tab, bg=BG_DEEP, bd=0, highlightthickness=0)
        canvas_frame.grid(row=1, column=0, sticky="nsew")
        canvas_frame.grid_rowconfigure(0, weight=1)
        canvas_frame.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            canvas_frame, bg=BG_DEEP, bd=0,
            highlightthickness=0, yscrollincrement=scaled(15), takefocus=0,
        )
        # Custom combined scrollbar + marker strip — same pattern as
        # plugin_panel._pmarker_strip / modlist_panel._marker_strip.
        self._SCROLL_W = scaled(16)
        self._marker_strip = tk.Canvas(
            canvas_frame, bg=BG_DEEP, bd=0, highlightthickness=0,
            width=self._SCROLL_W, takefocus=0,
        )
        self._scroll_first: float = 0.0
        self._scroll_last: float = 1.0
        self._thumb_drag_offset: float | None = None
        self._marker_strip_after_id: str | None = None

        self._canvas.configure(yscrollcommand=self._scroll_set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._marker_strip.grid(row=0, column=1, sticky="ns")

        self._marker_strip.bind("<Configure>",       self._on_marker_strip_resize)
        self._marker_strip.bind("<ButtonPress-1>",   self._on_scrollbar_press)
        self._marker_strip.bind("<B1-Motion>",       self._on_scrollbar_drag)
        self._marker_strip.bind("<ButtonRelease-1>", self._on_scrollbar_release)
        if not LEGACY_WHEEL_REDUNDANT:
            self._marker_strip.bind("<Button-4>",    lambda e: self._scroll(-3))
            self._marker_strip.bind("<Button-5>",    lambda e: self._scroll(3))
        self._marker_strip.bind("<MouseWheel>",      self._on_mousewheel)

        self._canvas.bind("<Configure>",       self._on_resize)
        if not LEGACY_WHEEL_REDUNDANT:
            self._canvas.bind("<Button-4>",        lambda e: self._scroll(-3))
            self._canvas.bind("<Button-5>",        lambda e: self._scroll(3))
        self._canvas.bind("<MouseWheel>",      self._on_mousewheel)
        self._canvas.bind("<Motion>",          self._on_motion)
        self._canvas.bind("<Leave>",           self._on_leave)
        self._canvas.bind("<ButtonPress-1>",   self._on_left_click)
        self._canvas.bind("<ButtonRelease-3>", self._on_right_click)

        # Bottom toolbar — Install Selected + Remove Selected (row 2)
        bottom = tk.Frame(tab, bg=BG_HEADER, height=scaled(32))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.grid_propagate(False)
        self._install_selected_btn = ctk.CTkButton(
            bottom, text="Install Selected (0)", width=160, height=28,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            font=FONT_HEADER, command=self._on_install_selected,
            state="disabled",
        )
        self._install_selected_btn.pack(side="left", padx=8, pady=2)
        self._remove_selected_btn = ctk.CTkButton(
            bottom, text="Remove Selected (0)", width=160, height=28,
            fg_color="#a02d2d", hover_color="#c33a3a", text_color="white",
            font=FONT_HEADER, command=self._on_remove_selected,
            state="disabled",
        )
        self._remove_selected_btn.pack(side="left", padx=(0, 8), pady=2)
        self._checked_change_cb = self._update_selection_buttons

        # Search bar (row 3) — filter the visible list by archive filename
        # substring. Same look as the Ini Files tab search bar.
        search_bar = tk.Frame(tab, bg=BG_HEADER, height=scaled(28))
        search_bar.grid(row=3, column=0, sticky="ew")
        search_bar.grid_propagate(False)
        tk.Label(
            search_bar, text="Search:", bg=BG_HEADER, fg=TEXT_DIM,
            font=(FONT_FAMILY, FS10),
        ).pack(side="left", padx=(8, 4), pady=3)
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", self._on_search_changed)
        self._search_entry = tk.Entry(
            search_bar, textvariable=self._search_var,
            bg=BG_DEEP, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=(FONT_FAMILY, FS10),
            highlightthickness=0, highlightbackground=BG_DEEP,
        )
        self._search_entry.pack(side="left", padx=(0, 8), pady=3,
                                fill="x", expand=True)
        self._search_entry.bind("<Escape>", lambda e: self._search_var.set(""))

        def _select_all(evt):
            evt.widget.select_range(0, tk.END)
            evt.widget.icursor(tk.END)
            return "break"
        self._search_entry.bind("<Control-a>", _select_all)
        self._search_after_id: str | None = None

    # ------------------------------------------------------------------
    # Pool creation
    # ------------------------------------------------------------------

    def _hide_pool_slot(self, s: int) -> None:
        """Hide every canvas item belonging to pool slot *s* and mark it free."""
        c = self._canvas
        c.itemconfigure(self._pool_bg[s], state="hidden")
        c.itemconfigure(self._pool_name[s], state="hidden")
        c.itemconfigure(self._pool_size[s], state="hidden")
        c.itemconfigure(self._pool_btn_ids[s], state="hidden")
        c.itemconfigure(self._pool_cb_rect[s], state="hidden")
        c.itemconfigure(self._pool_cb_mark[s], state="hidden")
        self._pool_slot[s] = -1
        self._pool_last_state[s] = None

    def _create_pool(self):
        """Pre-allocate canvas items for virtual rendering."""
        c = self._canvas
        OFF = -ROW_H * 2  # off-screen parking position

        for _ in range(_POOL_SIZE):
            bg = c.create_rectangle(0, OFF, 0, OFF, fill=BG_DEEP, outline="", state="hidden")

            cb_rect = c.create_rectangle(
                0, OFF, 0, OFF,
                fill=BG_DEEP, outline=BORDER, width=1, state="hidden",
            )
            cb_mark = c.create_text(
                0, OFF, text="✓", anchor="center",
                font=TK_FONT_SMALL, fill=TEXT_MAIN, state="hidden",
            )

            name_id = c.create_text(NAME_PAD_L, OFF, text="", anchor="w",
                                    font=TK_FONT_NORMAL, fill=TEXT_MAIN, state="hidden")
            size_id = c.create_text(0, OFF, text="", anchor="e",
                                    font=TK_FONT_SMALL, fill=TEXT_DIM, state="hidden")

            btn = tk.Button(
                c, text="Install",
                bg="#2d7a2d", fg="#ffffff",
                activebackground="#3a9e3a", activeforeground="#ffffff",
                relief="flat", font=TK_FONT_SMALL, bd=0,
                cursor="hand2", highlightthickness=0,
            )
            if not LEGACY_WHEEL_REDUNDANT:
                btn.bind("<Button-4>",   lambda e: self._scroll(-3))
                btn.bind("<Button-5>",   lambda e: self._scroll(3))
            btn.bind("<MouseWheel>", self._on_mousewheel)
            btn_win = c.create_window(0, OFF, window=btn,
                                      width=BTN_COL_W - 10, height=ROW_H - 10,
                                      state="hidden")

            self._pool_bg.append(bg)
            self._pool_name.append(name_id)
            self._pool_size.append(size_id)
            self._pool_slot.append(-1)
            self._pool_btns.append(btn)
            self._pool_btn_ids.append(btn_win)
            self._pool_cb_rect.append(cb_rect)
            self._pool_cb_mark.append(cb_mark)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _get_scan_dirs(self) -> list[Path]:
        """Return all directories to scan: default Downloads, the active
        game's download_cache, and user-added locations.

        Either default folder is skipped when the user has disabled it via
        the Locations overlay. The cache folder is per-game — only the
        currently selected game's cache is scanned.
        """
        dirs: list[Path] = []
        seen: set[Path] = set()
        if not is_default_downloads_disabled():
            default = _get_downloads_dir()
            try:
                key = default.resolve()
            except OSError:
                key = default
            dirs.append(default)
            seen.add(key)
        # Per-game cache folder for the currently active game.
        if not is_cache_default_disabled():
            cache_dir = self._get_active_game_cache_dir()
            if cache_dir is not None:
                try:
                    key = cache_dir.resolve()
                except OSError:
                    key = cache_dir
                if key not in seen:
                    dirs.append(cache_dir)
                    seen.add(key)
        for p in load_extra_download_locations():
            path = Path(p).expanduser().resolve()
            if path.is_dir() and path not in seen:
                dirs.append(path)
                seen.add(path)
        return dirs

    def _get_active_game_cache_dir(self) -> Optional[Path]:
        """Return the per-game download_cache dir for the currently
        selected game, or None if no game is selected / configured."""
        try:
            from Utils.config_paths import get_download_cache_dir_for_game
            app = self._parent.winfo_toplevel()
            topbar = getattr(app, "_topbar", None)
            if topbar is None:
                return None
            game_name = topbar._game_var.get()
            if not game_name:
                return None
            return get_download_cache_dir_for_game(game_name)
        except Exception:
            return None

    def refresh(self):
        """Scan Downloads + extra locations for archive files and rebuild everything."""
        scan_dirs = self._get_scan_dirs()
        if not scan_dirs:
            self._dir_label.configure(text="(no download locations configured)")
        else:
            primary = scan_dirs[0]
            self._dir_label.configure(
                text=str(primary) + (" +" + str(len(scan_dirs) - 1) + " more" if len(scan_dirs) > 1 else "")
            )

        # Scan per-directory so we can group rows under their source.
        per_dir: list[tuple[Path, list[tuple[Path, float, int]]]] = []
        for dl_dir in scan_dirs:
            if not dl_dir.is_dir():
                continue
            bucket: list[tuple[Path, float, int]] = []
            for entry in dl_dir.iterdir():
                if entry.is_file() and _is_archive(entry.name):
                    try:
                        st = entry.stat()
                        bucket.append((entry, st.st_mtime, st.st_size))
                    except OSError:
                        pass
            # Sort archives within the section by mtime (newest first)
            bucket.sort(key=lambda t: t[1], reverse=True)
            per_dir.append((dl_dir, bucket))

        installed_filenames = self._get_installed_filenames()

        # Resolve the active game's cache dir once so we can label its
        # section explicitly instead of as "Additional location: …".
        cache_dir = self._get_active_game_cache_dir()
        try:
            cache_key = cache_dir.resolve() if cache_dir is not None else None
        except OSError:
            cache_key = cache_dir

        default_dl = _get_downloads_dir()
        try:
            default_key = default_dl.resolve()
        except OSError:
            default_key = default_dl

        # Build the master DownloadEntry list with synthetic section headers.
        new_files: list[DownloadEntry] = []
        for dl_dir, bucket in per_dir:
            try:
                dl_key = dl_dir.resolve()
            except OSError:
                dl_key = dl_dir
            if dl_key == default_key:
                section_name = "Downloads"
            elif cache_key is not None and dl_key == cache_key:
                section_name = "Mod Manager Cache"
            else:
                section_name = f"Additional location: {dl_dir}"
            new_files.append(DownloadEntry(
                is_section_header=True,
                section_name=section_name,
                src_dir=dl_dir,
            ))
            for p, _mt, sz in bucket:
                new_files.append(DownloadEntry(
                    path=p,
                    size_str=_fmt_size(sz),
                    src_dir=dl_dir,
                ))

        self._files = new_files
        self._installed_filenames = installed_filenames
        self._sel_idx = -1
        # Prune checked-set to entries still present after the rescan.
        present = {e.path for e in self._files if e.path is not None}
        if self._checked - present:
            self._checked &= present
            if self._checked_change_cb is not None:
                try:
                    self._checked_change_cb()
                except Exception:
                    pass
        # Drop a stale range anchor that no longer exists on disk.
        if self._range_anchor is not None and self._range_anchor not in present:
            self._range_anchor = None

        # Refresh the dynamic filter lists (counts may have changed) and
        # recompute the visible view.
        self._refresh_filter_extension_list()
        self._refresh_filter_location_list()
        self._apply_filters()

        # Reset all pool slots
        for s in range(_POOL_SIZE):
            if s < len(self._pool_slot):
                self._hide_pool_slot(s)

        total_h = len(self._visible_files) * ROW_H
        self._canvas.configure(scrollregion=(0, 0, self._canvas_w, max(total_h, 1)))
        self._redraw()

        count = sum(1 for e in self._files if not e.is_section_header)
        self._log(f"Downloads: found {count} archive(s) in {len(scan_dirs)} location(s)")

    # ------------------------------------------------------------------
    # Pool-based virtual rendering
    # ------------------------------------------------------------------

    def _redraw(self):
        """Reconfigure pool slots to show only the visible viewport rows."""
        n = len(self._visible_files)
        if not n:
            # Hide every pool slot — there's nothing to show.
            for s in range(_POOL_SIZE):
                if self._pool_slot[s] != -1:
                    self._hide_pool_slot(s)
            total_h = 0
            self._canvas.configure(
                scrollregion=(0, 0, self._canvas_w, max(total_h, 1)),
            )
            self._draw_marker_strip()
            return

        c = self._canvas
        canvas_top = int(c.canvasy(0))
        canvas_h = max(c.winfo_height(), 1)
        first_row = max(0, canvas_top // ROW_H)
        last_row = min(n, (canvas_top + canvas_h) // ROW_H + 2)
        wanted = set(range(first_row, last_row))

        cw = self._canvas_w
        btn_left = cw - BTN_COL_W
        size_right = btn_left - NAME_PAD_R
        name_max_px = max(size_right - SIZE_COL_W - NAME_PAD_L - NAME_PAD_R, 20)
        btn_center_x = cw - BTN_COL_W // 2
        tk_call = c.tk.call

        # Pass 1: identify which slots are still showing wanted rows, free the rest
        showing: dict[int, int] = {}
        free: list[int] = []
        for s in range(_POOL_SIZE):
            di = self._pool_slot[s]
            if di != -1 and di in wanted:
                showing[di] = s
            else:
                if di != -1:
                    self._hide_pool_slot(s)
                free.append(s)

        # Pass 2: reposition already-showing slots and assign free slots to new rows.
        # The render helper handles both archive rows and section headers.
        fi = 0
        for di in range(first_row, last_row):
            if di in showing:
                s = showing[di]
                self._render_slot(c, s, di, cw, size_right, btn_center_x,
                                  name_max_px, tk_call)
                continue
            if fi >= len(free):
                break
            s = free[fi]; fi += 1
            self._pool_slot[s] = di
            self._render_slot(c, s, di, cw, size_right, btn_center_x,
                              name_max_px, tk_call)

        self._draw_marker_strip()

    def _section_archive_paths(self, header_idx: int) -> list[Path]:
        """Return every archive Path belonging to the section header at
        visible index *header_idx* (the contiguous run of non-header rows
        following it). Returns an empty list if *header_idx* is not a
        section header or the section has no archives."""
        n = len(self._visible_files)
        if header_idx < 0 or header_idx >= n:
            return []
        if not self._visible_files[header_idx].is_section_header:
            return []
        paths: list[Path] = []
        j = header_idx + 1
        while j < n and not self._visible_files[j].is_section_header:
            p = self._visible_files[j].path
            if p is not None:
                paths.append(p)
            j += 1
        return paths

    def _render_slot(self, c, s: int, di: int, cw: int, size_right: int,
                     btn_center_x: int, name_max_px: int, tk_call) -> None:
        """Configure pool slot *s* to display visible-row *di*.

        Uses a per-slot state-key cache: if the visual state of the slot
        has not changed since the last render, all canvas calls are
        skipped. This is the same skip-redraw-if-unchanged trick used
        by plugin_panel._predraw.
        """
        entry = self._visible_files[di]
        is_header = entry.is_section_header
        is_highlighted = (
            self._highlighted_archive is not None
            and not is_header
            and entry.path is not None
            and entry.path.name == self._highlighted_archive
        )
        is_checked = (not is_header
                      and entry.path is not None
                      and entry.path in self._checked)
        is_installed = (not is_header
                        and entry.path is not None
                        and self._installed_filenames.is_archive_installed(entry.path.name))

        # For headers, track whether every archive in the section is checked
        # so the "Select all"/"Deselect all" label re-renders when it flips.
        header_all_checked = False
        if is_header:
            section_paths = self._section_archive_paths(di)
            header_all_checked = bool(section_paths) and all(
                p in self._checked for p in section_paths
            )

        # State key — covers every visual aspect that affects the slot.
        key = (
            id(entry),
            is_header,
            entry.section_name if is_header else (entry.path.name if entry.path else ""),
            entry.size_str if not is_header else "",
            is_installed,
            is_highlighted,
            is_checked,
            header_all_checked,
            di == self._sel_idx,
            di,
            cw,
        )
        if self._pool_last_state[s] == key and self._pool_slot[s] == di:
            return
        self._pool_last_state[s] = key

        y0 = di * ROW_H
        y1 = y0 + ROW_H
        yc = y0 + ROW_H // 2

        # Background rectangle (always shown)
        c.coords(self._pool_bg[s], 0, y0, cw, y1)

        if is_header:
            # Section header: full-width header bg, bold left text, no
            # checkbox / install button. The (otherwise unused) size text
            # item is repurposed as a clickable "Select/Deselect all"
            # action on the right edge of the row.
            c.itemconfigure(self._pool_bg[s], fill=BG_HEADER, state="normal")
            c.itemconfigure(self._pool_cb_rect[s], state="hidden")
            c.itemconfigure(self._pool_cb_mark[s], state="hidden")
            c.itemconfigure(self._pool_btn_ids[s], state="hidden")

            section_paths = self._section_archive_paths(di)
            has_archives = bool(section_paths)
            all_checked = has_archives and all(
                p in self._checked for p in section_paths
            )
            action_text = "Deselect all" if all_checked else "Select all"
            if has_archives:
                c.coords(self._pool_size[s], cw - scaled(8), yc)
                c.itemconfigure(self._pool_size[s], text=action_text,
                                font=FONT_SMALL, fill=ACCENT, state="normal")
            else:
                c.itemconfigure(self._pool_size[s], state="hidden")

            name_w = max(cw - scaled(16) - HEADER_ACTION_W, 20) if has_archives \
                else max(cw - scaled(16), 20)
            label = _truncate_text_cached(
                tk_call, entry.section_name, TK_FONT_HEADER, name_w,
            )
            c.coords(self._pool_name[s], scaled(8), yc)
            c.itemconfigure(self._pool_name[s], text=label,
                            font=TK_FONT_HEADER, fill=TEXT_MAIN, state="normal")
            return

        # Archive row
        fpath = entry.path
        size_str = entry.size_str

        if di == self._sel_idx:
            bg = BG_HOVER
        elif is_highlighted:
            bg = _theme.plugin_mod
        elif di % 2 == 0:
            bg = BG_ROW
        else:
            bg = BG_ROW_ALT
        c.itemconfigure(self._pool_bg[s], fill=bg, state="normal")

        # Checkbox
        cb_x0 = (CB_COL_W - CB_SIZE) // 2
        cb_y0 = yc - CB_SIZE // 2
        c.coords(self._pool_cb_rect[s],
                 cb_x0, cb_y0, cb_x0 + CB_SIZE, cb_y0 + CB_SIZE)
        c.itemconfigure(self._pool_cb_rect[s], fill=BG_DEEP, state="normal")
        c.coords(self._pool_cb_mark[s], cb_x0 + CB_SIZE // 2, yc)
        c.itemconfigure(self._pool_cb_mark[s],
                        state="normal" if is_checked else "hidden")

        # File name
        name = _truncate_text_cached(
            tk_call, fpath.name if fpath is not None else "",
            FONT_NORMAL, name_max_px,
        )
        c.coords(self._pool_name[s], NAME_PAD_L, yc)
        c.itemconfigure(self._pool_name[s], text=name,
                        font=FONT_NORMAL, fill=TEXT_MAIN, state="normal")

        # File size — reset font/fill explicitly: this canvas item is
        # reused as the header "Select all" label (FONT_SMALL/ACCENT), so
        # restore the size styling when the slot flips to an archive row.
        c.coords(self._pool_size[s], size_right, yc)
        c.itemconfigure(self._pool_size[s], text=size_str,
                        font=FONT_SMALL, fill=TEXT_DIM, state="normal")

        # Install button
        is_installed = (fpath is not None
                        and self._installed_filenames.is_archive_installed(fpath.name))
        btn = self._pool_btns[s]
        btn.configure(
            text="Reinstall" if is_installed else "Install",
            bg="#c37800" if is_installed else "#2d7a2d",
            activebackground="#e28b00" if is_installed else "#3a9e3a",
            command=(lambda p=fpath: self._on_install(p)) if fpath is not None
                    else (lambda: None),
        )
        c.coords(self._pool_btn_ids[s], btn_center_x, yc)
        c.itemconfigure(self._pool_btn_ids[s], state="normal")

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def _scroll(self, units: int):
        # Tk Canvas.yview_scroll does not clamp when the scrollregion is
        # smaller than the viewport — it happily moves contents off-screen
        # in either direction. Guard manually: if the content fits in the
        # viewport, treat as un-scrollable.
        total_h = len(self._visible_files) * ROW_H
        if total_h <= self._canvas.winfo_height():
            if self._scroll_first != 0.0 or self._scroll_last != 1.0:
                self._canvas.yview_moveto(0)
            return
        # Also block scrolling past either edge.
        if units < 0 and self._scroll_first <= 0.0:
            return
        if units > 0 and self._scroll_last >= 1.0:
            return
        self._canvas.yview_scroll(units, "units")
        self._redraw()

    def _on_mousewheel(self, event):
        direction = -1 if event.delta > 0 else 1
        self._scroll(direction * 3)

    def _on_resize(self, event):
        new_w = event.width
        if new_w == self._canvas_w:
            return
        self._canvas_w = new_w
        if self._resize_after_id:
            self._canvas.after_cancel(self._resize_after_id)
        self._resize_after_id = self._canvas.after(150, self._apply_resize)

    def _apply_resize(self):
        self._resize_after_id = None
        # Invalidate all pool slots so _redraw reconfigures positions
        for s in range(_POOL_SIZE):
            self._hide_pool_slot(s)
        total_h = len(self._visible_files) * ROW_H
        self._canvas.configure(scrollregion=(0, 0, self._canvas_w, max(total_h, 1)))
        self._redraw()

    # ------------------------------------------------------------------
    # Marker-strip scrollbar
    # ------------------------------------------------------------------

    def _scroll_set(self, first: str, last: str) -> None:
        try:
            f = float(first); l = float(last)
        except (TypeError, ValueError):
            return
        if f == self._scroll_first and l == self._scroll_last:
            return
        self._scroll_first = f
        self._scroll_last = l
        self._redraw_thumb()

    def _on_marker_strip_resize(self, _event):
        if self._marker_strip_after_id is not None:
            try:
                self._marker_strip.after_cancel(self._marker_strip_after_id)
            except Exception:
                pass
        self._marker_strip_after_id = self._marker_strip.after(
            250, self._draw_marker_strip
        )

    def _draw_marker_strip(self) -> None:
        """Paint the trough + ticks (highlighted archive) + thumb."""
        self._marker_strip_after_id = None
        c = self._marker_strip
        strip_h = c.winfo_height()
        strip_w = c.winfo_width()
        c.delete("all")
        if strip_h <= 1 or strip_w <= 1:
            return
        c.create_rectangle(0, 0, strip_w, strip_h, fill=BG_DEEP, outline="", tags="trough")

        # Orange tick(s) for the highlighted archive
        if self._highlighted_archive and self._visible_files:
            n = len(self._visible_files)
            target = self._highlighted_archive
            inv_n = 1.0 / n
            strip_max = strip_h - 4
            for i, entry in enumerate(self._visible_files):
                if entry.is_section_header or entry.path is None:
                    continue
                if entry.path.name != target:
                    continue
                y = int(i * inv_n * strip_h)
                if y < 2:
                    y = 2
                elif y > strip_max:
                    y = strip_max
                c.create_rectangle(
                    0, y, strip_w, y + 3,
                    fill=_theme.plugin_mod, outline="", tags="marker",
                )

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
            return  # content fits — no thumb
        y1 = int(first * strip_h)
        y2 = max(y1 + 8, int(last * strip_h))
        if y2 > strip_h:
            y2 = strip_h
            y1 = max(0, y2 - 8)
        c.create_rectangle(0, y1, strip_w, y2, fill=BG_SEP, outline="", tags="thumb")

    def _on_scrollbar_press(self, event):
        strip_h = self._marker_strip.winfo_height()
        if strip_h <= 1:
            return
        first = self._scroll_first
        last = self._scroll_last
        thumb_top = first * strip_h
        thumb_bot = last * strip_h
        if thumb_top <= event.y <= thumb_bot:
            self._thumb_drag_offset = (event.y - thumb_top) / strip_h
        else:
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
        self._redraw()

    # ------------------------------------------------------------------
    # Hover highlight
    # ------------------------------------------------------------------

    def _on_motion(self, event):
        y = int(self._canvas.canvasy(event.y))
        idx = y // ROW_H
        if 0 <= idx < len(self._visible_files):
            entry = self._visible_files[idx]
            if entry.is_section_header:
                idx = -1
        else:
            idx = -1
        new_idx = idx
        if new_idx != self._sel_idx:
            self._sel_idx = new_idx
            self._redraw()

    def _on_leave(self, _event):
        if self._sel_idx != -1:
            self._sel_idx = -1
            self._redraw()

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    def _on_install(self, fpath: Path):
        self._log(f"Installing {fpath.name} \u2026")
        self._install_fn(str(fpath))

    # ------------------------------------------------------------------
    # Install Selected \u2014 multi-install with FOMOD deferral
    # ------------------------------------------------------------------

    def _update_selection_buttons(self) -> None:
        """Refresh the bottom-toolbar action buttons to reflect _checked size."""
        n = len(self._checked)
        state = "normal" if n > 0 else "disabled"
        install_btn = getattr(self, "_install_selected_btn", None)
        if install_btn is not None:
            install_btn.configure(text=f"Install Selected ({n})", state=state)
        remove_btn = getattr(self, "_remove_selected_btn", None)
        if remove_btn is not None:
            remove_btn.configure(text=f"Remove Selected ({n})", state=state)

    def _on_install_selected(self) -> None:
        """Install all checked archives sequentially. FOMODs are deferred to
        the end so they don't gate non-interactive installs (mirrors the
        collections install flow). One bad archive does not abort the batch.
        """
        paths = list(self._checked)
        if not paths:
            return
        # Resolve app/game/mod_panel up-front on the main thread.
        from gui.game_helpers import _GAMES
        from gui.install_mod import install_mod_from_archive, FOMOD_DEFERRED

        app = self._parent.winfo_toplevel()
        topbar = getattr(app, "_topbar", None)
        if topbar is None:
            self._log("Cannot install \u2014 top bar not ready.")
            return
        game = _GAMES.get(topbar._game_var.get())
        if game is None or not game.is_configured():
            self._log("No configured game selected \u2014 use + to set the game path first.")
            return
        mod_panel = getattr(app, "_mod_panel", None)
        status_bar = getattr(app, "_status", None)
        disable_extract = getattr(topbar, "_disable_extract", False)

        # Disable button immediately so the user can't double-fire.
        try:
            self._install_selected_btn.configure(state="disabled")
        except Exception:
            pass

        total = len(paths)

        # Pooled install notification — one toast with a progress bar for the
        # whole batch instead of one "Installed: X" toast per mod. Created on
        # the main thread; the worker drives it via app.after.
        from gui.ctk_components import CTkNotification
        pooled_notif: list = [None]

        def _make_pooled_notif():
            try:
                pooled_notif[0] = CTkNotification(
                    app.winfo_toplevel(), state="info",
                    message=f"Installing 0/{total} mods…",
                    show_progress=True,
                )
            except Exception:
                pooled_notif[0] = None
        app.after(0, _make_pooled_notif)

        def _update_pooled(done: int, label: str) -> None:
            def _do():
                n = pooled_notif[0]
                if n is not None and n.winfo_exists():
                    n.update_message(f"{label} {done}/{total} mods…")
                    n.set_progress(done / total if total else 1.0)
            app.after(0, _do)

        def _finish_pooled(installed: int, failed: int) -> None:
            def _do():
                n = pooled_notif[0]
                if n is None or not n.winfo_exists():
                    return
                if failed:
                    n.update_message(
                        f"Installed {installed}/{total} mods ({failed} failed)",
                        state="warning")
                else:
                    n.update_message(f"Installed {installed} mod(s)", state="success")
                n.set_progress(1.0)
                n.after(4000, n.destroy)
            app.after(0, _do)

        # NB: this batch flow no longer drives the StatusBar progress popup —
        # the pooled CTkNotification above is the single source of truth, so a
        # second bottom-right overlay doesn't appear. We still pass a
        # clear-progress callback to install_mod_from_archive so it can tidy any
        # popup it might create internally.
        def _clear_progress() -> None:
            if status_bar is not None:
                app.after(0, status_bar.clear_progress)

        def _worker():
            deferred: list[Path] = []
            done = 0
            installed = 0
            failed = 0
            try:
                # Phase A \u2014 non-FOMODs first.
                for path in paths:
                    _update_pooled(done, "Installing")
                    self._log(f"Installing {path.name} \u2026")
                    ok = False
                    try:
                        result = install_mod_from_archive(
                            str(path), app, self._log, game, mod_panel,
                            disable_extract=disable_extract,
                            clear_progress_fn=_clear_progress,
                            defer_interactive_fomod=True,
                            suppress_notification=True,
                        )
                        ok = True
                    except Exception as exc:
                        self._log(f"Install failed: {path.name}: {exc}")
                        result = None
                        failed += 1
                    if result == FOMOD_DEFERRED:
                        deferred.append(path)  # counted in Phase B
                    elif ok:
                        installed += 1
                    done += 1
                # Phase B \u2014 deferred FOMODs (interactive). These show their
                # own wizard UI, so the pooled toast just tracks the count.
                for path in deferred:
                    _update_pooled(done, "Installing FOMOD")
                    self._log(f"Installing FOMOD {path.name} \u2026")
                    try:
                        install_mod_from_archive(
                            str(path), app, self._log, game, mod_panel,
                            disable_extract=disable_extract,
                            clear_progress_fn=_clear_progress,
                            suppress_notification=True,
                        )
                        installed += 1
                    except Exception as exc:
                        self._log(f"FOMOD install failed: {path.name}: {exc}")
                        failed += 1
                    done += 1
            finally:
                _clear_progress()
                _finish_pooled(installed, failed)

                def _finish():
                    self._checked.clear()
                    self.refresh()
                    self._update_selection_buttons()

                app.after(0, _finish)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Remove Selected — delete checked archives from disk
    # ------------------------------------------------------------------

    def _on_remove_selected(self) -> None:
        """Permanently delete every checked archive from its source location.

        Prompts for confirmation (this can't be undone) and runs the
        delete loop on a background thread so a slow filesystem doesn't
        freeze the UI. Failures are logged and skipped.
        """
        paths = list(self._checked)
        if not paths:
            return

        app = self._parent.winfo_toplevel()

        # Confirm — this is destructive. Use a CTk dialog with a scrollable
        # list of every archive (no truncation) so the user can audit the
        # exact set being deleted.
        dlg = _RemoveArchivesConfirmDialog(app, paths)
        app.wait_window(dlg)
        if not dlg.confirmed:
            return

        # Snapshot + disable buttons immediately.
        try:
            self._remove_selected_btn.configure(state="disabled")
            self._install_selected_btn.configure(state="disabled")
        except Exception:
            pass

        status_bar = getattr(app, "_status", None)
        total = len(paths)

        def _set_progress(done: int) -> None:
            if status_bar is None:
                return
            app.after(0, lambda d=done:
                      status_bar.set_progress(d, total, title=f"Removing {d}/{total}"))

        def _clear_progress() -> None:
            if status_bar is not None:
                app.after(0, status_bar.clear_progress)

        def _worker():
            done = 0
            removed = 0
            try:
                for path in paths:
                    _set_progress(done)
                    try:
                        if path.is_file():
                            path.unlink()
                            removed += 1
                            self._log(f"Removed {path.name}")
                        else:
                            self._log(f"Skipped (not a file): {path.name}")
                    except Exception as exc:
                        self._log(f"Remove failed: {path.name}: {exc}")
                    done += 1
            finally:
                _clear_progress()
                self._log(f"Removed {removed}/{total} archive(s).")

                def _finish():
                    self._checked.clear()
                    self.refresh()
                    self._update_selection_buttons()

                app.after(0, _finish)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Filter side panel
    # ------------------------------------------------------------------

    def _build_filter_side_panel(self) -> None:
        """Build the inline filter side panel as a child of ModListPanel
        at column 0 (same slot used by modlist + plugin filter panels).
        """
        if self._filter_side_panel is not None:
            return
        mod_panel = getattr(self._parent.winfo_toplevel(), "_mod_panel", None)
        parent = mod_panel if mod_panel is not None else self._parent
        panel = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=0, width=380)
        panel.grid(row=0, column=0, rowspan=5, sticky="nsew")
        panel.grid_propagate(False)
        panel.grid_remove()
        self._filter_side_panel = panel

        # Header
        header = tk.Frame(panel, bg=BG_HEADER, height=scaled(36))
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(
            header, text="Download Filters", bg=BG_HEADER, fg=TEXT_MAIN,
            font=_theme.TK_FONT_BOLD, anchor="w",
        ).pack(side="left", padx=10, pady=6)

        close_btn = tk.Label(
            header, text="×", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, 16, "bold"), cursor="hand2",
        )
        close_btn.pack(side="right", padx=8)
        close_btn.bind("<Button-1>", lambda _e: self._close_filter_side_panel())
        close_btn.bind("<Enter>",    lambda _e: close_btn.configure(fg=TEXT_MAIN))
        close_btn.bind("<Leave>",    lambda _e: close_btn.configure(fg=TEXT_DIM))

        clear_btn = tk.Label(
            header, text="Clear all", bg=BG_HEADER, fg=TEXT_DIM,
            font=_theme.TK_FONT_SMALL, cursor="hand2",
        )
        clear_btn.pack(side="right", padx=(0, 4))
        clear_btn.bind("<Button-1>", lambda _e: self._clear_all_filters())
        clear_btn.bind("<Enter>",    lambda _e: clear_btn.configure(fg=TEXT_MAIN))
        clear_btn.bind("<Leave>",    lambda _e: clear_btn.configure(fg=TEXT_DIM))

        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x")

        scroll_frame = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0,
        )
        scroll_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self._filter_scroll_frame = scroll_frame

        # --- Status section (installed / not installed) ---
        tk.Label(
            scroll_frame, text="By status",
            font=_theme.TK_FONT_BOLD, fg=TEXT_MAIN,
            bg=BG_PANEL, anchor="w",
        ).pack(anchor="w", pady=(2, 4))

        self._fsp_only_installed_var = tk.IntVar(value=int(self._filter_only_installed or 0))
        TriStateCheckBox(
            scroll_frame,
            text="Show only installed",
            variable=self._fsp_only_installed_var,
            font=_theme.FONT_SMALL,
            text_color=TEXT_MAIN,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            border_color=BORDER,
            checkmark_color="white",
            command=self._on_filter_panel_change,
        ).pack(anchor="w", fill="x", pady=2)

        self._fsp_only_not_installed_var = tk.IntVar(value=int(self._filter_only_not_installed or 0))
        TriStateCheckBox(
            scroll_frame,
            text="Show only not installed",
            variable=self._fsp_only_not_installed_var,
            font=_theme.FONT_SMALL,
            text_color=TEXT_MAIN,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            border_color=BORDER,
            checkmark_color="white",
            command=self._on_filter_panel_change,
        ).pack(anchor="w", fill="x", pady=2)

        # --- Location section ---
        tk.Label(
            scroll_frame, text="By location",
            font=_theme.TK_FONT_BOLD, fg=TEXT_MAIN,
            bg=BG_PANEL, anchor="w",
        ).pack(anchor="w", pady=(10, 4))

        self._filter_loc_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        self._filter_loc_frame.pack(fill="x")

        # --- File type section ---
        tk.Label(
            scroll_frame, text="By file type",
            font=_theme.TK_FONT_BOLD, fg=TEXT_MAIN,
            bg=BG_PANEL, anchor="w",
        ).pack(anchor="w", pady=(10, 4))

        self._filter_ext_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        self._filter_ext_frame.pack(fill="x")

        self._refresh_filter_location_list()
        self._refresh_filter_extension_list()
        self._bind_filter_panel_scroll()

    def _bind_filter_panel_scroll(self) -> None:
        scroll_frame = self._filter_scroll_frame
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
            if not LEGACY_WHEEL_REDUNDANT:
                w.bind("<Button-4>", _on_wheel)
                w.bind("<Button-5>", _on_wheel)
            for child in w.winfo_children():
                _bind_recursive(child)

        _bind_recursive(scroll_frame)

    def _file_extension(self, name: str) -> str:
        """Return the lowercase extension, treating compound .tar.* together."""
        low = name.lower()
        for ext in (".tar.gz", ".tar.bz2", ".tar.xz"):
            if low.endswith(ext):
                return ext
        i = low.rfind(".")
        return low[i:] if i >= 0 else ""

    def _section_label_for_dir(self, dl_dir: Path) -> str:
        """Return the user-facing label used for a section header / filter
        row, matching the logic in refresh()."""
        try:
            dl_key = dl_dir.resolve()
        except OSError:
            dl_key = dl_dir
        try:
            default_key = _get_downloads_dir().resolve()
        except OSError:
            default_key = _get_downloads_dir()
        cache_dir = self._get_active_game_cache_dir()
        try:
            cache_key = cache_dir.resolve() if cache_dir is not None else None
        except OSError:
            cache_key = cache_dir
        if dl_key == default_key:
            return "Downloads"
        if cache_key is not None and dl_key == cache_key:
            return "Mod Manager Cache"
        return f"Additional location: {dl_dir}"

    def _refresh_filter_location_list(self) -> None:
        """Rebuild the per-location checkboxes from the current scan dirs."""
        frame = self._filter_loc_frame
        if frame is None:
            return
        for w in frame.winfo_children():
            w.destroy()

        # Count archives per source directory (skip headers).
        counts: dict[str, int] = {}
        labels: dict[str, str] = {}
        for entry in self._files:
            if entry.is_section_header or entry.path is None or entry.src_dir is None:
                continue
            try:
                key = str(entry.src_dir.resolve())
            except OSError:
                key = str(entry.src_dir)
            counts[key] = counts.get(key, 0) + 1
            if key not in labels:
                labels[key] = self._section_label_for_dir(entry.src_dir)

        # Preserve checked state across rebuilds.
        prev_state = {k: var.get() for k, var in self._fsp_loc_vars.items()}
        self._fsp_loc_vars = {}

        # Sort by label for stable display.
        for key in sorted(counts.keys(), key=lambda k: labels.get(k, k).lower()):
            label = labels.get(key, key)
            var = tk.IntVar(value=int(prev_state.get(key, 0)))
            self._fsp_loc_vars[key] = var
            TriStateCheckBox(
                frame,
                text=f"{label}  ({counts[key]})",
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_filter_panel_change,
            ).pack(anchor="w", fill="x", pady=2)

        self._bind_filter_panel_scroll()

    def _refresh_filter_extension_list(self) -> None:
        """Rebuild the per-extension checkboxes from the current master file list."""
        frame = self._filter_ext_frame
        if frame is None:
            return  # filter panel never built yet — nothing to refresh
        # Tear down existing children
        for w in frame.winfo_children():
            w.destroy()

        # Count by extension
        counts: dict[str, int] = {}
        for entry in self._files:
            if entry.is_section_header or entry.path is None:
                continue
            ext = self._file_extension(entry.path.name)
            counts[ext] = counts.get(ext, 0) + 1

        # Preserve checked state across rebuilds
        prev_state = {ext: var.get() for ext, var in self._fsp_ext_vars.items()}
        self._fsp_ext_vars = {}

        for ext in sorted(counts.keys()):
            var = tk.IntVar(value=int(prev_state.get(ext, 0)))
            self._fsp_ext_vars[ext] = var
            TriStateCheckBox(
                frame,
                text=f"{ext}  ({counts[ext]})",
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_filter_panel_change,
            ).pack(anchor="w", fill="x", pady=2)

        self._bind_filter_panel_scroll()

    def _on_filter_panel_change(self) -> None:
        self._filter_extensions = frozenset(
            ext for ext, var in self._fsp_ext_vars.items() if var.get() == 1
        )
        self._filter_extensions_exclude = frozenset(
            ext for ext, var in self._fsp_ext_vars.items() if var.get() == 2
        )
        self._filter_locations = frozenset(
            k for k, var in self._fsp_loc_vars.items() if var.get() == 1
        )
        self._filter_locations_exclude = frozenset(
            k for k, var in self._fsp_loc_vars.items() if var.get() == 2
        )
        self._filter_only_installed = (
            self._fsp_only_installed_var.get()
            if self._fsp_only_installed_var is not None else 0
        )
        self._filter_only_not_installed = (
            self._fsp_only_not_installed_var.get()
            if self._fsp_only_not_installed_var is not None else 0
        )

        self._update_filter_btn_color()
        self._apply_filters()
        # Reset scroll + invalidate pool so highlights/positions are recomputed.
        self._canvas.yview_moveto(0)
        for s in range(_POOL_SIZE):
            self._hide_pool_slot(s)
        total_h = len(self._visible_files) * ROW_H
        self._canvas.configure(scrollregion=(0, 0, self._canvas_w, max(total_h, 1)))
        self._redraw()

    def _apply_filters(self) -> None:
        """Recompute self._visible_files from self._files + active filters.

        Applies (combined with AND): file-extension filter, location
        filter, status filter (only-installed / only-not-installed), and
        the search-query filter. Section headers whose section ends up
        with zero archives are dropped so the view stays tidy.
        """
        wanted_exts = self._filter_extensions
        excluded_exts = self._filter_extensions_exclude
        wanted_locs = self._filter_locations
        excluded_locs = self._filter_locations_exclude
        only_installed = self._filter_only_installed
        only_not_installed = self._filter_only_not_installed
        installed = self._installed_filenames
        query = self._search_query

        any_active = bool(
            wanted_exts or excluded_exts or wanted_locs or excluded_locs
            or only_installed or only_not_installed or query
        )
        if not any_active:
            self._visible_files = list(self._files)
            return

        def _resolved_dir(p: Optional[Path]) -> str:
            if p is None:
                return ""
            try:
                return str(p.resolve())
            except OSError:
                return str(p)

        def _archive_matches(arc: DownloadEntry) -> bool:
            if arc.path is None:
                return False
            ext = self._file_extension(arc.path.name)
            if wanted_exts and ext not in wanted_exts:
                return False
            if excluded_exts and ext in excluded_exts:
                return False
            loc = _resolved_dir(arc.src_dir)
            if wanted_locs and loc not in wanted_locs:
                return False
            if excluded_locs and loc in excluded_locs:
                return False
            is_installed = installed.is_archive_installed(arc.path.name)
            if only_installed == 1 and not is_installed:
                return False
            if only_installed == 2 and is_installed:
                return False
            if only_not_installed == 1 and is_installed:
                return False
            if only_not_installed == 2 and not is_installed:
                return False
            if query and query not in arc.path.name.casefold():
                return False
            return True

        result: list[DownloadEntry] = []
        i = 0
        n = len(self._files)
        while i < n:
            entry = self._files[i]
            if entry.is_section_header:
                j = i + 1
                section_matches: list[DownloadEntry] = []
                while j < n and not self._files[j].is_section_header:
                    arc = self._files[j]
                    if _archive_matches(arc):
                        section_matches.append(arc)
                    j += 1
                if section_matches:
                    result.append(entry)
                    result.extend(section_matches)
                i = j
            else:
                if _archive_matches(entry):
                    result.append(entry)
                i += 1
        self._visible_files = result

    def _on_search_changed(self, *_args) -> None:
        """Debounced re-filter when the search field changes."""
        if self._search_after_id is not None:
            try:
                self._search_entry.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._search_after_id = self._search_entry.after(
            150, self._apply_search_now,
        )

    def _apply_search_now(self) -> None:
        self._search_after_id = None
        new_query = self._search_var.get().casefold().strip()
        if new_query == self._search_query:
            return
        self._search_query = new_query
        self._apply_filters()
        # Reset scroll + invalidate pool so positions recompute.
        self._canvas.yview_moveto(0)
        for s in range(_POOL_SIZE):
            self._hide_pool_slot(s)
        total_h = len(self._visible_files) * ROW_H
        self._canvas.configure(scrollregion=(0, 0, self._canvas_w, max(total_h, 1)))
        self._redraw()

    def _clear_all_filters(self) -> None:
        for v in self._fsp_ext_vars.values():
            v.set(0)
        for v in self._fsp_loc_vars.values():
            v.set(0)
        if self._fsp_only_installed_var is not None:
            self._fsp_only_installed_var.set(0)
        if self._fsp_only_not_installed_var is not None:
            self._fsp_only_not_installed_var.set(0)
        # Rebuild the lists so the TriStateCheckBoxes redraw their visuals.
        self._refresh_filter_extension_list()
        self._refresh_filter_location_list()
        self._on_filter_panel_change()

    def _update_filter_btn_color(self) -> None:
        btn = self._filter_btn
        if btn is None:
            return
        any_active = bool(
            self._filter_extensions or self._filter_extensions_exclude
            or self._filter_locations or self._filter_locations_exclude
            or self._filter_only_installed or self._filter_only_not_installed
        )
        btn.configure(fg_color=ACCENT_HOV if any_active else ACCENT)

    def _toggle_filter_side_panel(self) -> None:
        if self._filter_side_panel is None:
            self._build_filter_side_panel()
        if self._filter_panel_open:
            self._close_filter_side_panel()
        else:
            self._open_filter_side_panel()

    def _open_filter_side_panel(self) -> None:
        if self._filter_side_panel is None:
            self._build_filter_side_panel()
        if self._filter_side_panel is None:
            return
        # Close the other two filter panels if they're sharing column 0.
        app = self._parent.winfo_toplevel()
        mod_panel = getattr(app, "_mod_panel", None)
        plugin_panel = getattr(app, "_plugin_panel", None)
        if mod_panel is not None and getattr(mod_panel, "_filter_panel_open", False):
            try:
                mod_panel._close_filter_side_panel()
            except Exception:
                pass
        if plugin_panel is not None and getattr(plugin_panel, "_plugin_filter_panel_open", False):
            try:
                plugin_panel._close_plugin_filter_panel()
            except Exception:
                pass
        if plugin_panel is not None and getattr(plugin_panel, "_data_filter_panel_open", False):
            try:
                plugin_panel._close_data_filter_panel()
            except Exception:
                pass
        if plugin_panel is not None and getattr(plugin_panel, "_ini_filter_panel_open", False):
            try:
                plugin_panel._close_ini_filter_panel()
            except Exception:
                pass
        if plugin_panel is not None and getattr(plugin_panel, "_mf_filter_panel_open", False):
            try:
                plugin_panel._close_mf_filter_panel()
            except Exception:
                pass
        self._filter_panel_open = True
        if mod_panel is not None:
            mod_panel.grid_columnconfigure(0, minsize=scaled(380))
        self._filter_side_panel.grid()
        self._update_filter_btn_color()

    def _close_filter_side_panel(self) -> None:
        self._filter_panel_open = False
        app = self._parent.winfo_toplevel()
        mod_panel = getattr(app, "_mod_panel", None)
        if mod_panel is not None:
            mod_panel.grid_columnconfigure(0, minsize=0)
        if self._filter_side_panel is not None:
            self._filter_side_panel.grid_remove()
        self._update_filter_btn_color()

    # ------------------------------------------------------------------
    # Checkbox click handling
    # ------------------------------------------------------------------

    def _on_left_click(self, event):
        """Toggle the row's checkbox when the user clicks anywhere on the row.

        Clicks on the embedded Install button are intercepted by the
        button widget itself and never reach this handler. Section
        headers and clicks past the visible row range are ignored.
        Right-click goes through _on_right_click.
        """
        y = int(self._canvas.canvasy(event.y))
        idx = y // ROW_H
        if idx < 0 or idx >= len(self._visible_files):
            return
        entry = self._visible_files[idx]
        if entry.is_section_header:
            # Clicking the "Select all" / "Deselect all" action on the
            # right edge toggles every archive in that section.
            if int(self._canvas.canvasx(event.x)) >= self._canvas_w - HEADER_ACTION_W:
                self._toggle_section(idx)
            return
        if entry.path is None:
            return
        fpath = entry.path

        # Shift-click — apply the anchor's last action (select or deselect)
        # to every archive between the anchor row and this row inclusive.
        # Section headers inside the range are skipped. Falls back to a
        # plain toggle when there is no resolvable anchor.
        if (event.state & 0x0001) and self._range_anchor is not None:
            anchor_idx = self._visible_index_of(self._range_anchor)
            if anchor_idx is not None:
                lo, hi = sorted((anchor_idx, idx))
                for e in self._visible_files[lo:hi + 1]:
                    if e.is_section_header or e.path is None:
                        continue
                    if self._range_select:
                        self._checked.add(e.path)
                    else:
                        self._checked.discard(e.path)
                # Leave the anchor unchanged so the user can extend the
                # range from the same origin with another shift-click.
                self._redraw()
                if self._checked_change_cb is not None:
                    try:
                        self._checked_change_cb()
                    except Exception:
                        pass
                return

        if fpath in self._checked:
            self._checked.discard(fpath)
            self._range_select = False
        else:
            self._checked.add(fpath)
            self._range_select = True
        self._range_anchor = fpath
        self._redraw()
        if self._checked_change_cb is not None:
            try:
                self._checked_change_cb()
            except Exception:
                pass

    def _visible_index_of(self, path: Path) -> Optional[int]:
        """Return the index of the archive *path* in the current visible
        list, or None if it is not present (filtered out / removed)."""
        for i, e in enumerate(self._visible_files):
            if not e.is_section_header and e.path == path:
                return i
        return None

    def _toggle_section(self, header_idx: int) -> None:
        """Select all archives in the section at *header_idx*, or deselect
        them all if they are already fully selected."""
        section_paths = self._section_archive_paths(header_idx)
        if not section_paths:
            return
        if all(p in self._checked for p in section_paths):
            self._checked.difference_update(section_paths)
        else:
            self._checked.update(section_paths)
        self._redraw()
        if self._checked_change_cb is not None:
            try:
                self._checked_change_cb()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Modlist selection cross-link
    # ------------------------------------------------------------------

    def set_highlighted_archive(self, filename: str | None) -> None:
        """Highlight the row whose archive matches *filename* in orange,
        and paint an orange tick on the marker strip at its position.

        Pass ``None`` to clear the highlight. Called by gui.py when the
        modlist's anchor selection changes (single-selection only).
        """
        if filename == self._highlighted_archive:
            return
        self._highlighted_archive = filename or None
        # Force a re-render of every visible slot so the row colour updates
        # \u2014 _redraw() doesn't know what changed about each slot, so we
        # invalidate the slot map. Cheap because pool size is small.
        for s in range(_POOL_SIZE):
            self._hide_pool_slot(s)
        self._redraw()

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _on_right_click(self, event):
        y = int(self._canvas.canvasy(event.y))
        idx = y // ROW_H
        if idx < 0 or idx >= len(self._visible_files):
            return
        entry = self._visible_files[idx]
        if entry.is_section_header or entry.path is None:
            return
        fpath = entry.path
        if self._context_menu is None:
            self._context_menu = CTkPopupMenu(
                self._parent.winfo_toplevel(), width=200, title=""
            )
        menu = self._context_menu
        menu.clear()
        menu.add_command("Install Mod", lambda: self._on_install(fpath))
        menu.popup(event.x_root, event.y_root)


# ----------------------------------------------------------------------
# Tab-level glue
#
# These module-level helpers used to live as methods on PluginPanel.
# Keeping them in this module means each tab module owns its own
# build/install/locations wiring, and PluginPanel just composes.
# ----------------------------------------------------------------------


def _get_installed_filenames(plugin_panel) -> "InstalledIndex":
    """Return an :class:`InstalledIndex` describing every installed mod.

    Includes both the literal archive filename (as recorded in
    ``meta.ini``'s ``installationFile``) and the Nexus ``(mod_id,
    file_id)`` pair, so the Downloads tab can recognise an archive as
    installed even when the on-disk filename differs from what the
    collection / installer recorded (collections store the canonical
    Nexus file_name which sometimes uses different spacing/encoding
    from the actual download).
    """
    idx = InstalledIndex()
    try:
        from gui.game_helpers import _GAMES
        from Nexus.nexus_meta import read_meta

        app = plugin_panel.winfo_toplevel()
        topbar = app._topbar
        game = _GAMES.get(topbar._game_var.get())
        if game is None or not game.is_configured():
            return idx
        staging = game.get_effective_mod_staging_path()
        if not staging or not Path(staging).is_dir():
            return idx
        for folder in Path(staging).iterdir():
            meta_path = folder / "meta.ini"
            if not meta_path.is_file():
                continue
            try:
                m = read_meta(meta_path)
            except Exception:
                continue
            if m.installation_file:
                idx.names.add(m.installation_file)
            if m.mod_id and m.file_id:
                idx.mod_file_ids.add((int(m.mod_id), int(m.file_id)))
        return idx
    except Exception:
        return idx


def _install_from_downloads(plugin_panel, archive_path: str) -> None:
    """Trigger the standard install-mod flow for an archive picked from the Downloads tab."""
    from gui.game_helpers import _GAMES
    from gui.install_mod import install_mod_from_archive

    app = plugin_panel.winfo_toplevel()
    topbar = app._topbar
    game = _GAMES.get(topbar._game_var.get())
    if game is None or not game.is_configured():
        plugin_panel._log("No configured game selected — use + to set the game path first.")
        return
    plugin_panel._log(f"Installing: {os.path.basename(archive_path)}")
    mod_panel = getattr(app, "_mod_panel", None)
    status_bar = getattr(app, "_status", None)

    def _extract_progress(done: int, total: int, phase: str | None = None):
        if status_bar is not None:
            app.after(0, lambda d=done, t=total, p=phase:
                      status_bar.set_progress(d, t, p, title="Extracting"))

    def _cleanup():
        plugin_panel._downloads_panel.refresh()

    disable_extract = getattr(topbar, "_disable_extract", False)

    def _worker():
        try:
            install_mod_from_archive(
                archive_path, app, plugin_panel._log, game, mod_panel,
                on_installed=_cleanup,
                disable_extract=disable_extract,
                progress_fn=_extract_progress,
                clear_progress_fn=lambda:
                    app.after(0, status_bar.clear_progress)
                    if status_bar is not None else None,
            )
        finally:
            if status_bar is not None:
                app.after(0, status_bar.clear_progress)

    threading.Thread(target=_worker, daemon=True).start()


def _open_download_locations_overlay(plugin_panel) -> None:
    """Show the download locations overlay over the plugin panel."""
    _close_download_locations_overlay(plugin_panel)
    # Resolve the active game name so the overlay can list the per-game
    # cache folder (and let the user enable/disable scanning it).
    active_game = ""
    try:
        topbar = plugin_panel.winfo_toplevel()._topbar
        active_game = topbar._game_var.get() or ""
    except Exception:
        active_game = ""
    panel = DownloadLocationsOverlay(
        plugin_panel,
        on_close=lambda: _close_download_locations_overlay(plugin_panel),
        on_saved=lambda: plugin_panel._downloads_panel.refresh(),
        active_game_name=active_game,
    )
    panel.place(relx=0, rely=0, relwidth=1, relheight=1)
    plugin_panel._download_locations_overlay = panel


def _close_download_locations_overlay(plugin_panel) -> None:
    """Destroy the download locations overlay if present."""
    panel = getattr(plugin_panel, "_download_locations_overlay", None)
    if panel is not None:
        try:
            panel.destroy()
        except Exception:
            pass
        plugin_panel._download_locations_overlay = None


def build_downloads_tab(plugin_panel, tab) -> "DownloadsPanel":
    """Build the Downloads tab inside *tab* and return the DownloadsPanel.

    Sets ``plugin_panel._downloads_panel`` for external getattr lookups
    (e.g. ``app._plugin_panel._downloads_panel`` in gui.py).
    """
    panel = DownloadsPanel(
        tab,
        log_fn=plugin_panel._log,
        install_fn=lambda p: _install_from_downloads(plugin_panel, p),
        on_open_locations=lambda: _open_download_locations_overlay(plugin_panel),
        get_installed_filenames=lambda: _get_installed_filenames(plugin_panel),
    )
    plugin_panel._downloads_panel = panel
    return panel

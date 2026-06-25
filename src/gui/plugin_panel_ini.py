"""
Ini Files tab mixin for PluginPanel.

Owns the ini/json/toml file list:
- Tab construction with combined scrollbar + marker strip.
- Filename search + content keyword search across a broader extension set
  (.txt/.cfg/.yaml/.xml etc).
- Vanilla game-folder + profile-level ini collection alongside filemap entries.
- Row click → opens the ini editor overlay on the toplevel app.

Host (PluginPanel) owns: ``self._game``, ``self._tabs``, ``self._log``,
``self._safe_after``, ``self._get_filemap_path``, ``self._staging_root``,
``self._parse_filemap`` (provided by the Data mixin), and the ini-tab state
initialised in ``PluginPanel.__init__`` (``_ini_files_tab_dirty``).
"""

import os
import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path

import customtkinter as ctk

import gui.theme as _theme
from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_HOVER_ROW,
    BG_LIST,
    BG_PANEL,
    BORDER,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_OK,
    TEXT_ON_ACCENT,
    TAG_INI_PROFILE,
    scaled,
)
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT


class PluginPanelIniMixin:
    """Ini/json/toml file list with filename search + content search."""

    _INI_JSON_EXTENSIONS = frozenset({
        ".ini", ".json", ".toml", ".txt", ".cfg", ".conf", ".config",
        ".yaml", ".yml", ".xml", ".log", ".md",
    })
    _INI_CONTENT_SEARCH_EXTENSIONS = frozenset({
        ".ini", ".json", ".toml", ".txt", ".cfg", ".conf", ".config",
        ".yaml", ".yml", ".xml", ".log", ".md",
    })

    @staticmethod
    def _ini_display_name(rel_path: str) -> str:
        """Return '<parent>/<filename>' when the file is nested, else just '<filename>'."""
        p = Path(rel_path)
        if p.parent != Path("."):
            return f"{p.parent.name}/{p.name}"
        return p.name

    def _build_ini_files_tab(self):
        """Build the Ini Files tab: list of ini/json files with search and marker strip."""
        tab = self._tabs.tab("Ini Files")
        tab.configure(fg_color=BG_LIST)
        tab.grid_rowconfigure(3, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        # Toolbar with Refresh and Search
        toolbar = tk.Frame(tab, bg=BG_HEADER, height=scaled(28), highlightthickness=0)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        ctk.CTkButton(
            toolbar, text="↺ Refresh", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_MAIN,
            font=_theme.FONT_HEADER, corner_radius=4,
            command=self._refresh_ini_files_tab,
        ).pack(side="left", padx=8, pady=2)

        ctk.CTkButton(
            toolbar, text="Search Content", width=140, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_MAIN,
            font=_theme.FONT_HEADER, corner_radius=4,
            command=self._on_search_ini_content,
        ).pack(side="left", padx=(0, 8), pady=2)

        self._ini_filter_btn = ctk.CTkButton(
            toolbar, text="Filters", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            font=_theme.FONT_HEADER, corner_radius=4,
            command=self._toggle_ini_filter_panel,
        )
        self._ini_filter_btn.pack(side="left", padx=(0, 8), pady=2)
        self._build_ini_filter_side_panel()

        # Content-filter status row (row 1) — hidden when no content filter is active
        self._ini_content_status_row = tk.Frame(tab, bg=BG_HEADER, highlightthickness=0)
        self._ini_content_status_row.grid(row=1, column=0, sticky="ew")
        self._ini_content_status_row.grid_remove()
        self._ini_content_status_var = tk.StringVar(value="")
        self._ini_content_status_lbl = tk.Label(
            self._ini_content_status_row, textvariable=self._ini_content_status_var,
            bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, _theme.FS10),
        )
        self._ini_content_status_lbl.pack(side="left", padx=(8, 6), pady=2)
        self._ini_content_clear_btn = ctk.CTkButton(
            self._ini_content_status_row, text="✕ Clear", width=60, height=22,
            fg_color=BG_HOVER, hover_color=BG_HOVER_ROW, text_color=TEXT_MAIN,
            font=(_theme.FONT_FAMILY, _theme.CTK_FS10), corner_radius=4,
            command=self._clear_ini_content_filter,
        )
        self._ini_content_clear_btn.pack(side="left", padx=(0, 8), pady=2)

        # Inline content-search bar (row 2) — hidden by default
        self._build_ini_content_search_bar(tab)

        # List frame: tree | combined scrollbar+marker strip
        list_frame = tk.Frame(tab, bg=BG_LIST)
        list_frame.grid(row=3, column=0, sticky="nsew")
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        _bg = BG_LIST
        _fg = TEXT_MAIN
        _style_name = "IniFiles.Treeview"
        style = ttk.Style()
        style.theme_use("default")

        # Custom expand/collapse arrow — same icon as the Data tab. Falls back to
        # the native indicator inside the Flatpak sandbox (PNG decode quirks).
        from gui.ctk_components import _is_flatpak_sandbox
        use_default_indicator = _is_flatpak_sandbox()
        if not use_default_indicator:
            from PIL import Image as PilImage, ImageTk
            from gui.ctk_components import ICON_PATH as _ICON_PATH, _load_icon_image as _load_iim
            _im_open = _load_iim(_ICON_PATH.get("arrow"))
            _im_close = _im_open.rotate(90)
            _im_empty = PilImage.new("RGB", (15, 15), BG_DEEP)
            _img_open_i = ImageTk.PhotoImage(_im_open, name="img_open_ini", size=(15, 15))
            _img_close_i = ImageTk.PhotoImage(_im_close, name="img_close_ini", size=(15, 15))
            _img_empty_i = ImageTk.PhotoImage(_im_empty, name="img_empty_ini", size=(15, 15))
            self._ini_arrow_images = (_img_open_i, _img_close_i, _img_empty_i)
            try:
                style.element_create("Treeitem.iniindicator", "image", "img_close_ini",
                    ("user1", "img_open_ini"), ("user2", "img_empty_ini"),
                    sticky="w", width=15, height=15)
            except Exception:
                pass
        try:
            indicator_elem = "Treeitem.indicator" if use_default_indicator else "Treeitem.iniindicator"
            style.layout(f"{_style_name}.Item", [
                ("Treeitem.padding", {"sticky": "nsew", "children": [
                    (indicator_elem, {"side": "left", "sticky": "nsew"}),
                    ("Treeitem.image", {"side": "left", "sticky": "nsew"}),
                    ("Treeitem.focus", {"side": "left", "sticky": "nsew", "children": [
                        ("Treeitem.text", {"side": "left", "sticky": "nsew"}),
                    ]}),
                ]}),
            ])
        except Exception:
            pass

        style.configure(_style_name,
            background=_bg, foreground=_fg,
            fieldbackground=_bg, borderwidth=0,
            rowheight=scaled(22), font=(_theme.FONT_FAMILY, _theme.FS10),
            focuscolor=_bg,
        )
        style.map(_style_name,
            background=[("selected", _bg), ("focus", _bg)],
            foreground=[("selected", ACCENT)],
        )
        style.configure(f"{_style_name}.Heading",
            background=_bg, foreground=_fg,
            font=(_theme.FONT_FAMILY, _theme.FS10, "bold"), relief="flat",
        )
        self._ini_files_tree = ttk.Treeview(
            list_frame, columns=("mod",), style=_style_name,
            selectmode="browse", show="tree headings",
        )
        self._ini_files_tree.heading("#0", text="File", anchor="w")
        self._ini_files_tree.heading("mod", text="Mod", anchor="w")
        self._ini_files_tree.column("#0", minwidth=150, stretch=True)
        self._ini_files_tree.column("mod", minwidth=120, stretch=True)
        self._ini_files_tree.tag_configure("mod_highlight", background=_theme.plugin_mod, foreground=TEXT_MAIN)
        self._ini_files_tree.tag_configure("game_folder", foreground=TEXT_OK)
        self._ini_files_tree.tag_configure("profile_folder", foreground=TAG_INI_PROFILE)
        self._ini_files_tree.tag_configure("mygames_folder", foreground=ACCENT)
        self._ini_files_tree.tag_configure(
            "category", foreground=TEXT_MAIN,
            font=(_theme.FONT_FAMILY, _theme.FS10, "bold"),
        )

        # Combined scrollbar + marker strip — same pattern as modlist_panel /
        # plugins tab: one canvas paints trough, ticks, and thumb.
        self._INI_SCROLL_W = 16
        self._ini_marker_strip = tk.Canvas(
            list_frame, bg=BG_DEEP, bd=0, highlightthickness=0,
            width=self._INI_SCROLL_W, takefocus=0,
        )
        self._ini_vsb = self._ini_marker_strip  # alias kept for any external refs
        self._ini_files_tree.configure(yscrollcommand=self._ini_scroll_set)

        self._ini_files_tree.grid(row=0, column=0, sticky="nsew")
        self._ini_marker_strip.grid(row=0, column=1, sticky="ns")

        self._ini_scroll_first = 0.0
        self._ini_scroll_last = 1.0
        self._ini_thumb_drag_offset: float | None = None

        self._ini_marker_strip.bind("<Configure>",        self._on_ini_marker_strip_resize)
        self._ini_marker_strip.bind("<ButtonPress-1>",    self._on_ini_scrollbar_press)
        self._ini_marker_strip.bind("<B1-Motion>",        self._on_ini_scrollbar_drag)
        self._ini_marker_strip.bind("<ButtonRelease-1>",  self._on_ini_scrollbar_release)
        self._ini_marker_strip.bind("<Button-4>",         lambda e: self._ini_files_tree.yview_scroll(-3, "units"))
        self._ini_marker_strip.bind("<Button-5>",         lambda e: self._ini_files_tree.yview_scroll(3, "units"))
        self._ini_marker_strip.bind("<MouseWheel>",       self._on_ini_mousewheel)

        # Search bar (bottom)
        ini_search_bar = tk.Frame(tab, bg=BG_HEADER, highlightthickness=0)
        ini_search_bar.grid(row=4, column=0, sticky="ew")
        tk.Label(
            ini_search_bar, text="Search:", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, _theme.FS10),
        ).pack(side="left", padx=(8, 4), pady=3)
        self._ini_search_var = tk.StringVar()
        self._ini_search_var.trace_add("write", self._on_ini_search_changed)
        self._ini_search_entry = tk.Entry(
            ini_search_bar, textvariable=self._ini_search_var,
            bg=BG_DEEP, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=(_theme.FONT_FAMILY, _theme.FS10),
            highlightthickness=0, highlightbackground=BG_DEEP,
        )
        self._ini_search_entry.pack(side="left", padx=(0, 8), pady=3, fill="x", expand=True)
        self._ini_search_entry.bind("<Escape>", lambda e: self._ini_search_var.set(""))
        def _ini_select_all(evt):
            evt.widget.select_range(0, tk.END)
            evt.widget.icursor(tk.END)
            return "break"
        self._ini_search_entry.bind("<Control-a>", _ini_select_all)

        self._ini_files_tree.bind("<<TreeviewSelect>>", self._on_ini_file_select)
        self._ini_files_tree.bind("<<TreeviewOpen>>",  lambda _e: self._on_ini_marker_strip_resize())
        self._ini_files_tree.bind("<<TreeviewClose>>", lambda _e: self._on_ini_marker_strip_resize())
        if not LEGACY_WHEEL_REDUNDANT:
            self._ini_files_tree.bind("<Button-4>", lambda e: self._ini_files_tree.yview_scroll(-3, "units"))
            self._ini_files_tree.bind("<Button-5>", lambda e: self._ini_files_tree.yview_scroll(3, "units"))

        self._ini_files_entries: list[tuple[str, str, Path]] = []  # full list
        self._ini_files_displayed: list[tuple[str, str, Path]] = []  # filtered for display
        self._ini_files_status: str | None = None  # "load"|"nofile"|None
        self._highlighted_ini_mod: str | None = None
        self._ini_marker_strip_after_id: str | None = None
        self._ini_content_query: str | None = None
        self._ini_content_matches: set[tuple[str, str]] | None = None
        self._ini_content_extra_entries: list[tuple[str, str, Path]] = []
        self._ini_filter_extensions: set[str] = set()           # include-only
        self._ini_filter_extensions_exclude: set[str] = set()   # hide these
        # Source filter — one of {"mod", "profile", "game"} per entry.
        # Empty include set = "all sources allowed".
        self._ini_filter_sources: set[str] = set()
        self._ini_filter_sources_exclude: set[str] = set()
        self._ini_filter_panel_open: bool = False
        # tree item iid → entry index into _ini_files_displayed (file rows only;
        # category header rows are absent from this map).
        self._ini_tree_item_entry: dict[str, int] = {}

    def _build_ini_filter_side_panel(self) -> None:
        """Build the Ini Files filter side panel — same pattern as the Data /
        Downloads filter panels, sharing column 0 on the ModListPanel."""
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        parent = mod_panel if mod_panel is not None else self
        panel = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=0, width=380)
        panel.grid(row=0, column=0, rowspan=5, sticky="nsew")
        panel.grid_propagate(False)
        panel.grid_remove()
        self._ini_filter_side_panel = panel

        header = tk.Frame(panel, bg=BG_HEADER, height=scaled(36))
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(
            header, text="Ini Filters", bg=BG_HEADER, fg=TEXT_MAIN,
            font=_theme.TK_FONT_BOLD, anchor="w",
        ).pack(side="left", padx=10, pady=6)

        close_btn = tk.Label(
            header, text="×", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, 16, "bold"), cursor="hand2",
        )
        close_btn.pack(side="right", padx=8)
        close_btn.bind("<Button-1>", lambda _e: self._close_ini_filter_panel())
        close_btn.bind("<Enter>",    lambda _e: close_btn.configure(fg=TEXT_MAIN))
        close_btn.bind("<Leave>",    lambda _e: close_btn.configure(fg=TEXT_DIM))

        clear_btn = tk.Label(
            header, text="Clear all", bg=BG_HEADER, fg=TEXT_DIM,
            font=_theme.TK_FONT_SMALL, cursor="hand2",
        )
        clear_btn.pack(side="right", padx=(0, 4))
        clear_btn.bind("<Button-1>", lambda _e: self._clear_all_ini_filters())
        clear_btn.bind("<Enter>",    lambda _e: clear_btn.configure(fg=TEXT_MAIN))
        clear_btn.bind("<Leave>",    lambda _e: clear_btn.configure(fg=TEXT_DIM))

        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x")

        scroll_frame = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0,
        )
        scroll_frame.pack(fill="both", expand=True, padx=8, pady=6)
        self._ini_filter_scroll_frame = scroll_frame

        tk.Label(
            scroll_frame, text="By file type",
            font=_theme.TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_PANEL, anchor="w",
        ).pack(anchor="w", pady=(2, 4))

        self._ifsp_filetype_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        self._ifsp_filetype_frame.pack(anchor="w", fill="x", pady=(2, 0))
        self._ifsp_filetype_vars = {}

        tk.Label(
            scroll_frame, text="By source",
            font=_theme.TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_PANEL, anchor="w",
        ).pack(anchor="w", pady=(10, 4))

        self._ifsp_source_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        self._ifsp_source_frame.pack(anchor="w", fill="x", pady=(2, 0))
        self._ifsp_source_vars: dict[str, tk.IntVar] = {}

        self._bind_ini_filter_panel_scroll()

    def _bind_ini_filter_panel_scroll(self) -> None:
        scroll_frame = getattr(self, "_ini_filter_scroll_frame", None)
        if not scroll_frame or not hasattr(scroll_frame, "_parent_canvas"):
            return
        step = 2

        def _on_wheel(evt):
            num = getattr(evt, "num", None)
            delta = getattr(evt, "delta", 0) or 0
            if num == 4 or delta > 0:
                scroll_frame._parent_canvas.yview_scroll(-step, "units")
            elif num == 5 or delta < 0:
                scroll_frame._parent_canvas.yview_scroll(step, "units")
            return "break"

        _legacy = None if LEGACY_WHEEL_REDUNDANT else _on_wheel

        def _bind_recursive(w):
            if _legacy is not None:
                w.bind("<Button-4>", _legacy)
                w.bind("<Button-5>", _legacy)
            for child in w.winfo_children():
                _bind_recursive(child)

        _bind_recursive(scroll_frame)

    def _get_ini_filetype_counts(self) -> "dict[str, int]":
        """Extension (lowercase, with dot) → count across the full ini entries list."""
        counts: dict[str, int] = {}
        for rel_path, _mod, _p in self._ini_files_entries:
            ext = Path(rel_path).suffix.lower()
            if ext:
                counts[ext] = counts.get(ext, 0) + 1
        return counts

    @staticmethod
    def _ini_entry_source(mod_name: str) -> str:
        """Classify an ini entry's origin from its mod_name field.

        Synthetic names "Game Folder" / "Profile" / "My Games" mark non-mod
        sources; anything else is a real mod folder.
        """
        if mod_name == "Game Folder":
            return "game"
        if mod_name == "Profile":
            return "profile"
        if mod_name == "My Games":
            return "mygames"
        return "mod"

    _INI_SOURCE_LABELS = (
        ("mod",     "Mod folders"),
        ("profile", "Profile"),
        ("game",    "Game folder"),
        ("mygames", "My Games"),
    )
    # Display ordering for the "location" grouping — entries are grouped by
    # source in this order, then sorted alphabetically within each group.
    _INI_SOURCE_ORDER = {key: i for i, (key, _label) in enumerate(_INI_SOURCE_LABELS)}

    def _ini_sort_key(self, entry: "tuple[str, str, Path]") -> tuple:
        """Sort key: group by source location first, then alphabetically by
        file path then mod name within each location."""
        rel_path, mod_name, _p = entry
        src = self._ini_entry_source(mod_name)
        return (
            self._INI_SOURCE_ORDER.get(src, len(self._INI_SOURCE_ORDER)),
            rel_path.lower(),
            mod_name.lower(),
        )

    def _get_ini_source_counts(self) -> "dict[str, int]":
        counts: dict[str, int] = {}
        for _rel, mod_name, _p in self._ini_files_entries:
            src = self._ini_entry_source(mod_name)
            counts[src] = counts.get(src, 0) + 1
        return counts

    def _refresh_ini_filter_source_list(self) -> None:
        frame = getattr(self, "_ifsp_source_frame", None)
        if frame is None:
            return
        for w in frame.winfo_children():
            w.destroy()
        self._ifsp_source_vars.clear()
        counts = self._get_ini_source_counts()
        from gui.tri_state_checkbox import TriStateCheckBox
        for key, label in self._INI_SOURCE_LABELS:
            count = counts.get(key, 0)
            if key in self._ini_filter_sources:
                init = 1
            elif key in self._ini_filter_sources_exclude:
                init = 2
            else:
                init = 0
            var = tk.IntVar(value=init)
            self._ifsp_source_vars[key] = var
            TriStateCheckBox(
                frame,
                text=f"{label}  ({count:,})",
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_ini_filter_panel_change,
            ).pack(anchor="w", pady=2)
        self._bind_ini_filter_panel_scroll()

    def _refresh_ini_filter_filetype_list(self) -> None:
        frame = self._ifsp_filetype_frame
        if frame is None:
            return
        for w in frame.winfo_children():
            w.destroy()
        self._ifsp_filetype_vars.clear()
        counts = self._get_ini_filetype_counts()
        if not counts:
            ctk.CTkLabel(
                frame, text="(no files in Ini tab)",
                font=_theme.FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            ).pack(anchor="w", pady=2)
            self._bind_ini_filter_panel_scroll()
            return
        from gui.tri_state_checkbox import TriStateCheckBox
        for ext, count in sorted(counts.items(), key=lambda kv: kv[0]):
            if ext in self._ini_filter_extensions:
                init = 1
            elif ext in self._ini_filter_extensions_exclude:
                init = 2
            else:
                init = 0
            var = tk.IntVar(value=init)
            self._ifsp_filetype_vars[ext] = var
            TriStateCheckBox(
                frame,
                text=f"{ext}  ({count:,})",
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_ini_filter_panel_change,
            ).pack(anchor="w", pady=2)
        self._bind_ini_filter_panel_scroll()

    def _on_ini_filter_panel_change(self) -> None:
        self._ini_filter_extensions = {
            ext for ext, v in self._ifsp_filetype_vars.items() if v.get() == 1
        }
        self._ini_filter_extensions_exclude = {
            ext for ext, v in self._ifsp_filetype_vars.items() if v.get() == 2
        }
        self._ini_filter_sources = {
            k for k, v in self._ifsp_source_vars.items() if v.get() == 1
        }
        self._ini_filter_sources_exclude = {
            k for k, v in self._ifsp_source_vars.items() if v.get() == 2
        }
        self._update_ini_filter_btn_color()
        self._apply_ini_search_filter()

    def _clear_all_ini_filters(self) -> None:
        self._ini_filter_extensions = set()
        self._ini_filter_extensions_exclude = set()
        self._ini_filter_sources = set()
        self._ini_filter_sources_exclude = set()
        for v in self._ifsp_filetype_vars.values():
            v.set(0)
        for v in self._ifsp_source_vars.values():
            v.set(0)
        self._refresh_ini_filter_filetype_list()
        self._refresh_ini_filter_source_list()
        self._update_ini_filter_btn_color()
        self._apply_ini_search_filter()

    def _toggle_ini_filter_panel(self) -> None:
        if getattr(self, "_ini_filter_panel_open", False):
            self._close_ini_filter_panel()
        else:
            self._open_ini_filter_panel()

    def _open_ini_filter_panel(self) -> None:
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        if mod_panel is None or self._ini_filter_side_panel is None:
            return
        # Mutual exclusion with the other filter panels that share column 0.
        if getattr(mod_panel, "_filter_panel_open", False):
            try:
                mod_panel._close_filter_side_panel()
            except Exception:
                pass
        if getattr(self, "_plugin_filter_panel_open", False):
            try:
                self._close_plugin_filter_panel()
            except Exception:
                pass
        if getattr(self, "_data_filter_panel_open", False):
            try:
                self._close_data_filter_panel()
            except Exception:
                pass
        if getattr(self, "_mf_filter_panel_open", False):
            try:
                self._close_mf_filter_panel()
            except Exception:
                pass
        app = self.winfo_toplevel()
        dl_panel = getattr(app, "_downloads_panel", None)
        if dl_panel is not None and getattr(dl_panel, "_filter_panel_open", False):
            try:
                dl_panel._close_filter_side_panel()
            except Exception:
                pass
        self._ini_filter_panel_open = True
        mod_panel.grid_columnconfigure(0, minsize=scaled(380))
        self._ini_filter_side_panel.grid()
        self._refresh_ini_filter_filetype_list()
        self._refresh_ini_filter_source_list()
        self._update_ini_filter_btn_color()

    def _close_ini_filter_panel(self) -> None:
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        self._ini_filter_panel_open = False
        if mod_panel is not None:
            mod_panel.grid_columnconfigure(0, minsize=0)
        if self._ini_filter_side_panel is not None:
            self._ini_filter_side_panel.grid_remove()
        self._update_ini_filter_btn_color()

    def _update_ini_filter_btn_color(self) -> None:
        btn = getattr(self, "_ini_filter_btn", None)
        if btn is None:
            return
        if (self._ini_filter_extensions or self._ini_filter_extensions_exclude
                or self._ini_filter_sources or self._ini_filter_sources_exclude):
            btn.configure(fg_color=ACCENT_HOV, hover_color=ACCENT_HOV)
        else:
            btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOV)

    def _resolve_ini_file_path(self, rel_path: str, mod_name: str) -> Path | None:
        """Resolve full file path from filemap entry. Returns None if staging_root unknown.

        Tries an exact path first; if that doesn't exist, walks each path segment
        case-insensitively to handle case-normalised filemap paths on Linux.
        """
        if self._staging_root is None:
            return None
        from Utils.filemap import OVERWRITE_NAME, ROOT_FOLDER_NAME
        rel_path = rel_path.replace("\\", "/")
        if mod_name == OVERWRITE_NAME:
            base = self._staging_root.parent / "overwrite"
        elif mod_name == ROOT_FOLDER_NAME:
            base = self._staging_root.parent / "Root_Folder"
        else:
            base = self._staging_root / mod_name
        exact = base / rel_path
        if exact.exists():
            return exact
        # Case-insensitive fallback: resolve each segment against the actual directory.
        current = base
        for segment in rel_path.split("/"):
            if not current.is_dir():
                return exact  # can't resolve further — return exact for display
            seg_lower = segment.lower()
            match = next(
                (child for child in current.iterdir() if child.name.lower() == seg_lower),
                None,
            )
            if match is None:
                return exact  # segment not found — return exact for display
            current = match
        return current

    def _refresh_ini_files_tab(self):
        """Populate Ini Files tab from filemap.txt, filtering to .ini and .json.

        Deferred when the Ini Files tab is not visible — rebuilt on tab switch.
        """
        try:
            if self._tabs.get() != "Ini Files":
                self._ini_files_tab_dirty = True
                return
        except Exception:
            pass
        self._ini_files_tab_dirty = False
        self._ini_files_entries.clear()
        if self._ini_content_matches is not None:
            self._ini_content_query = None
            self._ini_content_matches = None
            self._ini_content_extra_entries = []
            self._update_ini_content_status()

        filemap_path_str = self._get_filemap_path()
        if filemap_path_str is None or not self._staging_root:
            self._ini_files_displayed = []
            self._ini_files_status = "load"
            self._build_ini_tree_from_displayed()
            return

        filemap_path = Path(filemap_path_str)
        if not filemap_path.is_file():
            self._ini_files_displayed = []
            self._ini_files_status = "nofile"
            self._build_ini_tree_from_displayed()
            return
        self._ini_files_status = None

        entries = self._parse_filemap(filemap_path)
        ini_entries: list[tuple[str, str, Path]] = []
        for rel_path, mod_name in entries:
            ext = Path(rel_path).suffix.lower()
            if ext not in self._INI_JSON_EXTENSIONS:
                continue
            full_path = self._resolve_ini_file_path(rel_path, mod_name)
            if full_path is None:
                continue
            ini_entries.append((rel_path, mod_name, full_path))

        # Also scan the game folder for vanilla ini/json files (not hardlinks/symlinks).
        game_path = self._game.get_game_path() if self._game and hasattr(self._game, "get_game_path") else None
        if game_path and Path(game_path).is_dir():
            game_root = Path(game_path)
            for fpath in game_root.rglob("*"):
                if fpath.suffix.lower() not in self._INI_JSON_EXTENSIONS:
                    continue
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                # Skip symlinks and hardlinks (deployed files have nlink > 1)
                if fpath.is_symlink() or st.st_nlink > 1:
                    continue
                rel = fpath.relative_to(game_root).as_posix()
                ini_entries.append((rel, "Game Folder", fpath))

        # Also include profile-level ini files (the ones that get symlinked into My Games).
        for rel, fpath in self._collect_profile_ini_files(self._INI_JSON_EXTENSIONS):
            ini_entries.append((rel, "Profile", fpath))

        # Also include real files living in the prefix's My Games folder (Bethesda
        # games) — INIs the game writes itself plus logs, handy for viewing.
        for rel, fpath in self._collect_mygames_ini_files(self._INI_JSON_EXTENSIONS):
            ini_entries.append((rel, "My Games", fpath))

        self._ini_files_entries = sorted(ini_entries, key=self._ini_sort_key)
        self._apply_ini_search_filter()

    # Top-level subfolders of a profile dir that are already surfaced by other
    # sources (per-mod staging, the Overwrite mod, root-folder staging) or hold
    # backups — walking them here would dump thousands of duplicate mod files
    # under the synthetic "Profile" source.
    _PROFILE_SCAN_SKIP_DIRS = frozenset({
        "mods", "overwrite", "root_folder", "backups", "fomod",
    })

    def _collect_profile_ini_files(self, extensions: "frozenset[str]") -> "list[tuple[str, Path]]":
        """Return (rel_path, full_path) for config/text files anywhere in the
        active profile folder (recursive), e.g. modlist.txt, filemap.txt,
        data_deployed.txt, collection.json, loot.json, and the Bethesda
        'ini files' subdir that gets symlinked into My Games.

        rel_path is relative to the profile folder (POSIX). Mod staging,
        overwrite, root-folder and backup subfolders are skipped — those files
        already appear under their own sources. Returns [] if no profile dir is
        known."""
        profile_dir = getattr(self._game, "_active_profile_dir", None) if self._game else None
        if not profile_dir:
            return []
        profile_root = Path(profile_dir)
        if not profile_root.is_dir():
            return []
        results: list[tuple[str, Path]] = []
        try:
            for dirpath, dirnames, filenames in os.walk(profile_root):
                # Prune skipped subtrees at the top level only (so a mod named
                # e.g. "logs" deeper in the tree isn't accidentally excluded).
                if Path(dirpath) == profile_root:
                    dirnames[:] = [
                        d for d in dirnames
                        if d.lower() not in self._PROFILE_SCAN_SKIP_DIRS
                    ]
                for name in filenames:
                    fpath = Path(dirpath) / name
                    if fpath.suffix.lower() not in extensions:
                        continue
                    if not fpath.is_file() or fpath.is_symlink():
                        continue
                    rel = fpath.relative_to(profile_root).as_posix()
                    results.append((rel, fpath))
        except OSError:
            return []
        return results

    def _collect_mygames_ini_files(self, extensions: "frozenset[str]") -> "list[tuple[str, Path]]":
        """Return (rel_path, full_path) for config/log files in the game's My Games
        folder(s) inside the Proton prefix (Bethesda games only).

        rel_path is relative to the My Games root. Symlinks are skipped — those are
        the profile INIs already shown under the "Profile" source. Returns [] when
        the game has no ``_mygames_paths`` (non-Bethesda) or none exist yet.
        """
        mygames_fn = getattr(self._game, "_mygames_paths", None) if self._game else None
        if not callable(mygames_fn):
            return []
        try:
            mygames_dirs = mygames_fn()
        except Exception:
            return []
        results: list[tuple[str, Path]] = []
        seen_rel: set[str] = set()
        for mygames in mygames_dirs:
            mygames = Path(mygames)
            if not mygames.is_dir():
                continue
            try:
                for fpath in mygames.rglob("*"):
                    if fpath.suffix.lower() not in extensions:
                        continue
                    if fpath.is_symlink() or not fpath.is_file():
                        continue
                    rel = fpath.relative_to(mygames).as_posix()
                    if rel in seen_rel:
                        continue
                    seen_rel.add(rel)
                    results.append((rel, fpath))
            except OSError:
                continue
        return results

    def _on_ini_search_changed(self, *_):
        """Filter displayed ini files by search query (filename or mod name)."""
        self._apply_ini_search_filter()

    def _apply_ini_search_filter(self):
        """Apply search filter and rebuild tree."""
        query = self._ini_search_var.get().strip().casefold()
        content_matches = self._ini_content_matches
        entries = self._ini_files_entries
        if content_matches is not None:
            extra = getattr(self, "_ini_content_extra_entries", None) or []
            combined = list(entries) + list(extra)
            entries = [e for e in combined if (e[0], e[1]) in content_matches]
            entries.sort(key=self._ini_sort_key)
        ext_filter = self._ini_filter_extensions
        ext_exclude = self._ini_filter_extensions_exclude
        if ext_filter:
            entries = [e for e in entries if Path(e[0]).suffix.lower() in ext_filter]
        if ext_exclude:
            entries = [e for e in entries if Path(e[0]).suffix.lower() not in ext_exclude]
        src_filter = self._ini_filter_sources
        src_exclude = self._ini_filter_sources_exclude
        if src_filter:
            entries = [e for e in entries if self._ini_entry_source(e[1]) in src_filter]
        if src_exclude:
            entries = [e for e in entries if self._ini_entry_source(e[1]) not in src_exclude]
        if not query:
            self._ini_files_displayed = list(entries)
        else:
            self._ini_files_displayed = [
                (r, m, p) for r, m, p in entries
                if query in r.casefold() or query in m.casefold()
            ]
        self._build_ini_tree_from_displayed()

    def _build_ini_content_search_bar(self, tab):
        """Inline search bar shown at the top of the Ini Files tab (hidden by default)."""
        bar = tk.Frame(tab, bg=BG_HEADER, highlightthickness=0)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_remove()
        self._ini_content_search_bar = bar

        tk.Label(
            bar, text="Search content:", bg=BG_HEADER, fg=TEXT_MAIN,
            font=(_theme.FONT_FAMILY, _theme.FS10),
        ).pack(side="left", padx=(8, 4), pady=6)

        self._ini_content_search_var = tk.StringVar()
        self._ini_content_search_entry = ctk.CTkEntry(
            bar, textvariable=self._ini_content_search_var,
            font=(_theme.FONT_FAMILY, _theme.CTK_FS10),
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER,
            placeholder_text="e.g.  fCompassPosY",
            width=140, height=26,
        )
        self._ini_content_search_entry.pack(side="left", padx=(0, 6), pady=6, fill="x", expand=True)
        self._ini_content_search_entry.bind(
            "<Return>", lambda _e: self._on_ini_content_search_submit()
        )
        self._ini_content_search_entry.bind(
            "<Escape>", lambda _e: self.hide_ini_content_search_bar()
        )

        ctk.CTkButton(
            bar, text="Search", width=72, height=26, font=_theme.FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ini_content_search_submit,
        ).pack(side="left", padx=(0, 4), pady=6)

        ctk.CTkButton(
            bar, text="Cancel", width=72, height=26, font=_theme.FONT_NORMAL,
            fg_color=BG_HOVER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self.hide_ini_content_search_bar,
        ).pack(side="left", padx=(0, 8), pady=6)

    def _on_search_ini_content(self):
        """Toggle the inline content-search bar at the top of the tab."""
        bar = getattr(self, "_ini_content_search_bar", None)
        if bar is None:
            return
        if bar.winfo_manager():
            self.hide_ini_content_search_bar()
            return
        self._ini_content_search_var.set(self._ini_content_query or "")
        bar.grid()
        self._ini_content_search_entry.focus_set()
        try:
            self._ini_content_search_entry.select_range(0, tk.END)
        except Exception:
            pass

    def hide_ini_content_search_bar(self):
        bar = getattr(self, "_ini_content_search_bar", None)
        if bar is not None:
            bar.grid_remove()

    def _on_ini_content_search_submit(self):
        kw = self._ini_content_search_var.get().strip()
        if not kw:
            return
        self.hide_ini_content_search_bar()
        self._run_ini_content_search(kw)

    def _collect_ini_content_search_entries(self) -> list[tuple[str, str, Path]]:
        """Return every text-like config file from filemap + game folder for content search.
        Uses a broader extension set than the Ini Files tab (.txt/.cfg/.yaml/.xml etc)."""
        out: list[tuple[str, str, Path]] = []
        seen: set[tuple[str, str]] = set()

        for r, m, p in self._ini_files_entries:
            key = (r, m)
            if key in seen:
                continue
            seen.add(key)
            out.append((r, m, p))

        filemap_path_str = self._get_filemap_path()
        if filemap_path_str and self._staging_root:
            filemap_path = Path(filemap_path_str)
            if filemap_path.is_file():
                for rel_path, mod_name in self._parse_filemap(filemap_path):
                    ext = Path(rel_path).suffix.lower()
                    if ext not in self._INI_CONTENT_SEARCH_EXTENSIONS:
                        continue
                    key = (rel_path, mod_name)
                    if key in seen:
                        continue
                    full_path = self._resolve_ini_file_path(rel_path, mod_name)
                    if full_path is None:
                        continue
                    seen.add(key)
                    out.append((rel_path, mod_name, full_path))

        game_path = self._game.get_game_path() if self._game and hasattr(self._game, "get_game_path") else None
        if game_path and Path(game_path).is_dir():
            game_root = Path(game_path)
            for fpath in game_root.rglob("*"):
                if fpath.suffix.lower() not in self._INI_CONTENT_SEARCH_EXTENSIONS:
                    continue
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                if fpath.is_symlink() or st.st_nlink > 1:
                    continue
                rel = fpath.relative_to(game_root).as_posix()
                key = (rel, "Game Folder")
                if key in seen:
                    continue
                seen.add(key)
                out.append((rel, "Game Folder", fpath))

        for rel, fpath in self._collect_profile_ini_files(self._INI_CONTENT_SEARCH_EXTENSIONS):
            key = (rel, "Profile")
            if key in seen:
                continue
            seen.add(key)
            out.append((rel, "Profile", fpath))

        for rel, fpath in self._collect_mygames_ini_files(self._INI_CONTENT_SEARCH_EXTENSIONS):
            key = (rel, "My Games")
            if key in seen:
                continue
            seen.add(key)
            out.append((rel, "My Games", fpath))

        return out

    def _run_ini_content_search(self, keyword: str):
        """Scan every text-like config file (broad extension set) for keyword (case-insensitive)."""
        needle = keyword.casefold()
        candidates = self._collect_ini_content_search_entries()
        matched_entries: list[tuple[str, str, Path]] = []
        for rel_path, mod_name, full_path in candidates:
            try:
                if not full_path.is_file():
                    continue
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    if needle in f.read().casefold():
                        matched_entries.append((rel_path, mod_name, full_path))
            except OSError:
                continue

        self._ini_content_query = keyword
        self._ini_content_matches = {(r, m) for r, m, _ in matched_entries}
        self._ini_content_extra_entries = [
            (r, m, p) for r, m, p in matched_entries
            if (r, m) not in {(er, em) for er, em, _ in self._ini_files_entries}
        ]
        self._update_ini_content_status()
        self._apply_ini_search_filter()

    def _clear_ini_content_filter(self):
        self._ini_content_query = None
        self._ini_content_matches = None
        self._ini_content_extra_entries = []
        self._update_ini_content_status()
        self._apply_ini_search_filter()

    def _update_ini_content_status(self):
        if self._ini_content_matches is None:
            self._ini_content_status_var.set("")
            try:
                self._ini_content_status_row.grid_remove()
            except Exception:
                pass
        else:
            n = len(self._ini_content_matches)
            q = self._ini_content_query or ""
            self._ini_content_status_var.set(f"Content: \"{q}\"  ({n} match{'es' if n != 1 else ''})")
            try:
                self._ini_content_status_row.grid()
            except Exception:
                pass

    def _ini_row_tags(self, mod_name: str) -> tuple:
        """Treeview tags for a file row, by its source/mod name."""
        if self._highlighted_ini_mod and mod_name == self._highlighted_ini_mod:
            return ("mod_highlight",)
        if mod_name == "Game Folder":
            return ("game_folder",)
        if mod_name == "Profile":
            return ("profile_folder",)
        if mod_name == "My Games":
            return ("mygames_folder",)
        return ()

    def _build_ini_tree_from_displayed(self):
        """Rebuild tree from _ini_files_displayed, grouped by source location.

        Each location ("Mod folders", "Profile", "Game folder", "My Games")
        becomes an expandable top-level folder holding its file rows.
        """
        self._ini_files_tree.delete(*self._ini_files_tree.get_children())
        self._ini_tree_item_entry = {}
        status = getattr(self, "_ini_files_status", None)
        if status == "load":
            self._ini_files_tree.insert("", "end", text="(load a game first)", values=("",))
            return
        if status == "nofile":
            self._ini_files_tree.insert("", "end", text="(filemap.txt not found)", values=("",))
            return
        if not self._ini_files_displayed:
            if self._ini_search_var.get().strip() or self._ini_content_matches is not None:
                self._ini_files_tree.insert("", "end", text="(no matches)", values=("",))
            else:
                self._ini_files_tree.insert("", "end", text="(no ini/json files in filemap)", values=("",))
            return

        labels = dict(self._INI_SOURCE_LABELS)
        # Start categories collapsed so the tab opens tidy; auto-expand only when
        # a search/content filter is active so matches stay visible.
        open_categories = bool(
            self._ini_search_var.get().strip() or self._ini_content_matches is not None
        )
        category_nodes: dict[str, str] = {}
        for idx, (rel_path, mod_name, _) in enumerate(self._ini_files_displayed):
            src = self._ini_entry_source(mod_name)
            parent = category_nodes.get(src)
            if parent is None:
                parent = self._ini_files_tree.insert(
                    "", "end", text=labels.get(src, src),
                    values=("",), tags=("category",), open=open_categories,
                )
                category_nodes[src] = parent
            iid = self._ini_files_tree.insert(
                parent, "end", text=self._ini_display_name(rel_path),
                values=(mod_name,), tags=self._ini_row_tags(mod_name),
            )
            self._ini_tree_item_entry[iid] = idx
        self._draw_ini_marker_strip()

    def _on_ini_marker_strip_resize(self, _event=None):
        if self._ini_marker_strip_after_id is not None:
            self.after_cancel(self._ini_marker_strip_after_id)
        self._ini_marker_strip_after_id = self.after(50, self._draw_ini_marker_strip)

    def _apply_ini_row_highlight(self):
        """Update row background (orange) for items belonging to the selected mod."""
        displayed = self._ini_files_displayed
        for iid, idx in self._ini_tree_item_entry.items():
            if idx >= len(displayed):
                continue
            _, mod_name, _ = displayed[idx]
            self._ini_files_tree.item(iid, tags=self._ini_row_tags(mod_name))

    def _draw_ini_marker_strip(self):
        """Paint the combined scrollbar + marker strip for the Ini Files tab.

        Layers (bottom → top):
          1. Trough background
          2. Orange tick marks for ini/json files belonging to the selected mod
          3. Thumb rectangle
        """
        self._ini_marker_strip_after_id = None
        c = self._ini_marker_strip
        c.delete("all")
        strip_h = c.winfo_height()
        strip_w = c.winfo_width()
        if strip_h <= 1 or strip_w <= 1:
            return

        c.create_rectangle(0, 0, strip_w, strip_h, fill=BG_DEEP, outline="", tags="trough")

        # Coarse position hint: walk the visible tree rows (category headers +
        # expanded children) and place a tick at each highlighted-mod file row.
        if self._highlighted_ini_mod:
            visible = self._ini_visible_rows()
            n = len(visible)
            if n:
                highlighted_rows = [
                    i for i, iid in enumerate(visible)
                    if self._ini_row_mod_name(iid) == self._highlighted_ini_mod
                ]
                if highlighted_rows:
                    strip_max = strip_h - 4
                    inv_n = 1.0 / n
                    color = _theme.plugin_mod
                    for row_idx in highlighted_rows:
                        y = int(row_idx * inv_n * strip_h)
                        if y < 2:
                            y = 2
                        elif y > strip_max:
                            y = strip_max
                        c.create_rectangle(0, y, strip_w, y + 3, fill=color, outline="", tags="marker")

        self._redraw_ini_thumb()

    def _ini_visible_rows(self) -> "list[str]":
        """Tree iids in display order, descending into expanded folders only —
        matches what the user actually sees / what the scrollbar spans."""
        out: list[str] = []
        for cat in self._ini_files_tree.get_children(""):
            out.append(cat)
            if self._ini_files_tree.item(cat, "open"):
                out.extend(self._ini_files_tree.get_children(cat))
        return out

    def _ini_row_mod_name(self, iid: str) -> "str | None":
        """Mod name for a file-row iid (None for category headers)."""
        idx = self._ini_tree_item_entry.get(iid)
        if idx is None or idx >= len(self._ini_files_displayed):
            return None
        return self._ini_files_displayed[idx][1]

    def _redraw_ini_thumb(self) -> None:
        c = self._ini_marker_strip
        c.delete("thumb")
        strip_h = c.winfo_height()
        strip_w = c.winfo_width()
        if strip_h <= 1 or strip_w <= 1:
            return
        first = max(0.0, min(1.0, self._ini_scroll_first))
        last = max(first, min(1.0, self._ini_scroll_last))
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

    def _ini_scroll_set(self, first: str, last: str) -> None:
        try:
            f = float(first); l = float(last)
        except (TypeError, ValueError):
            return
        if f == self._ini_scroll_first and l == self._ini_scroll_last:
            return
        self._ini_scroll_first = f
        self._ini_scroll_last = l
        self._redraw_ini_thumb()

    def _on_ini_scrollbar_press(self, event):
        strip_h = self._ini_marker_strip.winfo_height()
        if strip_h <= 1:
            return
        first = self._ini_scroll_first
        last = self._ini_scroll_last
        thumb_top = first * strip_h
        thumb_bot = last * strip_h
        if thumb_top <= event.y <= thumb_bot:
            self._ini_thumb_drag_offset = (event.y - thumb_top) / strip_h
        else:
            self._ini_thumb_drag_offset = (last - first) / 2.0
            self._ini_scroll_to_pointer(event.y)

    def _on_ini_scrollbar_drag(self, event):
        if self._ini_thumb_drag_offset is None:
            return
        self._ini_scroll_to_pointer(event.y)

    def _on_ini_scrollbar_release(self, _event):
        self._ini_thumb_drag_offset = None

    def _ini_scroll_to_pointer(self, py: int) -> None:
        strip_h = self._ini_marker_strip.winfo_height()
        if strip_h <= 1 or self._ini_thumb_drag_offset is None:
            return
        frac = (py / strip_h) - self._ini_thumb_drag_offset
        frac = max(0.0, min(1.0, frac))
        self._ini_files_tree.yview_moveto(frac)

    def _on_ini_mousewheel(self, event):
        delta = event.delta
        if delta == 0:
            return
        step = -3 if delta > 0 else 3
        self._ini_files_tree.yview_scroll(step, "units")

    def _on_ini_file_select(self, _event=None):
        self._on_ini_file_edit()

    def _on_ini_file_edit(self):
        """Open the ini/json file editor overlay."""
        sel = self._ini_files_tree.selection()
        if not sel:
            return
        item = sel[0]
        idx = self._ini_tree_item_entry.get(item)
        # Category header rows (and placeholder rows) aren't in the map — clicking
        # one does nothing here; the native expand indicator handles open/close.
        if idx is None:
            return
        if idx < 0 or idx >= len(self._ini_files_displayed):
            return
        rel_path, mod_name, full_path = self._ini_files_displayed[idx]
        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_ini_editor_panel", None)
        if show_fn:
            show_fn(str(full_path), rel_path, mod_name, highlight=self._ini_content_query)

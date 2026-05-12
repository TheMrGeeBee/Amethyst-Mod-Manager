"""
Data tab mixin for PluginPanel.

Owns the deployed-files viewer:
- Tab construction with combined scrollbar + marker strip.
- Lazy realisation of folder children on first open.
- Routing-rule resolution so paths reflect actual deploy destination
  (UE5 ``_resolve_filemap_entries`` + custom_routing_rules folder/ext/filename).
- Search bar with debounced filtering and incremental refinement.
- Mod highlight strip (orange ticks for the currently-highlighted mod).

Host (PluginPanel) owns: ``self._game``, ``self._tabs``, ``self._log``,
``self._safe_after``, ``self._get_filemap_path``, ``self._get_conflict_cache``,
``self._show_simple_context_menu``, ``self._get_staging_path``,
``self._open_folder_in_browser``, the modlist callbacks
``self._on_plugin_selected_cb`` / ``self._on_mod_selected_cb``, and the
data-tab state initialised in ``PluginPanel.__init__`` (``_data_tab_dirty``,
``_data_filemap_entries``, ``_data_filemap_casefold``,
``_data_search_prev_query``, ``_data_search_prev_indices``,
``_data_search_after_id``, ``_data_resolved_cache``, ``_data_lazy_subtrees``,
``_data_realised_nodes``, ``_data_only_conflicts_var``,
``_highlighted_data_mod``).
"""

import tkinter as tk
import tkinter.ttk as ttk
from pathlib import Path

import customtkinter as ctk
from PIL import Image as PilImage, ImageTk

import gui.theme as _theme
from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_LIST,
    BG_PANEL,
    BORDER,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_ON_ACCENT,
    TAG_FOLDER,
    scaled,
)
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT


class PluginPanelDataMixin:
    """Deployed-files tree with rule resolution, search, and marker strip."""

    def _build_data_tab(self):
        tab = self._tabs.tab("Data")
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        toolbar = tk.Frame(tab, bg=BG_HEADER, height=scaled(28), highlightthickness=0)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)
        ctk.CTkButton(
            toolbar, text="↺ Refresh", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_MAIN,
            font=_theme.FONT_HEADER, corner_radius=4,
            command=self._refresh_data_tab,
        ).pack(side="left", padx=(8, 2), pady=2)

        # Filters button — styled to match the Downloads tab (blue accent).
        self._data_filter_btn = ctk.CTkButton(
            toolbar, text="Filters", width=72, height=26,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            font=_theme.FONT_HEADER, corner_radius=4,
            command=self._toggle_data_filter_panel,
        )
        self._data_filter_btn.pack(side="left", padx=(0, 8), pady=2)
        self._build_data_filter_side_panel()

        self._data_tree_expanded: bool = False
        self._data_expand_btn = ctk.CTkButton(
            toolbar, text="⊞ Expand All", width=110, height=26,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            font=_theme.FONT_HEADER, corner_radius=4,
            command=self._toggle_data_tree_expand,
        )
        self._data_expand_btn.pack(side="left", padx=(0, 8), pady=2)

        self._data_only_conflicts_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            toolbar, text="Show only conflicts",
            variable=self._data_only_conflicts_var,
            width=140, height=20,
            checkbox_width=16, checkbox_height=16,
            font=("Cantarell", _theme.FS10),
            text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            border_color=BORDER, checkmark_color="white",
            bg_color=BG_HEADER,
            command=self._refresh_data_tab,
        ).pack(side="left", padx=(0, 8), pady=2)

        # List frame: tree | combined scrollbar+marker strip — same pattern as
        # Ini Files tab so the orange row highlight is also visible on the strip.
        list_frame = tk.Frame(tab, bg=BG_LIST)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        from gui.ctk_components import _is_flatpak_sandbox
        style = ttk.Style()
        style.theme_use("default")
        use_default_indicator = _is_flatpak_sandbox()
        if not use_default_indicator:
            from gui.ctk_components import ICON_PATH as _ICON_PATH, _load_icon_image as _load_iim
            _im_open = _load_iim(_ICON_PATH.get("arrow"))
            _im_close = _im_open.rotate(90)
            _im_empty = PilImage.new("RGB", (15, 15), BG_DEEP)
            _img_open_d = ImageTk.PhotoImage(_im_open, name="img_open_data", size=(15, 15))
            _img_close_d = ImageTk.PhotoImage(_im_close, name="img_close_data", size=(15, 15))
            _img_empty_d = ImageTk.PhotoImage(_im_empty, name="img_empty_data", size=(15, 15))
            self._data_arrow_images = (_img_open_d, _img_close_d, _img_empty_d)
            try:
                style.element_create("Treeitem.dataindicator", "image", "img_close_data",
                    ("user1", "img_open_data"), ("user2", "img_empty_data"),
                    sticky="w", width=15, height=15)
            except Exception:
                pass
        try:
            indicator_elem = "Treeitem.indicator" if use_default_indicator else "Treeitem.dataindicator"
            style.layout("DataTab.Treeview.Item", [
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

        _bg = BG_LIST
        _fg = TEXT_MAIN
        style.configure("DataTab.Treeview",
            background=_bg, foreground=_fg,
            fieldbackground=_bg, borderwidth=0,
            rowheight=scaled(22), font=(_theme.FONT_FAMILY, _theme.FS10),
            focuscolor=_bg,
        )
        style.map("DataTab.Treeview",
            background=[("selected", _bg), ("focus", _bg)],
            foreground=[("selected", ACCENT)],
        )
        style.configure("DataTab.Treeview.Heading",
            background=_bg, foreground=_fg,
            font=(_theme.FONT_FAMILY, _theme.FS10, "bold"), relief="flat",
        )

        self._data_tree = ttk.Treeview(
            list_frame,
            columns=("mod",),
            style="DataTab.Treeview",
            selectmode="browse",
            show="tree headings",
        )
        self._data_tree.heading("#0", text="Path", anchor="w")
        self._data_tree.heading("mod", text="Winning Mod", anchor="w")
        self._data_tree.column("#0", minwidth=scaled(200), stretch=True)
        self._data_tree.column("mod", minwidth=scaled(160), stretch=True)
        # Existing call sites use `self._data_tree.treeview.X` (legacy from CTkTreeview);
        # alias to self so those keep working without churn.
        self._data_tree.treeview = self._data_tree

        # Combined scrollbar + marker strip
        self._DATA_SCROLL_W = 16
        self._data_marker_strip = tk.Canvas(
            list_frame, bg=BG_DEEP, bd=0, highlightthickness=0,
            width=self._DATA_SCROLL_W, takefocus=0,
        )
        self._data_tree.configure(yscrollcommand=self._data_scroll_set)

        self._data_tree.grid(row=0, column=0, sticky="nsew")
        self._data_marker_strip.grid(row=0, column=1, sticky="ns")

        self._data_scroll_first = 0.0
        self._data_scroll_last = 1.0
        self._data_thumb_drag_offset: float | None = None
        self._data_marker_strip_after_id: str | None = None
        self._highlighted_data_mod: str | None = None

        self._data_marker_strip.bind("<Configure>",        self._on_data_marker_strip_resize)
        self._data_marker_strip.bind("<ButtonPress-1>",    self._on_data_scrollbar_press)
        self._data_marker_strip.bind("<B1-Motion>",        self._on_data_scrollbar_drag)
        self._data_marker_strip.bind("<ButtonRelease-1>",  self._on_data_scrollbar_release)
        self._data_marker_strip.bind("<Button-4>",         lambda e: self._data_tree.yview_scroll(-3, "units"))
        self._data_marker_strip.bind("<Button-5>",         lambda e: self._data_tree.yview_scroll(3, "units"))
        self._data_marker_strip.bind("<MouseWheel>",       self._on_data_mousewheel)

        # Realise lazy children on first open, then redraw markers.
        self._data_tree.bind("<<TreeviewOpen>>",  self._on_data_tree_open)
        self._data_tree.bind("<<TreeviewClose>>", lambda e: self._draw_data_marker_strip())

        # Search bar (bottom)
        data_search_bar = tk.Frame(tab, bg=BG_HEADER, highlightthickness=0)
        data_search_bar.grid(row=2, column=0, sticky="ew")
        tk.Label(
            data_search_bar, text="Search:", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, _theme.FS10),
        ).pack(side="left", padx=(8, 4), pady=3)
        self._data_search_var = tk.StringVar()
        self._data_search_var.trace_add("write", self._on_data_search_changed)
        _data_search_entry = tk.Entry(
            data_search_bar, textvariable=self._data_search_var,
            bg=BG_DEEP, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=(_theme.FONT_FAMILY, _theme.FS10),
            highlightthickness=0, highlightbackground=BG_DEEP,
        )
        _data_search_entry.pack(side="left", padx=(0, 8), pady=3, fill="x", expand=True)
        _data_search_entry.bind("<Escape>", lambda e: self._data_search_var.set(""))
        def _data_select_all(evt):
            evt.widget.select_range(0, tk.END)
            evt.widget.icursor(tk.END)
            return "break"
        _data_search_entry.bind("<Control-a>", _data_select_all)

        if not LEGACY_WHEEL_REDUNDANT:
            self._data_tree.bind("<Button-4>",
                lambda e: self._data_tree.yview_scroll(-3, "units"))
            self._data_tree.bind("<Button-5>",
                lambda e: self._data_tree.yview_scroll(3, "units"))
        self._data_tree.bind("<<TreeviewSelect>>", self._on_data_file_selected)
        self._data_tree.bind("<Button-3>", self._on_data_right_click)

    # ------------------------------------------------------------------
    # Data filter side panel — build / open / close
    # ------------------------------------------------------------------

    def _build_data_filter_side_panel(self) -> None:
        """Build the Data tab filter side panel as a child of ModListPanel at column 0.

        Shares the same column as the modlist and plugin filter panels;
        mutual exclusion is handled in _open_data_filter_panel.
        """
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        parent = mod_panel if mod_panel is not None else self
        panel = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=0, width=380)
        panel.grid(row=0, column=0, rowspan=5, sticky="nsew")
        panel.grid_propagate(False)
        panel.grid_remove()
        self._data_filter_side_panel = panel

        header = tk.Frame(panel, bg=BG_HEADER, height=scaled(36))
        header.pack(fill="x", side="top")
        header.pack_propagate(False)

        tk.Label(
            header, text="Data Filters", bg=BG_HEADER, fg=TEXT_MAIN,
            font=_theme.FONT_BOLD, anchor="w",
        ).pack(side="left", padx=10, pady=6)

        close_btn = tk.Label(
            header, text="×", bg=BG_HEADER, fg=TEXT_DIM,
            font=(_theme.FONT_FAMILY, 16, "bold"), cursor="hand2",
        )
        close_btn.pack(side="right", padx=8)
        close_btn.bind("<Button-1>", lambda _e: self._close_data_filter_panel())
        close_btn.bind("<Enter>",    lambda _e: close_btn.configure(fg=TEXT_MAIN))
        close_btn.bind("<Leave>",    lambda _e: close_btn.configure(fg=TEXT_DIM))

        clear_btn = tk.Label(
            header, text="Clear all", bg=BG_HEADER, fg=TEXT_DIM,
            font=_theme.FONT_SMALL, cursor="hand2",
        )
        clear_btn.pack(side="right", padx=(0, 4))
        clear_btn.bind("<Button-1>", lambda _e: self._clear_all_data_filters())
        clear_btn.bind("<Enter>",    lambda _e: clear_btn.configure(fg=TEXT_MAIN))
        clear_btn.bind("<Leave>",    lambda _e: clear_btn.configure(fg=TEXT_DIM))

        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x")

        scroll_frame = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0,
        )
        scroll_frame.pack(fill="both", expand=True, padx=8, pady=6)

        ctk.CTkLabel(
            scroll_frame, text="Show only filetypes:",
            font=_theme.FONT_SMALL, text_color=TEXT_DIM, anchor="w",
        ).pack(anchor="w")
        self._dfsp_filetype_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
        self._dfsp_filetype_frame.pack(anchor="w", fill="x", pady=(2, 0))
        self._dfsp_filetype_vars: dict[str, tk.BooleanVar] = {}

        self._data_filter_scroll_frame = scroll_frame
        self._bind_data_filter_panel_scroll()

    def _bind_data_filter_panel_scroll(self) -> None:
        scroll_frame = getattr(self, "_data_filter_scroll_frame", None)
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

    def _refresh_data_filter_filetype_list(self) -> None:
        """Populate the filetype checkboxes from the current Data tab entries.

        Counts are taken from the resolved + deploy-filtered entry list
        (``_data_filemap_entries``) so they match what the tree shows.
        """
        for w in self._dfsp_filetype_frame.winfo_children():
            w.destroy()
        self._dfsp_filetype_vars.clear()
        counts = self._get_data_filetype_counts()
        if not counts:
            ctk.CTkLabel(
                self._dfsp_filetype_frame,
                text="(no files in Data tab)",
                font=_theme.FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            ).pack(anchor="w", pady=2)
            self._bind_data_filter_panel_scroll()
            return
        ordered = sorted(counts.items(), key=lambda kv: kv[0])
        for ext, count in ordered:
            var = tk.BooleanVar(value=ext in self._data_filter_filetypes)
            self._dfsp_filetype_vars[ext] = var
            ctk.CTkCheckBox(
                self._dfsp_filetype_frame,
                text=f"{ext}  ({count:,})",
                variable=var,
                font=_theme.FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                border_color=BORDER,
                checkmark_color="white",
                command=self._on_data_filter_panel_change,
            ).pack(anchor="w", pady=2)

        self._bind_data_filter_panel_scroll()

    def _get_data_filetype_counts(self) -> "dict[str, int]":
        """Extension (lowercase, with dot) → file count across the Data tab entries."""
        from os.path import splitext
        counts: dict[str, int] = {}
        for rel_path, _mod in self._data_filemap_entries:
            ext = splitext(rel_path)[1].lower()
            if ext:
                counts[ext] = counts.get(ext, 0) + 1
        return counts

    def _toggle_data_filter_panel(self) -> None:
        if getattr(self, "_data_filter_panel_open", False):
            self._close_data_filter_panel()
        else:
            self._open_data_filter_panel()

    def _open_data_filter_panel(self) -> None:
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        if mod_panel is None:
            return
        # Mutual exclusion with the other two filter panels — they share column 0.
        if getattr(mod_panel, "_filter_panel_open", False):
            mod_panel._close_filter_side_panel()
        if getattr(self, "_plugin_filter_panel_open", False):
            self._close_plugin_filter_panel()
        self._data_filter_panel_open = True
        mod_panel.grid_columnconfigure(0, minsize=scaled(380))
        self._data_filter_side_panel.grid()
        self._refresh_data_filter_filetype_list()
        self._update_data_filter_btn_color()

    def _close_data_filter_panel(self) -> None:
        mod_panel = getattr(self.winfo_toplevel(), "_mod_panel", None)
        self._data_filter_panel_open = False
        if mod_panel is not None:
            mod_panel.grid_columnconfigure(0, minsize=0)
        self._data_filter_side_panel.grid_remove()
        self._update_data_filter_btn_color()

    def _update_data_filter_btn_color(self) -> None:
        btn = getattr(self, "_data_filter_btn", None)
        if btn is None:
            return
        # Match Downloads tab: ACCENT (idle) → ACCENT_HOV (filters active).
        if self._data_filter_filetypes:
            btn.configure(fg_color=ACCENT_HOV, hover_color=ACCENT_HOV)
        else:
            btn.configure(fg_color=ACCENT, hover_color=ACCENT_HOV)

    def _clear_all_data_filters(self) -> None:
        for v in self._dfsp_filetype_vars.values():
            v.set(False)
        self._on_data_filter_panel_change()

    def _on_data_filter_panel_change(self) -> None:
        self._data_filter_filetypes = frozenset(
            ext for ext, v in self._dfsp_filetype_vars.items() if v.get()
        )
        self._update_data_filter_btn_color()
        # Re-run search/filter pipeline so the tree reflects the new filtertype set.
        self._apply_data_search()

    def _refresh_data_tab(self):
        """Reload the Data tab tree from filemap.txt.

        If the Data tab is not currently visible, just mark it dirty and defer
        the expensive tree rebuild until the user switches to it.
        """
        try:
            if self._tabs.get() != "Data":
                self._data_tab_dirty = True
                return
        except Exception:
            pass
        self._data_tab_dirty = False
        self._data_tree.delete(*self._data_tree.get_children())
        self._data_filemap_entries = []
        self._data_filemap_casefold = []
        self._data_search_prev_query = ""
        self._data_search_prev_indices = None
        filemap_path_str = self._get_filemap_path()
        if filemap_path_str is None:
            self._data_tree.insert("", "end",
                text="(no filemap.txt — load a game first)", values=("",))
            return
        filemap_path = Path(filemap_path_str)
        if not filemap_path.is_file():
            self._data_tree.insert("", "end",
                text="(filemap.txt not found)", values=("",))
            return
        raw_entries = self._parse_filemap(filemap_path)
        # Filter out mods that belong to a separator with a custom deploy location —
        # those files are deployed elsewhere and should not appear in the Data tab.
        custom_deploy_mods: set[str] = set()
        profile_dir = (
            getattr(self._game, "_active_profile_dir", None)
            or filemap_path.parent
        )
        modlist_path = profile_dir / "modlist.txt"
        if modlist_path.is_file():
            from Utils.modlist import read_modlist
            from Utils.deploy import load_separator_deploy_paths, expand_separator_deploy_paths
            _sep_paths = load_separator_deploy_paths(profile_dir)
            if _sep_paths:
                _entries = read_modlist(modlist_path)
                custom_deploy_mods = set(expand_separator_deploy_paths(_sep_paths, _entries).keys())
        if custom_deploy_mods:
            raw_entries = [(p, m) for p, m in raw_entries if m not in custom_deploy_mods]
        try:
            fm_mtime = filemap_path.stat().st_mtime
        except OSError:
            fm_mtime = 0.0
        cache_key = (str(filemap_path), fm_mtime, id(self._game), len(raw_entries))
        cached = self._data_resolved_cache
        if cached is not None and cached[0] == cache_key:
            self._data_filemap_entries = cached[1]
            self._data_filemap_casefold = cached[2]
        else:
            resolved = self._resolve_data_entries(raw_entries)
            casefold = [(rp.casefold(), mn.casefold()) for rp, mn in resolved]
            self._data_resolved_cache = (cache_key, resolved, casefold)
            self._data_filemap_entries = resolved
            self._data_filemap_casefold = casefold

        # Build contested_keys from the shared conflict cache.
        contested_keys, _ = self._get_conflict_cache(None)
        self._data_contested_keys = contested_keys
        self._build_data_tree_from_entries(self._data_filemap_entries, contested_keys)

    def _resolve_data_entries(self, entries):
        """Prefix each entry's path with its resolved deploy destination so the
        Data tab shows where files will actually land in the game.

        UE5 games use their own _match_rule/_apply_strip logic.
        Other games with custom_routing_rules use the same folder-match logic
        as deploy_custom_rules() (first matching rule wins, full path preserved
        under dest).
        """
        from Games.ue5_game import UE5Game
        game = self._game
        if isinstance(game, UE5Game):
            # Build priority map so flatten collisions show only the winner.
            priority_map: dict[str, int] = {}
            filemap_path_str = self._get_filemap_path()
            if filemap_path_str:
                profile_dir = (
                    getattr(game, "_active_profile_dir", None)
                    or Path(filemap_path_str).parent
                )
                modlist_path = profile_dir / "modlist.txt"
                if modlist_path.is_file():
                    try:
                        from Utils.modlist import read_modlist
                        for rank, e in enumerate(read_modlist(modlist_path)):
                            priority_map[e.name] = rank
                    except Exception:
                        pass
            # Use _resolve_filemap_entries so include_siblings drags same-mod
            # files under a matched container along to the rule's dest.
            winners: dict[str, tuple[int, str, str]] = {}
            for rel_path, mod_name, dest, final_rel in game._resolve_filemap_entries(
                list(entries)
            ):
                full_path = dest + "/" + final_rel if dest else final_rel
                rank = priority_map.get(mod_name, 1 << 30)
                existing = winners.get(full_path)
                if existing is None or rank < existing[0]:
                    winners[full_path] = (rank, full_path, mod_name)
            return [(p, m) for _r, p, m in winners.values()]

        rules = getattr(game, "custom_routing_rules", None)
        if not rules:
            return entries

        import fnmatch
        import os
        # Pre-process rules (mirrors deploy_custom_rules logic).
        # Extensions sorted longest-first so multi-dot extensions like
        # ".dekcns.json" win over their plain ".json" suffix.
        _rules = [
            (r,
             {f.lower() for f in r.folders},
             sorted({e.lower() for e in r.extensions}, key=len, reverse=True),
             {n.lower() for n in r.filenames})
            for r in rules
        ]

        def _ext_match(filename: str, exts: list[str]) -> str | None:
            for e in exts:
                if filename.endswith(e) and len(filename) > len(e):
                    return e
            return None

        def _name_match(filename: str, names: set[str]) -> bool:
            # Filenames may be glob patterns (``*``, ``?``, ``[seq]``); plain
            # entries match by exact equality. Mirrors deploy_custom_rules.
            for n in names:
                if any(c in n for c in "*?["):
                    if fnmatch.fnmatchcase(filename, n):
                        return True
                elif filename == n:
                    return True
            return False

        def _match_one(rel_lower, rule, folders, exts, filenames):
            """Match a single rule against rel_lower. Returns
            ``(strip_len, matched_ext)`` on a hit or None. Same semantics as
            ``deploy_custom_rules._match_single_rule``.
            """
            parts = rel_lower.split("/")
            filename = parts[-1]
            is_loose = len(parts) == 1
            strip_len = -1
            folder_hit = False
            if folders:
                for f in folders:
                    if "/" in f:
                        idx = rel_lower.find(f + "/")
                        if idx < 0 and rel_lower.endswith(f):
                            idx = len(rel_lower) - len(f)
                        if idx >= 0 and (idx == 0 or rel_lower[idx - 1] == "/"):
                            strip_len = idx
                            folder_hit = True
                            break
                    else:
                        for pi, seg in enumerate(parts[:-1]):
                            if seg == f:
                                strip_len = sum(len(parts[j]) + 1 for j in range(pi))
                                folder_hit = True
                                break
                        if folder_hit:
                            break
                if folder_hit and rule.loose_only and strip_len != 0:
                    return None
            matched_ext = _ext_match(filename, exts) if exts else None
            if folder_hit and (not exts or matched_ext is not None):
                return strip_len, matched_ext or ""
            if rule.loose_only and not is_loose:
                return None
            if matched_ext is not None and not folders and not filenames:
                return -1, matched_ext
            if filenames and _name_match(filename, filenames):
                return -1, ""
            return None

        # Build the per-file index used by the companion pass.
        primary_rules: dict[int, tuple] = {}
        entries_by_parent: dict[str, list[tuple[int, str]]] = {}
        normalised: list[str] = []
        for idx, (rel_path, _mod_name) in enumerate(entries):
            rel_norm = rel_path.replace("\\", "/")
            normalised.append(rel_norm)
            rel_lower = rel_norm.lower()
            parent_lower, _, _name_lower = rel_lower.rpartition("/")
            entries_by_parent.setdefault(parent_lower, []).append((idx, _name_lower))

        # Process rules in declaration order so an earlier include_siblings
        # rule claims its container before a later rule can match a file
        # inside it. Mirrors deploy_custom_rules' rule-ordered first pass.
        sibling_overrides: dict[int, str] = {}
        from Utils.deploy_custom_rules import _sibling_container
        claimed: set[int] = set()
        for rule, folders, exts, filenames in _rules:
            new_primary_idxs: list[int] = []
            for idx, (rel_path, mod_name) in enumerate(entries):
                if idx in claimed:
                    continue
                rel_lower = normalised[idx].lower()
                hit = _match_one(rel_lower, rule, folders, exts, filenames)
                if hit is None:
                    continue
                strip_len, matched_ext = hit
                primary_rules[idx] = (rule, strip_len, matched_ext)
                claimed.add(idx)
                new_primary_idxs.append(idx)
            if not getattr(rule, "include_siblings", False) or not new_primary_idxs:
                continue
            # Build drag specs for this rule's primaries, then claim siblings.
            drags: list[tuple[str, str, str, bool]] = []
            for pidx in new_primary_idxs:
                _r, sl, _me = primary_rules[pidx]
                rn = normalised[pidx]; pmod = entries[pidx][1]
                info = _sibling_container(rn, sl, pmod)
                if info is None:
                    continue
                cont, cname = info
                is_whole = cont == ""
                drags.append((cont.lower(), cname, pmod, is_whole))
                # Override the primary itself.
                tail = rn if is_whole else rn[len(cont) + 1:]
                sibling_overrides[pidx] = (cname + "/" + tail) if cname else tail
            drags.sort(key=lambda t: (0 if t[3] else 1, -len(t[0])))
            seen_drags: set[tuple[str, str]] = set()
            for cont_lower, cname, pmod, is_whole in drags:
                key = (cont_lower, pmod)
                if key in seen_drags:
                    continue
                seen_drags.add(key)
                prefix_lower = cont_lower + "/" if cont_lower else ""
                for sib_idx, (rel_path, sib_mod) in enumerate(entries):
                    if sib_idx in claimed:
                        continue
                    if sib_mod != pmod:
                        continue
                    sn = normalised[sib_idx]; slow = sn.lower()
                    if is_whole:
                        ric = sn
                    else:
                        if not slow.startswith(prefix_lower):
                            continue
                        ric = sn[len(cont_lower) + 1:]
                    sibling_overrides[sib_idx] = (cname + "/" + ric) if cname else ric
                    primary_rules[sib_idx] = (rule, -2, "")
                    claimed.add(sib_idx)

        # Second pass: mark companions (same folder, same stem, companion ext)
        # with their primary's rule.
        for idx, (rule, strip_len, matched_ext) in list(primary_rules.items()):
            companions = sorted(
                {c.lower() for c in getattr(rule, "companion_extensions", [])},
                key=len, reverse=True,
            )
            if not companions:
                continue
            rel_norm = normalised[idx]
            rel_lower = rel_norm.lower()
            parent_lower, _, name_lower = rel_lower.rpartition("/")
            if matched_ext and name_lower.endswith(matched_ext):
                stem_lower = name_lower[: -len(matched_ext)]
            else:
                stem_lower, _ = os.path.splitext(name_lower)
            stem_dot = stem_lower + "."
            for sib_idx, sib_name_lower in entries_by_parent.get(parent_lower, ()):
                if sib_idx == idx:
                    continue
                if sib_idx in primary_rules:
                    continue
                if not sib_name_lower.startswith(stem_dot):
                    continue
                for c in companions:
                    if sib_name_lower.endswith(c) and len(sib_name_lower) > len(c):
                        primary_rules[sib_idx] = (rule, strip_len, c)
                        break

        resolved = []
        for idx, (rel_path, mod_name) in enumerate(entries):
            rel_norm = normalised[idx]
            match = primary_rules.get(idx)
            if match is not None:
                rule, strip_len, _matched_ext = match
                dest = rule.dest
                override = sibling_overrides.get(idx)
                if override is not None:
                    # include_siblings: matched file or dragged sibling lands
                    # at dest/<container_name>/<rel-from-container>.
                    full_path = dest + "/" + override if dest else override
                elif rule.flatten:
                    if strip_len >= 0:
                        # Folder match + flatten=True: strip prefix above
                        # the folder, keep folder + contents under dest.
                        kept = rel_norm[strip_len:].lstrip("/")
                        full_path = dest + "/" + kept if dest else kept
                    else:
                        # Ext/filename-only + flatten=True: bare filename.
                        basename = rel_norm.split("/")[-1]
                        full_path = dest + "/" + basename if dest else basename
                else:
                    # flatten=False (any match type): preserve full
                    # mod-relative path under dest.
                    full_path = dest + "/" + rel_norm if dest else rel_norm
                # Strip the game's deploy subfolder prefix so the resolved
                # path is shown relative to that folder (matching how the
                # filemap entries themselves are stored).
                _mods_dir = getattr(game, "mods_dir", None)
                if _mods_dir:
                    _prefix = _mods_dir.rstrip("/") + "/"
                    if full_path.lower().startswith(_prefix.lower()):
                        full_path = full_path[len(_prefix):]
            else:
                full_path = rel_norm
            resolved.append((full_path, mod_name))
        return resolved

    @staticmethod
    def _parse_filemap(filemap_path: Path):
        """Parse filemap.txt and return a list of (rel_path, mod_name) tuples."""
        entries = []
        with filemap_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if "\t" not in line:
                    continue
                rel_path, mod_name = line.split("\t", 1)
                entries.append((rel_path, mod_name))
        return entries

    def _build_data_tree_from_entries(self, entries, contested_keys: "set[str] | None" = None):
        """Build the tree hierarchy from a list of (rel_path, mod_name) entries.

        Folder children are realised lazily on first <<TreeviewOpen>> — only
        top-level folders + a placeholder per non-empty folder are inserted up
        front. See _realise_data_node / _on_data_tree_open.
        """
        self._data_tree_expanded = False
        self._data_expand_btn.configure(text="⊞ Expand All")
        self._data_tree.delete(*self._data_tree.get_children())
        self._data_lazy_subtrees.clear()
        self._data_realised_nodes.clear()
        contested_keys = contested_keys or set()

        only_conflicts = bool(
            self._data_only_conflicts_var and self._data_only_conflicts_var.get()
        )
        filetype_filter = getattr(self, "_data_filter_filetypes", frozenset())

        tree_dict: dict = {}
        for rel_path, mod_name in entries:
            rel_norm = rel_path.replace("\\", "/")
            rel_key_lower = rel_norm.lower()
            if only_conflicts and rel_key_lower not in contested_keys:
                continue
            if filetype_filter:
                _dot = rel_key_lower.rfind(".")
                _slash = rel_key_lower.rfind("/")
                if _dot <= _slash:
                    continue
                if rel_key_lower[_dot:] not in filetype_filter:
                    continue
            parts = rel_norm.split("/")
            node = tree_dict
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node.setdefault("__files__", []).append((parts[-1], mod_name, rel_key_lower))

        self._data_tree.tag_configure("folder",        foreground=TAG_FOLDER)
        self._data_tree.tag_configure("file",          foreground=TEXT_MAIN)
        self._data_tree.tag_configure("conflict_win",  foreground=_theme.conflict_higher)
        self._data_tree.tag_configure(
            "mod_highlight", background=_theme.plugin_mod, foreground=TEXT_MAIN,
        )

        # Stash the build-time tree + filter context for lazy realisation.
        self._data_tree_dict = tree_dict
        self._data_contested_keys_active = contested_keys

        # Insert top-level folders (with placeholder child) and top-level files.
        tree = self._data_tree
        folder_keys = sorted(k for k in tree_dict if k != "__files__")
        for top in folder_keys:
            subtree = tree_dict[top]
            node_id = tree.insert(
                "", "end",
                text=f"  {top}", values=("",),
                open=False, tags=("folder",),
            )
            if any(k != "__files__" for k in subtree) or subtree.get("__files__"):
                # Placeholder makes the disclosure arrow appear without
                # forcing us to insert real children yet.
                tree.insert(node_id, "end", text="", values=("",), tags=("folder",))
                self._data_lazy_subtrees[node_id] = subtree

        hi_mod = self._highlighted_data_mod
        for fname, mod, rel_key_lower in sorted(tree_dict.get("__files__", [])):
            base = "conflict_win" if rel_key_lower in contested_keys else "file"
            tags = (base, "mod_highlight") if (hi_mod and mod == hi_mod) else (base,)
            tree.insert("", "end", text=fname, values=(mod,), tags=tags)

        self._draw_data_marker_strip()

    def _realise_data_node(self, node_id: str) -> bool:
        """Insert real children into a lazily-populated folder node.

        Returns True if anything was inserted, False if the node was already
        realised or has no pending subtree.
        """
        subtree = self._data_lazy_subtrees.pop(node_id, None)
        if subtree is None:
            return False
        self._data_realised_nodes.add(node_id)
        tree = self._data_tree
        # Drop the placeholder before inserting real rows.
        existing = tree.get_children(node_id)
        if existing:
            tree.delete(*existing)

        contested_keys = self._data_contested_keys_active or set()
        hi_mod = self._highlighted_data_mod

        for child in sorted(k for k in subtree if k != "__files__"):
            child_subtree = subtree[child]
            child_id = tree.insert(
                node_id, "end",
                text=f"  {child}", values=("",),
                open=False, tags=("folder",),
            )
            if any(k != "__files__" for k in child_subtree) or child_subtree.get("__files__"):
                tree.insert(child_id, "end", text="", values=("",), tags=("folder",))
                self._data_lazy_subtrees[child_id] = child_subtree

        for fname, mod, rel_key_lower in sorted(subtree.get("__files__", [])):
            base = "conflict_win" if rel_key_lower in contested_keys else "file"
            tags = (base, "mod_highlight") if (hi_mod and mod == hi_mod) else (base,)
            tree.insert(node_id, "end", text=fname, values=(mod,), tags=tags)
        return True

    def _realise_data_subtree(self, node_id: str) -> None:
        """Realise a folder and every lazy descendant (depth-first)."""
        self._realise_data_node(node_id)
        for child in self._data_tree.get_children(node_id):
            self._realise_data_subtree(child)

    def _on_data_tree_open(self, _event=None):
        focus = self._data_tree.focus()
        if focus:
            self._realise_data_node(focus)
        self._draw_data_marker_strip()

    def _toggle_data_tree_expand(self):
        """Expand all folders in the Data tree, or collapse them if already expanded."""
        self._data_tree_expanded = not self._data_tree_expanded
        open_state = self._data_tree_expanded
        tv = self._data_tree.treeview

        if open_state:
            # Realise every lazy subtree so opens have something to show.
            for top in tv.get_children(""):
                self._realise_data_subtree(top)

        def _set_all(item):
            children = tv.get_children(item)
            if children:
                tv.item(item, open=open_state)
                for child in children:
                    _set_all(child)

        for top in tv.get_children(""):
            _set_all(top)

        self._data_expand_btn.configure(
            text="⊟ Collapse All" if self._data_tree_expanded else "⊞ Expand All"
        )
        self._draw_data_marker_strip()

    # ------------------------------------------------------------------
    # Data tab marker strip + row highlight
    # ------------------------------------------------------------------

    def _data_visible_rows(self) -> list[tuple[str, str]]:
        """Return (iid, mod_name) for every currently visible row in the Data tree.

        A row is visible iff every ancestor is open. Folder rows return ("", ""),
        which lets the caller skip them when painting marker ticks.
        """
        out: list[tuple[str, str]] = []
        tv = self._data_tree

        def walk(parent: str):
            for iid in tv.get_children(parent):
                vals = tv.item(iid, "values")
                mod = vals[0] if vals else ""
                out.append((iid, mod))
                if tv.item(iid, "open"):
                    walk(iid)

        walk("")
        return out

    def _apply_data_row_highlight(self):
        """Update row backgrounds (orange) for files belonging to the highlighted mod."""
        tv = self._data_tree
        hi = self._highlighted_data_mod

        def walk(parent: str):
            for iid in tv.get_children(parent):
                vals = tv.item(iid, "values")
                mod = vals[0] if vals else ""
                if mod:  # file row
                    cur = list(tv.item(iid, "tags") or ())
                    has = "mod_highlight" in cur
                    want = bool(hi and mod == hi)
                    if want and not has:
                        cur.append("mod_highlight")
                        tv.item(iid, tags=tuple(cur))
                    elif not want and has:
                        cur.remove("mod_highlight")
                        tv.item(iid, tags=tuple(cur))
                walk(iid)

        walk("")

    def _on_data_marker_strip_resize(self, _event=None):
        if self._data_marker_strip_after_id is not None:
            try:
                self.after_cancel(self._data_marker_strip_after_id)
            except Exception:
                pass
        self._data_marker_strip_after_id = self.after(50, self._draw_data_marker_strip)

    def _draw_data_marker_strip(self):
        """Paint the combined scrollbar + marker strip for the Data tab.

        Layers:
          1. Trough background
          2. Orange tick marks for files belonging to the highlighted mod
             (using each row's index in the *currently visible* row list)
          3. Thumb rectangle
        """
        self._data_marker_strip_after_id = None
        c = self._data_marker_strip
        c.delete("all")
        strip_h = c.winfo_height()
        strip_w = c.winfo_width()
        if strip_h <= 1 or strip_w <= 1:
            return

        c.create_rectangle(0, 0, strip_w, strip_h, fill=BG_DEEP, outline="", tags="trough")

        hi = self._highlighted_data_mod
        if hi:
            rows = self._data_visible_rows()
            n = len(rows)
            if n:
                strip_max = strip_h - 4
                inv_n = 1.0 / n
                color = _theme.plugin_mod
                for row_idx, (_iid, mod) in enumerate(rows):
                    if mod != hi:
                        continue
                    y = int(row_idx * inv_n * strip_h)
                    if y < 2:
                        y = 2
                    elif y > strip_max:
                        y = strip_max
                    c.create_rectangle(0, y, strip_w, y + 3, fill=color, outline="", tags="marker")

        self._redraw_data_thumb()

    def _redraw_data_thumb(self) -> None:
        c = self._data_marker_strip
        c.delete("thumb")
        strip_h = c.winfo_height()
        strip_w = c.winfo_width()
        if strip_h <= 1 or strip_w <= 1:
            return
        first = max(0.0, min(1.0, self._data_scroll_first))
        last = max(first, min(1.0, self._data_scroll_last))
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

    def _data_scroll_set(self, first: str, last: str) -> None:
        try:
            f = float(first); l = float(last)
        except (TypeError, ValueError):
            return
        if f == self._data_scroll_first and l == self._data_scroll_last:
            return
        self._data_scroll_first = f
        self._data_scroll_last = l
        self._redraw_data_thumb()

    def _on_data_scrollbar_press(self, event):
        strip_h = self._data_marker_strip.winfo_height()
        if strip_h <= 1:
            return
        first = self._data_scroll_first
        last = self._data_scroll_last
        thumb_top = first * strip_h
        thumb_bot = last * strip_h
        if thumb_top <= event.y <= thumb_bot:
            self._data_thumb_drag_offset = (event.y - thumb_top) / strip_h
        else:
            self._data_thumb_drag_offset = (last - first) / 2.0
            self._data_scroll_to_pointer(event.y)

    def _on_data_scrollbar_drag(self, event):
        if self._data_thumb_drag_offset is None:
            return
        self._data_scroll_to_pointer(event.y)

    def _on_data_scrollbar_release(self, _event):
        self._data_thumb_drag_offset = None

    def _data_scroll_to_pointer(self, py: int) -> None:
        strip_h = self._data_marker_strip.winfo_height()
        if strip_h <= 1 or self._data_thumb_drag_offset is None:
            return
        frac = (py / strip_h) - self._data_thumb_drag_offset
        frac = max(0.0, min(1.0, frac))
        self._data_tree.yview_moveto(frac)

    def _on_data_mousewheel(self, event):
        delta = event.delta
        if delta == 0:
            return
        step = -3 if delta > 0 else 3
        self._data_tree.yview_scroll(step, "units")

    def _on_data_right_click(self, event):
        """Show context menu for Data tab tree rows."""
        tv = self._data_tree.treeview
        iid = tv.identify_row(event.y)
        if not iid:
            return
        staging = self._get_staging_path()

        values = tv.item(iid, "values")
        mod_name = values[0] if values else ""
        is_file = bool(mod_name)

        if is_file and staging:
            # Reconstruct the relative path by walking up the tree hierarchy
            parts = [tv.item(iid, "text").strip()]
            cur = tv.parent(iid)
            while cur:
                parts.append(tv.item(cur, "text").strip())
                cur = tv.parent(cur)
            parts.reverse()
            rel_path = "/".join(parts)
            open_path = Path(staging) / mod_name / rel_path
            open_path = open_path.parent
        elif not is_file:
            # Folder row — walk up for full folder path; no mod_name known
            parts = [tv.item(iid, "text").strip()]
            cur = tv.parent(iid)
            while cur:
                parts.append(tv.item(cur, "text").strip())
                cur = tv.parent(cur)
            parts.reverse()
            # Can't resolve to a specific mod folder without mod_name; open staging root
            open_path = staging if staging else None
        else:
            open_path = None

        if open_path is None:
            return

        self._show_simple_context_menu(tv, event.x_root, event.y_root, [
            ("Open in File Browser", lambda p=open_path: self._open_folder_in_browser(p)),
        ])

    def _on_data_file_selected(self, _event=None):
        """When a file row is selected in the Data tab, highlight its mod in the modlist."""
        sel = self._data_tree.treeview.selection()
        if not sel:
            return
        item = sel[0]
        values = self._data_tree.treeview.item(item, "values")
        mod_name = values[0] if values else ""
        if not mod_name:
            # Folder row — clear highlight
            if self._on_plugin_selected_cb is not None:
                self._on_plugin_selected_cb(None)
            return
        if self._on_mod_selected_cb is not None:
            self._on_mod_selected_cb()
        if self._on_plugin_selected_cb is not None:
            self._on_plugin_selected_cb(mod_name)

    def _on_data_search_changed(self, *_):
        """Debounced filter of the Data tree based on the search query."""
        if self._data_search_after_id is not None:
            try:
                self.after_cancel(self._data_search_after_id)
            except Exception:
                pass
        self._data_search_after_id = self.after(150, self._apply_data_search)

    def _apply_data_search(self):
        """Apply the current search query to the Data tree."""
        self._data_search_after_id = None
        query = self._data_search_var.get().casefold()
        if not self._data_filemap_entries:
            return
        _ck = getattr(self, "_data_contested_keys", None)
        if not query:
            self._data_search_prev_query = ""
            self._data_search_prev_indices = None
            self._build_data_tree_from_entries(self._data_filemap_entries, _ck)
            return

        cf = self._data_filemap_casefold
        entries = self._data_filemap_entries
        prev_q = self._data_search_prev_query
        prev_idx = self._data_search_prev_indices
        if prev_q and prev_idx is not None and query.startswith(prev_q):
            source = prev_idx
        else:
            source = range(len(entries))

        matched: list[int] = [
            i for i in source
            if query in cf[i][0] or query in cf[i][1]
        ]
        self._data_search_prev_query = query
        self._data_search_prev_indices = matched

        filtered = [entries[i] for i in matched]
        self._build_data_tree_from_entries(filtered, _ck)
        # Expand all nodes so filtered results are visible
        for item in self._data_tree.get_children():
            self._expand_all(item)

    def _expand_all(self, item):
        """Recursively expand a treeview item and all its children."""
        self._realise_data_node(item)
        self._data_tree.item(item, open=True)
        for child in self._data_tree.get_children(item):
            self._expand_all(child)

"""
Nexus browser overlay — Browse, Tracked, Endorsed, Trending mods from Nexus Mods.

Embeds inside the ModListPanel area as an overlay (like Collections).
Shows when the Nexus toolbar button is pressed.
"""

from __future__ import annotations

import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk
from Utils.xdg import open_url
from Utils.config_paths import get_download_cache_dir_for_game

from gui.tracked_mods_panel import TrackedModsPanel
from gui.endorsed_mods_panel import EndorsedModsPanel
from gui.browse_mods_panel import BrowseModsPanel
from gui.trending_mods_panel import TrendingModsPanel
from gui.install_mod import install_mod_from_archive
from Nexus.nexus_meta import build_meta_from_download, scan_installed_mods
from Nexus.nexus_download import delete_archive_and_sidecar

from gui.theme import (
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    BG_ROW,
    BG_ROW_ALT,
    BG_HOVER_ROW,
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    BORDER,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_BOLD,
    FONT_NORMAL,
    load_icon,
    scaled,
    TK_FONT_BOLD,
    TK_FONT_SMALL,
)


def _fmt_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if size_bytes <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if isinstance(size_bytes, float) else f"{size_bytes} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


class _FileChooserOverlay(tk.Frame):
    """Inline overlay that lets the user pick which file to install
    when a Nexus mod has multiple files in the MAIN category.
    Placed over the parent widget instead of opening a separate window,
    which works better on Steam Deck gaming mode."""

    _MIN_WIDTH = 550
    _MAX_WIDTH = 1100

    def __init__(self, parent, mod_name: str, files: list, on_pick=None):
        """``on_pick(file_or_none)`` is called when the user picks or cancels."""
        super().__init__(parent, bg=BG_DEEP)
        self._on_pick = on_pick
        self.result = None

        # All raw-tk geometry below must be scale-aware — these widgets are
        # plain tk (not CTk) so nothing multiplies by ui_scale for us.
        min_width = scaled(self._MIN_WIDTH)
        max_width = scaled(self._MAX_WIDTH)
        margin = scaled(40)

        # Semi-transparent backdrop — absorbs clicks outside the card
        self._backdrop = tk.Frame(parent, bg="#000000")
        self._backdrop.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._backdrop.bind("<Button-1>", lambda e: self._dismiss(None))

        pad = scaled(14)

        # Auto-size the card to fit the widest row, clamped to [_MIN_WIDTH, _MAX_WIDTH]
        # and the parent's available width.
        try:
            bold_font = tkfont.Font(font=TK_FONT_BOLD)
            small_font = tkfont.Font(font=TK_FONT_SMALL)
            header_w = bold_font.measure(f"'{mod_name}' has multiple main files.")
            row_w = 0
            for f in files:
                name_text = f.name or f.file_name or ""
                size_bytes = f.size_in_bytes or (f.size_kb * 1024 if f.size_kb else 0)
                parts = []
                if f.version:
                    parts.append(f"v{f.version}")
                if size_bytes > 0:
                    parts.append(_fmt_size(size_bytes))
                detail = "  —  ".join(parts)
                w = bold_font.measure(name_text) + (small_font.measure(detail) if detail else 0) + scaled(40)
                row_w = max(row_w, w)
            content_w = max(header_w, row_w) + pad * 2 + scaled(4)
            try:
                parent_w = parent.winfo_width() or max_width
            except Exception:
                parent_w = max_width
            card_w = max(min_width, min(content_w, max_width, parent_w - margin))
        except Exception:
            card_w = min_width

        # Card — use grid so header/list/footer can each take their natural
        # share, with the list section the only one that's allowed to flex
        # and scroll when the file count is large.
        card = tk.Frame(self._backdrop, bg=BG_DEEP, bd=1, relief="solid",
                        highlightbackground=BORDER, highlightthickness=1)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)  # list section flexes

        # Header (fixed)
        header = tk.Frame(card, bg=BG_DEEP)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            header, text=f"'{mod_name}' has multiple main files.",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_DEEP, anchor="w",
        ).pack(fill="x", padx=pad, pady=(pad, 2))
        tk.Label(
            header, text="Select which file to install:",
            font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP, anchor="w",
        ).pack(fill="x", padx=pad, pady=(0, 8))

        # Scrollable file list
        list_outer = tk.Frame(card, bg=BG_DEEP)
        list_outer.grid(row=1, column=0, sticky="nsew", padx=pad, pady=(0, 8))
        list_outer.grid_columnconfigure(0, weight=1)
        list_outer.grid_rowconfigure(0, weight=1)

        canvas = tk.Canvas(list_outer, bg=BORDER, bd=0, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        vbar = ctk.CTkScrollbar(list_outer, orientation="vertical", command=canvas.yview,
                                width=14)
        vbar.grid(row=0, column=1, sticky="ns", padx=(2, 0))
        canvas.configure(yscrollcommand=vbar.set)

        list_frame = tk.Frame(canvas, bg=BORDER)
        list_window = canvas.create_window((0, 0), window=list_frame, anchor="nw")

        def _on_list_configure(_evt=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        list_frame.bind("<Configure>", _on_list_configure)

        def _on_canvas_configure(evt):
            canvas.itemconfigure(list_window, width=evt.width)
        canvas.bind("<Configure>", _on_canvas_configure)

        def _on_mousewheel(evt):
            delta = -1 if getattr(evt, "delta", 0) > 0 or getattr(evt, "num", 0) == 4 else 1
            canvas.yview_scroll(delta * 3, "units")
            return "break"

        files = sorted(files, key=lambda f: -(f.uploaded_timestamp or 0))

        row_widgets = []
        for idx, f in enumerate(files):
            bg = BG_ROW if idx % 2 == 0 else BG_ROW_ALT
            row = tk.Frame(list_frame, bg=bg, cursor="hand2")
            row.pack(fill="x", ipady=scaled(6))
            row_widgets.append(row)

            name_text = f.name or f.file_name
            tk.Label(
                row, text=name_text,
                font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=bg, anchor="w",
            ).pack(side="left", padx=(scaled(12), scaled(6)), pady=(scaled(4), 0), anchor="nw")

            size_bytes = f.size_in_bytes or (f.size_kb * 1024 if f.size_kb else 0)
            detail_parts = []
            if f.version:
                detail_parts.append(f"v{f.version}")
            if size_bytes > 0:
                detail_parts.append(_fmt_size(size_bytes))
            if detail_parts:
                tk.Label(
                    row, text="  —  ".join(detail_parts),
                    font=TK_FONT_SMALL, fg=TEXT_DIM, bg=bg, anchor="e",
                ).pack(side="right", padx=(scaled(6), scaled(12)), pady=(scaled(4), 0))

            def _enter(e, r=row):
                for w in (r, *r.winfo_children()):
                    w.configure(bg=BG_HOVER_ROW)

            def _leave(e, r=row, b=bg):
                for w in (r, *r.winfo_children()):
                    w.configure(bg=b)

            def _click(e, fi=f):
                self._dismiss(fi)

            for widget in (row, *row.winfo_children()):
                widget.bind("<Enter>", _enter)
                widget.bind("<Leave>", _leave)
                widget.bind("<Button-1>", _click)
                widget.bind("<MouseWheel>", _on_mousewheel)
                widget.bind("<Button-4>", _on_mousewheel)
                widget.bind("<Button-5>", _on_mousewheel)

        for w in (canvas, list_frame):
            w.bind("<MouseWheel>", _on_mousewheel)
            w.bind("<Button-4>", _on_mousewheel)
            w.bind("<Button-5>", _on_mousewheel)

        # Cancel button (fixed footer)
        footer = tk.Frame(card, bg=BG_DEEP)
        footer.grid(row=2, column=0, sticky="ew")
        ctk.CTkButton(
            footer, text="Cancel", width=100, height=30,
            fg_color="#555", hover_color="#666",
            text_color="white", font=FONT_BOLD,
            command=lambda: self._dismiss(None),
        ).pack(pady=(0, pad))

        # Place + size the card. Fill the full available height of the parent
        # (the modlist panel) so the card is as tall as it can be; the list
        # canvas absorbs the extra space below the rows. Header and footer stay
        # fixed via the grid weights, so they always remain visible.
        try:
            parent.update_idletasks()
            parent_h = parent.winfo_height() or scaled(600)
        except Exception:
            parent_h = scaled(600)
        card_h = max(scaled(240), parent_h - margin)
        card.place(relx=0.5, rely=0.5, anchor="center",
                   width=card_w, height=card_h)

    def _dismiss(self, chosen):
        self.result = chosen
        try:
            self._backdrop.destroy()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        if self._on_pick:
            self._on_pick(chosen)


def install_nexus_mod_from_entry(app, api, game, mod_panel, log_fn, entry,
                                 label: str = "Missing Requirements",
                                 on_complete: Optional[Callable] = None):
    """Download and install a mod from Nexus by entry (mod_id, domain_name, name).
    Used by missing-requirements panel and Nexus browser overlay.
    """
    domain = getattr(entry, "domain_name", "") or ""
    mod_id = getattr(entry, "mod_id", 0)
    mod_name = getattr(entry, "name", "") or f"Mod {mod_id}"

    if api is None:
        log_fn(f"{label}: Login to Nexus first")
        return
    if game is None or not game.is_configured():
        log_fn(f"{label}: No configured game selected.")
        return

    cancel_ev = mod_panel.get_download_cancel_event() if mod_panel else None
    if mod_panel:
        mod_panel.show_download_progress(f"Installing: {mod_name}", cancel=cancel_ev)

    def _worker():
        downloader = getattr(app, "_nexus_downloader", None)
        if downloader is None:
            app.after(0, lambda: (
                mod_panel.hide_download_progress(cancel=cancel_ev) if mod_panel else None,
                log_fn(f"{label}: Downloader not initialised."),
            ))
            return

        is_premium = False
        try:
            user = api.validate()
            is_premium = user.is_premium
        except Exception:
            pass

        if not is_premium:
            files_url = f"https://www.nexusmods.com/{domain}/mods/{mod_id}?tab=files"
            def _fallback():
                if mod_panel:
                    mod_panel.hide_download_progress(cancel=cancel_ev)
                log_fn(f"{label}: Premium required for direct download.")
                log_fn(f'{label}: Opening files page — click "Download with Mod Manager" there.')
                log_fn(f"{label}: {files_url}")
                try:
                    open_url(files_url)
                except Exception as exc:
                    log_fn(f"{label}: Could not open browser — {exc}")
            app.after(0, _fallback)
            return

        file_info = None
        user_picked = False
        try:
            files_resp = api.get_mod_files(domain, mod_id)
            main_files = [f for f in files_resp.files if f.category_name == "MAIN"]
            if len(main_files) > 1:
                # Multiple main files — ask the user which one to install.
                # The dialog runs on the main thread; block this worker until
                # the user picks one (or cancels).
                chosen = [None]
                pick_event = threading.Event()

                def _show_chooser():
                    if mod_panel:
                        mod_panel.suspend_download_progress(cancel=cancel_ev)

                    def _on_pick(result):
                        chosen[0] = result
                        pick_event.set()

                    _FileChooserOverlay(mod_panel or app, mod_name,
                                        main_files, on_pick=_on_pick)

                app.after(0, _show_chooser)
                pick_event.wait()
                file_info = chosen[0]
                user_picked = True
            elif main_files:
                file_info = main_files[0]
            elif files_resp.files:
                file_info = max(files_resp.files, key=lambda f: f.uploaded_timestamp)
        except Exception as exc:
            app.after(0, lambda e=exc: (
                mod_panel.hide_download_progress(cancel=cancel_ev) if mod_panel else None,
                log_fn(f"{label}: Could not fetch file list — {e}"),
            ))
            return

        if file_info is None:
            def _no_file():
                if mod_panel:
                    mod_panel.hide_download_progress(cancel=cancel_ev)
                if not user_picked:
                    log_fn(f"{label}: No files found for '{mod_name}'.")
            app.after(0, _no_file)
            return

        # Re-show download progress after the chooser dialog was dismissed
        if user_picked and mod_panel:
            app.after(0, lambda: (
                mod_panel.resume_download_progress(cancel=cancel_ev),
                mod_panel.show_download_progress(
                    f"Installing: {mod_name}", cancel=cancel_ev),
            ))

        mod_info_for_meta = None
        try:
            mod_info_for_meta, _ = api.get_mod_and_file_info_graphql(
                domain, mod_id, file_info.file_id
            )
        except Exception:
            pass

        result = downloader.download_file(
            game_domain=domain,
            mod_id=mod_id,
            file_id=file_info.file_id,
            progress_cb=lambda cur, total: app.after(
                0, lambda c=cur, t=total: (
                    mod_panel.update_download_progress(c, t, cancel=cancel_ev)
                    if mod_panel else None
                )
            ),
            cancel=cancel_ev,
            known_file_name=file_info.file_name,
            dest_dir=get_download_cache_dir_for_game(getattr(game, "name", "") or ""),
        )

        if result.success and result.file_path:
            _archive_path = result.file_path

            def _install():
                try:
                    if app.grab_current() is not None:
                        app.after(500, _install)
                        return
                except Exception:
                    pass
                if mod_panel:
                    mod_panel.hide_download_progress(cancel=cancel_ev)
                log_fn(f"{label}: Installing '{mod_name}'...")
                try:
                    _mod_info = mod_info_for_meta if mod_info_for_meta is not None else entry
                    _prebuilt = build_meta_from_download(
                        game_domain=domain,
                        mod_id=mod_id,
                        file_id=file_info.file_id,
                        archive_name=result.file_name,
                        mod_info=_mod_info,
                        file_info=file_info,
                    )
                except Exception:
                    _prebuilt = None

                status_bar = getattr(app, "_status", None)

                def _extract_progress(done: int, total: int, phase: str | None = None):
                    if status_bar is not None:
                        app.after(0, lambda d=done, t=total, p=phase: status_bar.set_progress(d, t, p, title="Extracting"))

                def _cleanup(is_fomod: bool = False):
                    if on_complete is not None:
                        app.after(0, on_complete)
                    from Utils.ui_config import (
                        load_clear_archive_after_install,
                        load_keep_fomod_archives,
                    )
                    if not load_clear_archive_after_install():
                        return
                    if is_fomod and load_keep_fomod_archives():
                        return
                    delete_archive_and_sidecar(Path(_archive_path))

                def _install_worker():
                    try:
                        install_mod_from_archive(
                            str(_archive_path), app, log_fn, game, mod_panel,
                            prebuilt_meta=_prebuilt,
                            on_installed=_cleanup,
                            progress_fn=_extract_progress,
                            clear_progress_fn=lambda: app.after(0, status_bar.clear_progress) if status_bar is not None else None)
                    finally:
                        if status_bar is not None:
                            app.after(0, status_bar.clear_progress)

                threading.Thread(target=_install_worker, daemon=True).start()
            app.after(0, _install)
        else:
            app.after(0, lambda: (
                mod_panel.hide_download_progress(cancel=cancel_ev) if mod_panel else None,
                log_fn(f"{label}: Download failed — {result.error}"),
            ))

    log_fn(f"{label}: Installing '{mod_name}'...")
    threading.Thread(target=_worker, daemon=True).start()


class NexusBrowserOverlay(tk.Frame):
    """
    Browse / Tracked / Endorsed mods panel — embeds inside the ModListPanel area.
    """

    def __init__(
        self,
        parent: tk.Widget,
        game_domain: str,
        api,
        game=None,
        log_fn: Optional[Callable] = None,
        app_root: Optional[tk.Widget] = None,
        on_close: Optional[Callable] = None,
        on_open_settings: Optional[Callable] = None,
        on_rehost: Optional[Callable] = None,
        is_popped_out: bool = False,
        initial_tab: str = "Browse",
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._game_domain = game_domain
        self._api = api
        self._game = game
        self._app_root = app_root or parent.winfo_toplevel()
        self._log = log_fn or (lambda msg: None)
        self._on_close = on_close
        self._on_open_settings = on_open_settings
        # Called when the user clicks the popout/dock toggle. The launcher
        # rebuilds the overlay in the other host (overlay vs. separate window),
        # leaving the modlist visible underneath when popped out.
        self._on_rehost = on_rehost
        self._is_popped_out = is_popped_out
        self._initial_tab = initial_tab if initial_tab in (
            "Browse", "Tracked", "Endorsed", "Trending") else "Browse"

        self._build()

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(self, bg=BG_HEADER, height=scaled(42))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        def _btn(name: str, cmd, w=85):
            return ctk.CTkButton(
                toolbar, text=name, width=w, height=30,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
                font=FONT_BOLD, command=cmd,
            )

        self._browse_btn = _btn("Browse", lambda: self._show_panel("Browse"), 78)
        self._browse_btn.pack(side="left", padx=3, pady=5)
        self._tracked_btn = _btn("Tracked", lambda: self._show_panel("Tracked"), 80)
        self._tracked_btn.pack(side="left", padx=3, pady=5)
        self._endorsed_btn = _btn("Endorsed", lambda: self._show_panel("Endorsed"), 90)
        self._endorsed_btn.pack(side="left", padx=3, pady=5)
        self._trending_btn = _btn("Trending", lambda: self._show_panel("Trending"), 85)
        self._trending_btn.pack(side="left", padx=3, pady=5)

        # Right-side buttons are packed first so they're never the ones clipped
        # when the toolbar is too narrow (pack gives earlier widgets priority).
        ctk.CTkButton(
            toolbar, text="✕ Close", width=80, height=36,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_close,
        ).pack(side="right", padx=(6, 12), pady=5)

        # Popout / dock toggle. Hidden when no re-host handler is wired up.
        if self._on_rehost is not None:
            self._popout_btn = ctk.CTkButton(
                toolbar, text=("⤡" if self._is_popped_out else "⤢"),
                width=36, height=36, font=FONT_BOLD,
                fg_color=BG_HEADER, hover_color=BG_HOVER_ROW,
                text_color=TEXT_MAIN, corner_radius=4,
                command=self._on_popout_toggle,
            )
            self._popout_btn.pack(side="right", padx=(0, 4), pady=5)

        if self._on_open_settings:
            _settings_icon = load_icon("settings.png", size=(16, 16))
            ctk.CTkButton(
                toolbar, text="Settings" if _settings_icon else "⚙ Settings",
                image=_settings_icon, compound="left",
                width=92, height=30,
                fg_color="#da8e35", hover_color="#e5a04a", text_color="white",
                font=FONT_BOLD, command=self._on_open_settings,
            ).pack(side="right", padx=(0, 4), pady=5)

        ctk.CTkButton(
            toolbar, text="🌐 Open on Nexus", width=120, height=30,
            fg_color="#d98f40", hover_color="#e5a04d", text_color="white",
            font=FONT_BOLD, command=self._on_open_nexus,
        ).pack(side="right", padx=(0, 6), pady=5)

        if self._on_rehost is not None:
            try:
                from gui.ctk_tooltip import CTkToolTip
                CTkToolTip(
                    self._popout_btn,
                    message="Dock back to main window" if self._is_popped_out
                    else "Open in a separate window (keep browsing while sorting)",
                )
            except Exception:
                pass

        # Content area — stacked panel frames
        content = tk.Frame(self, bg=BG_DEEP)
        content.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        def _get_api():
            return self._api

        def _get_game_domain():
            return self._game_domain

        def _get_installed_mod_ids():
            """Return set of Nexus mod IDs already installed for the current game."""
            game = self._game
            if game is None or not game.is_configured():
                return set()
            try:
                staging = game.get_effective_mod_staging_path()
                if not staging or not Path(staging).is_dir():
                    return set()
                installed = scan_installed_mods(Path(staging))
                domain = (self._game_domain or "").lower()
                return {
                    m.mod_id for m in installed
                    if m.mod_id > 0 and (not domain or (m.game_domain or "").lower() == domain)
                }
            except Exception:
                return set()

        self._panel_frames = {}
        for name in ("Browse", "Tracked", "Endorsed", "Trending"):
            frame = tk.Frame(content, bg=BG_DEEP)
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_rowconfigure(1, weight=1)
            frame.grid_columnconfigure(0, weight=1)
            self._panel_frames[name] = frame

        # Lazy construction: each tab panel is a full canvas/card grid (~3k
        # widgets). Building all four up front spikes RSS ~700MB on open. Instead
        # build only the tab the user actually views, on first _show_panel. The
        # factories capture the per-panel install callback + shared getters.
        self._panels: dict[str, object] = {}
        self._panel_factories = {
            "Browse": lambda f: BrowseModsPanel(
                f, log_fn=self._log, get_api=_get_api,
                get_game_domain=_get_game_domain,
                install_fn=self._install_from_browse,
                get_installed_mod_ids=_get_installed_mod_ids,
            ),
            "Tracked": lambda f: TrackedModsPanel(
                f, log_fn=self._log, get_api=_get_api,
                get_game_domain=_get_game_domain,
                install_fn=self._install_from_tracked,
                get_installed_mod_ids=_get_installed_mod_ids,
            ),
            "Endorsed": lambda f: EndorsedModsPanel(
                f, log_fn=self._log, get_api=_get_api,
                get_game_domain=_get_game_domain,
                install_fn=self._install_from_endorsed,
                get_installed_mod_ids=_get_installed_mod_ids,
            ),
            "Trending": lambda f: TrendingModsPanel(
                f, log_fn=self._log, get_api=_get_api,
                get_game_domain=_get_game_domain,
                install_fn=self._install_from_trending,
                get_installed_mod_ids=_get_installed_mod_ids,
            ),
        }

        # If not logged in, show a centered login prompt over the content area
        if self._api is None and self._on_open_settings:
            login_frame = tk.Frame(content, bg=BG_DEEP)
            login_frame.grid(row=0, column=0, sticky="nsew")
            login_frame.grid_rowconfigure(0, weight=1)
            login_frame.grid_columnconfigure(0, weight=1)

            inner = ctk.CTkFrame(login_frame, fg_color=BG_PANEL, corner_radius=10)
            inner.grid(row=0, column=0)

            ctk.CTkLabel(
                inner, text="You are not logged in to Nexus Mods.",
                font=FONT_BOLD, text_color=TEXT_MAIN,
            ).pack(padx=32, pady=(24, 6))
            ctk.CTkLabel(
                inner, text="Log in to browse, track, and download mods.",
                font=FONT_NORMAL, text_color=TEXT_DIM,
            ).pack(padx=32, pady=(0, 16))
            ctk.CTkButton(
                inner, text="Log in via Nexus Mods", width=200, height=36,
                fg_color="#d98f40", hover_color="#e5a04d", text_color="white",
                font=FONT_BOLD, command=self._on_open_settings,
            ).pack(padx=32, pady=(0, 24))
            return

        self._current_panel = self._initial_tab
        self._show_panel(self._initial_tab)

    def _get_panel(self, name: str):
        """Return the tab panel for *name*, building it on first access."""
        panel = self._panels.get(name)
        if panel is None:
            factory = self._panel_factories.get(name)
            frame = self._panel_frames.get(name)
            if factory is None or frame is None:
                return None
            panel = factory(frame)
            self._panels[name] = panel
        return panel

    def _show_panel(self, name: str):
        """Switch to the Browse, Tracked, Endorsed, or Trending panel."""
        for n, frame in self._panel_frames.items():
            if n == name:
                frame.grid()
            else:
                frame.grid_remove()
        self._current_panel = name
        # Highlight active button
        for btn, n in [
            (self._browse_btn, "Browse"),
            (self._tracked_btn, "Tracked"),
            (self._endorsed_btn, "Endorsed"),
            (self._trending_btn, "Trending"),
        ]:
            btn.configure(fg_color=ACCENT_HOV if n == name else ACCENT)
        # Build the panel on first view, then refresh it.
        panel = self._get_panel(name)
        if panel is not None and hasattr(panel, "refresh"):
            panel.refresh()

    def _on_open_nexus(self):
        domain = self._game_domain
        if domain:
            open_url(f"https://www.nexusmods.com/games/{domain}")

    def _do_close(self):
        if self._on_close:
            self._on_close()
        else:
            self.place_forget()

    def _on_popout_toggle(self):
        """Hand off to the launcher, which rebuilds the overlay in the other
        host (docked overlay vs. separate window). The browser is stateless —
        panels reload on show — so a fresh overlay is built in the new host,
        carrying only which tab the user was on."""
        if self._on_rehost is None:
            return
        going_to_popout = not self._is_popped_out
        current_tab = getattr(self, "_current_panel", "Browse")
        self.destroy()
        self._on_rehost(going_to_popout, current_tab)

    def _install_from_tracked(self, entry):
        self._install_nexus_mod(
            entry, "Tracked Mods",
            lambda e: (e.domain_name, e.mod_id, e.name),
        )

    def _install_from_endorsed(self, entry):
        self._install_nexus_mod(
            entry, "Endorsed Mods",
            lambda e: (e.domain_name, e.mod_id, e.name),
        )

    def _install_from_browse(self, entry):
        self._install_nexus_mod(
            entry, "Browse",
            lambda e: (e.domain_name, e.mod_id, e.name),
        )

    def _install_from_trending(self, entry):
        self._install_nexus_mod(
            entry, "Trending",
            lambda e: (e.domain_name, e.mod_id, e.name),
        )

    def _install_nexus_mod(self, entry, label: str, extract_fn: Callable):
        """Download and install a mod from Nexus (Tracked/Endorsed/Browse)."""
        domain, mod_id, mod_name = extract_fn(entry)
        from types import SimpleNamespace
        e = SimpleNamespace(domain_name=domain, mod_id=mod_id, name=mod_name)
        app = self._app_root
        mod_panel = getattr(app, "_mod_panel", None)

        # Flip every panel's matching card to "Reinstall" once the install lands,
        # so the button updates without needing a manual refresh.
        def _on_complete():
            # Only the panels that have actually been built (lazily) exist;
            # un-viewed tabs refresh from scratch when first opened anyway.
            for panel in self._panels.values():
                try:
                    panel.mark_installed(mod_id)
                except Exception:
                    pass

        install_nexus_mod_from_entry(
            app, self._api, self._game, mod_panel, self._log, e, label,
            on_complete=_on_complete,
        )

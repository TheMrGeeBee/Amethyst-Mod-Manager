"""
Trending Mods panel — displays trending mods from Nexus Mods for the
currently selected game.

Trending = mods published in the last 7 days, sorted by endorsements (highest first).
Uses the Nexus GraphQL API with createdAt filter and endorsements sort.
Each mod is shown as a CTkCard with image, name, stats, summary, View/Install.
Right-click offers Open on Nexus, Install Mod, Track Mod, Endorse Mod.
"""

from __future__ import annotations

import threading
import tkinter as tk
from Utils.xdg import open_url
from dataclasses import dataclass
from typing import Callable, Optional

import customtkinter as ctk

from gui.nexus_mod_list_panel_base import _NexusModListPanel
from gui.theme import FONT_HEADER

# Mods per page when browsing trending
PAGE_SIZE = 20


@dataclass
class TrendingModEntry:
    """A trending mod (published in last 7 days, by endorsements)."""
    mod_id: int = 0
    domain_name: str = ""
    name: str = ""
    author: str = ""
    version: str = ""
    summary: str = ""
    endorsement_count: int = 0
    downloads_total: int = 0
    picture_url: str = ""


class TrendingModsPanel(_NexusModListPanel):
    """
    Card-grid panel listing trending mods for the selected game.
    Trending = mods published in the last 7 days, sorted by endorsements.
    """

    _toolbar_column = 1
    _has_cat_sidebar = True

    def __init__(
        self,
        parent_tab: tk.Widget,
        log_fn: Optional[Callable] = None,
        get_api: Optional[Callable] = None,
        get_game_domain: Optional[Callable] = None,
        install_fn: Optional[Callable] = None,
        get_installed_mod_ids: Optional[Callable[[], set]] = None,
    ):
        self._page: int = 0
        super().__init__(
            parent_tab,
            log_fn=log_fn,
            get_api=get_api,
            get_game_domain=get_game_domain,
            install_fn=install_fn,
            get_installed_mod_ids=get_installed_mod_ids,
        )

    def _initial_status_text(self) -> str:
        return "Click Refresh to load trending mods"

    def _build_toolbar(self, toolbar: tk.Frame) -> None:
        self._cat_filter_btn = ctk.CTkButton(
            toolbar, text="Categories", width=80, height=26,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            font=FONT_HEADER, command=lambda: self._toggle_cat_sidebar("Trending: "),
        )
        self._cat_filter_btn.pack(side="left", padx=4, pady=2)

        self._prev_btn = ctk.CTkButton(
            toolbar, text="Previous", width=70, height=26,
            fg_color="#c37800", hover_color="#e28b00", text_color="white",
            font=FONT_HEADER, command=self._go_prev_page,
            state="disabled",
        )
        self._prev_btn.pack(side="left", padx=4, pady=2)

        self._next_btn = ctk.CTkButton(
            toolbar, text="Next", width=52, height=26,
            fg_color="#c37800", hover_color="#e28b00", text_color="white",
            font=FONT_HEADER, command=self._go_next_page,
            state="disabled",
        )
        self._next_btn.pack(side="left", padx=4, pady=2)

    # ------------------------------------------------------------------
    # Refresh / Pagination
    # ------------------------------------------------------------------

    def refresh(self):
        """Fetch page 0 of trending mods."""
        self._page = 0
        self._load_page()

    def _go_prev_page(self):
        if self._page > 0 and not self._loading:
            self._page -= 1
            self._load_page()

    def _go_next_page(self):
        if not self._loading and len(self._entries) >= PAGE_SIZE:
            self._page += 1
            self._load_page()

    def _load_page(self):
        """Fetch the current page of trending mods."""
        api = self._get_api()
        if api is None:
            self._log("Trending: Login to Nexus first")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log("Trending: No game selected.")
            return
        self._sync_game_domain(domain)

        if self._loading:
            return
        self._loading = True
        self._refresh_btn.configure(state="disabled")
        self._prev_btn.configure(state="disabled")
        self._next_btn.configure(state="disabled")
        page = self._page
        self._status_label.configure(text=f"Loading trending (page {page + 1})…")
        self._show_loader()

        def _worker():
            try:
                mod_infos = api.get_trending_mods_graphql(
                    domain,
                    count=PAGE_SIZE,
                    offset=page * PAGE_SIZE,
                    category_names=self._selected_cat_names or None,
                )
                entries: list[TrendingModEntry] = []
                for info in mod_infos:
                    entries.append(TrendingModEntry(
                        mod_id=info.mod_id,
                        domain_name=getattr(info, "domain_name", domain),
                        name=info.name or f"Mod {info.mod_id}",
                        author=info.author or "",
                        version=info.version or "",
                        summary=info.summary or "",
                        endorsement_count=info.endorsement_count or 0,
                        downloads_total=info.downloads_total or 0,
                        picture_url=info.picture_url or "",
                    ))

                def _done():
                    self._entries = entries
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._prev_btn.configure(state="normal" if page > 0 else "disabled")
                    self._next_btn.configure(
                        state="normal" if len(entries) >= PAGE_SIZE else "disabled"
                    )
                    self._build_cards()
                    self._canvas.yview_moveto(0)
                    self._status_label.configure(text=f"Trending: page {page + 1}")
                    self._log(f"Trending: Loaded page {page + 1} — {len(entries)} mod(s)")

                self._parent.after(0, _done)

            except Exception as exc:
                def _err(exc=exc):
                    self._hide_loader()
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._prev_btn.configure(state="normal")
                    self._next_btn.configure(state="normal")
                    self._status_label.configure(text="Error")
                    self._log(f"Trending: Failed — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, event, entry: TrendingModEntry, url: str):
        menu = self._get_or_create_context_menu()
        menu.clear()
        menu.add_command("Open on Nexus", lambda: open_url(url))
        menu.add_command("Install Mod", lambda: self._install_fn(entry))
        menu.add_separator()
        menu.add_command("Track Mod", lambda: self._track_mod(entry))
        menu.add_command("Endorse Mod", lambda: self._endorse_mod(entry))
        menu.popup(event.x_root, event.y_root)

    def _track_mod(self, entry: TrendingModEntry):
        api = self._get_api()
        if api is None:
            self._log("Trending: No API key set.")
            return

        def _worker():
            try:
                api.track_mod(entry.domain_name, entry.mod_id)
                self._parent.after(0,
                    lambda: self._log(f"Trending: Now tracking '{entry.name}' ({entry.mod_id})."))
            except Exception as exc:
                self._parent.after(0,
                    lambda e=exc: self._log(f"Trending: Track failed — {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _endorse_mod(self, entry: TrendingModEntry):
        api = self._get_api()
        if api is None:
            self._log("Trending: No API key set.")
            return

        def _worker():
            try:
                api.endorse_mod(entry.domain_name, entry.mod_id, entry.version)
                self._parent.after(0,
                    lambda: self._log(f"Trending: Endorsed '{entry.name}' ({entry.mod_id})."))
            except Exception as exc:
                self._parent.after(0,
                    lambda e=exc: self._log(f"Trending: Endorse failed — {e}"))

        threading.Thread(target=_worker, daemon=True).start()

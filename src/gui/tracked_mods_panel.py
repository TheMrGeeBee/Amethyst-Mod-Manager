"""
Tracked Mods panel — displays mods the user is tracking on Nexus Mods
for the currently selected game.

Fetches the tracked-mod list via the Nexus v1 REST API, then enriches
each entry with full mod details (name, author, version, summary, picture)
by calling ``get_mod()`` per mod.  Results are cached so switching back to
the tab is instant until the user clicks **Refresh**.

Each mod is shown as a CTkCard with image, name, stats, summary, View/Install.
Right-click offers **Open on Nexus**, **Install Mod**, and **Untrack Mod**.
"""

from __future__ import annotations

import threading
import tkinter as tk
from Utils.xdg import open_url
from dataclasses import dataclass
from typing import Callable, Optional


from gui.nexus_mod_list_panel_base import _NexusModListPanel


@dataclass
class TrackedModEntry:
    """A tracked mod with enriched info."""
    mod_id: int = 0
    domain_name: str = ""
    name: str = ""
    author: str = ""
    version: str = ""
    summary: str = ""
    endorsement_count: int = 0
    downloads_total: int = 0
    picture_url: str = ""


class TrackedModsPanel(_NexusModListPanel):
    """
    Card-grid panel listing mods tracked by the user on Nexus Mods.

    Built into an existing parent widget (a tab frame from CTkTabview).
    """

    def __init__(
        self,
        parent_tab: tk.Widget,
        log_fn: Optional[Callable] = None,
        get_api: Optional[Callable] = None,
        get_game_domain: Optional[Callable] = None,
        install_fn: Optional[Callable] = None,
        get_installed_mod_ids: Optional[Callable[[], set]] = None,
    ):
        super().__init__(
            parent_tab,
            log_fn=log_fn,
            get_api=get_api,
            get_game_domain=get_game_domain,
            install_fn=install_fn,
            get_installed_mod_ids=get_installed_mod_ids,
        )

    def _initial_status_text(self) -> str:
        return "Click Refresh to load tracked mods"

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def refresh(self):
        """Fetch tracked mods from the Nexus API."""
        api = self._get_api()
        if api is None:
            self._log("Tracked Mods: Login to Nexus first")
            return
        domain = self._get_game_domain()
        if not domain:
            self._log("Tracked Mods: No game selected.")
            return

        if self._loading:
            return
        self._loading = True
        self._refresh_btn.configure(state="disabled")
        self._status_label.configure(text="Loading…")
        self._show_loader()

        def _worker():
            try:
                all_tracked = api.get_tracked_mods()
                game_tracked = [t for t in all_tracked if t.get("domain_name", "") == domain]
                total = len(game_tracked)

                # Fetch all mod info in batches of 20 via GraphQL (no rate limit cost)
                mod_ids = [t.get("mod_id", 0) for t in game_tracked if t.get("mod_id", 0) > 0]
                self._parent.after(0, lambda n=total: self._status_label.configure(
                    text=f"Fetching info for {n} mods…"
                ))
                info_map = api.graphql_mod_info_batch([(domain, mid) for mid in mod_ids])

                entries: list[TrackedModEntry] = []
                for t in game_tracked:
                    mod_id = t.get("mod_id", 0)
                    if mod_id <= 0:
                        continue
                    info = info_map.get(mod_id)
                    entry = TrackedModEntry(mod_id=mod_id, domain_name=domain)
                    if info:
                        entry.name              = info.name or f"Mod {mod_id}"
                        entry.author            = info.author
                        entry.version           = info.version
                        entry.summary           = info.summary
                        entry.endorsement_count = info.endorsement_count
                        entry.downloads_total   = info.downloads_total
                        entry.picture_url       = info.picture_url or ""
                    else:
                        entry.name = f"Mod {mod_id}"
                    entries.append(entry)

                def _done():
                    self._entries = entries
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._status_label.configure(
                        text=f"{len(entries)} tracked mod(s) for {domain}"
                    )
                    self._build_cards()
                    self._canvas.yview_moveto(0)
                    self._log(f"Tracked Mods: Found {len(entries)} tracked mod(s) for {domain}.")

                self._parent.after(0, _done)

            except Exception as exc:
                def _err(exc=exc):
                    self._hide_loader()
                    self._loading = False
                    self._refresh_btn.configure(state="normal")
                    self._status_label.configure(text="Error")
                    self._log(f"Tracked Mods: Failed — {exc}")
                self._parent.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Right-click context menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, event, entry: TrackedModEntry, url: str):
        menu = self._get_or_create_context_menu()
        menu.clear()
        menu.add_command("Open on Nexus", lambda: open_url(url))
        menu.add_command("Install Mod", lambda: self._install_fn(entry))
        menu.add_separator()
        menu.add_command("Untrack Mod", lambda: self._untrack_mod(entry))
        menu.popup(event.x_root, event.y_root)

    def _untrack_mod(self, entry: TrackedModEntry):
        api = self._get_api()
        if api is None:
            self._log("Tracked Mods: No API key set.")
            return

        def _worker():
            try:
                api.untrack_mod(entry.domain_name, entry.mod_id)
                def _done():
                    self._log(f"Tracked Mods: Untracked '{entry.name}' ({entry.mod_id}).")
                    self._entries = [e for e in self._entries if e.mod_id != entry.mod_id]
                    self._build_cards()
                    self._status_label.configure(
                        text=f"{len(self._entries)} tracked mod(s) for {entry.domain_name}"
                    )
                self._parent.after(0, _done)
            except Exception as exc:
                self._parent.after(0,
                    lambda e=exc: self._log(f"Tracked Mods: Untrack failed — {e}"))

        threading.Thread(target=_worker, daemon=True).start()

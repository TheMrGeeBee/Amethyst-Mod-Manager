"""
Nexus action mixin for ModListPanel.

Endorse/abstain, the file-picker overlay (Update), background download+install,
reinstall from a saved archive, opening Nexus pages, and the "check updates"
flow (all-mods or a selected subset). Every method here owns its own background
thread and routes results back to the Tk thread via app.after(0, ...).

Host must provide:
- self._log
- self._modlist_path, self._staging_root, self._game
- self.get_download_cancel_event / show_download_progress / update_download_progress / hide_download_progress
- self._scan_update_flags, self._scan_meta_flags_async, self._redraw
- self._endorsed_mods, self._update_btn
- self._call_threadsafe (unused here but kept consistent with other mixins)
- app._nexus_api, app._nexus_downloader, app._topbar (live on the toplevel)
"""

import json
import threading
from pathlib import Path

import customtkinter as ctk

from gui.theme import BG_DEEP
from Utils.config_paths import get_download_cache_dir_for_game
from Utils.xdg import open_url
from gui.ctk_components import CTkAlert, CTkNotification
from gui.game_helpers import _GAMES
from gui.install_mod import install_mod_from_archive, _show_mod_notification
from gui.mod_files_overlay import ModFilesOverlay, resolve_latest_name_match
from Nexus.nexus_download import delete_archive_and_sidecar
from Nexus.nexus_meta import build_meta_from_download, read_meta, write_meta
from Nexus.nexus_update_checker import check_for_updates


def _load_bg3_modio(stem):
    """Load a Games/Baldur's Gate 3/<stem>.py module by file path (the folder
    name has spaces, so it isn't importable by dotted path)."""
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path
    mn = f"{stem}_bg3"
    cached = _sys.modules.get(mn)
    if cached is not None:
        return cached
    bg3_dir = _Path(__file__).resolve().parent.parent / "Games" / "Baldur's Gate 3"
    spec = importlib.util.spec_from_file_location(mn, str(bg3_dir / f"{stem}.py"))
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[mn] = mod
    spec.loader.exec_module(mod)
    return mod


def _modio_key_present(game) -> bool:
    """True if this is BG3 and a mod.io API key is configured."""
    try:
        if getattr(game, "game_id", "") != "baldurs_gate_3":
            return False
        return bool(_load_bg3_modio("modio_key").load_modio_key())
    except Exception:
        return False


def _check_modio_updates(game, staging, log_fn, only_names=None):
    """BG3-only mod.io update check. Returns a list of ModioUpdateInfo (mods
    with a newer file, or unknown installed version), or [] for other games /
    when no mod.io key is configured.  *only_names* restricts the check to
    those staging folder names.  Never raises."""
    try:
        if getattr(game, "game_id", "") != "baldurs_gate_3":
            return []
        from pathlib import Path as _Path
        api_key = _load_bg3_modio("modio_key").load_modio_key()
        if not api_key:
            return []
        checker = _load_bg3_modio("modio_update_checker")
        return checker.check_for_updates(
            _Path(staging), api_key, progress_cb=log_fn, only_names=only_names)
    except Exception as e:
        log_fn(f"mod.io: update check failed — {e}")
        return []


class ModListNexusActionsMixin:
    """Endorse/abstain, update install, reinstall, and update-check flows."""

    def _warn_nexus_login_required(self) -> None:
        """Log the standard message and show an alert with a shortcut to the
        Nexus settings panel so the user knows where to log in."""
        self._log("Nexus: Login to Nexus first (Nexus button).")
        app = self.winfo_toplevel()
        open_settings = (
            app.get_nexus_settings_opener()
            if hasattr(app, "get_nexus_settings_opener") else None
        )
        alert = CTkAlert(
            state="warning",
            title="Nexus login required",
            body_text=(
                "You need to log in to Nexus Mods first.\n\n"
                "Open Nexus settings to log into Nexus"
            ),
            btn1="Open Nexus Settings",
            btn2="Cancel",
            parent=app,
        )
        if alert.get() == "Open Nexus Settings" and open_settings is not None:
            try:
                open_settings()
            except Exception as exc:
                self._log(f"Nexus: Could not open settings — {exc}")

    def _open_nexus_page(self, url: str) -> None:
        """Open a Nexus Mods page in the default browser."""
        if url:
            open_url(url)
            self._log(f"Nexus: Opened {url}")

    def _open_nexus_pages(self, urls: list[str]) -> None:
        """Open multiple Nexus Mods pages, one tab per mod."""
        for url in urls:
            self._open_nexus_page(url)

    def _vote_nexus_mod(self, mod_name: str, domain: str, meta,
                       endorse: bool) -> None:
        """Endorse or abstain on Nexus in a background thread.
        Drives both _endorse_nexus_mod and _abstain_nexus_mod."""
        app = self.winfo_toplevel()
        api = getattr(app, "_nexus_api", None)
        if api is None:
            self._log("Nexus: Login to Nexus first.")
            return
        log_fn = self._log
        verb = "Endorsed" if endorse else "Abstained from"
        action = "Endorse" if endorse else "Abstain"
        api_call = api.endorse_mod if endorse else api.abstain_mod

        def _worker():
            try:
                result = api_call(domain, meta.mod_id, meta.version)

                def _done(res):
                    log_fn(f"Nexus: {verb} '{mod_name}' ({meta.mod_id}).")
                    if res is not None:
                        body = json.dumps(res, indent=None)
                        log_fn(f"  Response: {body[:500]}{'...' if len(body) > 500 else ''}")
                    try:
                        if self._modlist_path is not None:
                            meta_path = self._staging_root / mod_name / "meta.ini"
                            if meta_path.is_file():
                                m = read_meta(meta_path)
                                m.endorsed = endorse
                                write_meta(meta_path, m)
                    except Exception:
                        pass
                    if endorse:
                        self._endorsed_mods.add(mod_name)
                    else:
                        self._endorsed_mods.discard(mod_name)
                    self._redraw()

                app.after(0, lambda: _done(result))
            except Exception as exc:
                app.after(0, lambda e=exc: log_fn(f"Nexus: {action} failed — {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _endorse_nexus_mod(self, mod_name: str, domain: str, meta) -> None:
        self._vote_nexus_mod(mod_name, domain, meta, endorse=True)

    def _abstain_nexus_mod(self, mod_name: str, domain: str, meta) -> None:
        self._vote_nexus_mod(mod_name, domain, meta, endorse=False)

    def _vote_selected_mods(self, targets: list[tuple], endorse: bool) -> None:
        """Endorse or abstain a list of (mod_name, domain, meta) tuples.

        Each target reuses _vote_nexus_mod, which spins its own background
        thread — so this just fans them out and lets the API/Nexus rate-limit
        itself."""
        if not targets:
            return
        app = self.winfo_toplevel()
        if getattr(app, "_nexus_api", None) is None:
            self._warn_nexus_login_required()
            return
        verb = "Endorsing" if endorse else "Abstaining from"
        self._log(f"Nexus: {verb} {len(targets)} mod(s)…")
        for mod_name, domain, meta in targets:
            self._vote_nexus_mod(mod_name, domain, meta, endorse=endorse)

    def _endorse_selected_mods(self, targets: list[tuple]) -> None:
        self._vote_selected_mods(targets, endorse=True)

    def _abstain_selected_mods(self, targets: list[tuple]) -> None:
        self._vote_selected_mods(targets, endorse=False)

    def _prompt_remove_previous_version(self, old_mod_name: str,
                                        new_mod_name: str,
                                        _attempt: int = 0) -> None:
        """After Change Version installs a differently-named mod, offer to
        remove the previous version. Same-mod-id files may also be optional
        variants, so removal must be opt-in.

        On Remove, the new mod inherits the old mod's modlist position so the
        user doesn't have to re-find where it belonged."""
        # The new mod is added by install via reload_after_install, which is
        # scheduled after this callback fires. Wait for it to appear.
        new_idx = next((i for i, e in enumerate(self._entries)
                        if not e.is_separator and e.name == new_mod_name), -1)
        if new_idx < 0 and _attempt < 40:
            self.after(50, lambda: self._prompt_remove_previous_version(
                old_mod_name, new_mod_name, _attempt + 1))
            return
        old_idx = next((i for i, e in enumerate(self._entries)
                        if not e.is_separator and e.name == old_mod_name), -1)
        if old_idx < 0:
            return
        alert = CTkAlert(
            state="info",
            title="Remove previous version?",
            body_text=(
                f"'{new_mod_name}' was installed as a new mod (different folder name) "
                f"because it did not replace '{old_mod_name}'.\n\n"
                f"Remove the previous version '{old_mod_name}'?\n\n"
                "The new mod will take the previous version's position in the modlist.\n\n"
                "Choose Keep if this is an optional/alternative variant rather than "
                "a replacement."
            ),
            btn1="Remove",
            btn2="Keep",
            parent=self.winfo_toplevel(),
        )
        if alert.get() != "Remove":
            return
        # Re-resolve after the dialog: indices may have changed during wait_window.
        old_idx = next((i for i, e in enumerate(self._entries)
                        if not e.is_separator and e.name == old_mod_name), -1)
        if old_idx < 0:
            return
        new_idx = next((i for i, e in enumerate(self._entries)
                        if not e.is_separator and e.name == new_mod_name), -1)
        # Mirror the old mod's enabled state onto the new one — a disabled
        # mod swapped to a different version should stay disabled, and vice
        # versa, so the user doesn't have to re-toggle after the swap.
        if new_idx >= 0:
            old_entry = self._entries[old_idx]
            new_entry = self._entries[new_idx]
            if new_entry.enabled != old_entry.enabled:
                new_entry.enabled = old_entry.enabled
                new_var = self._check_vars[new_idx]
                if new_var is not None:
                    new_var.set(old_entry.enabled)
        if new_idx >= 0 and new_idx != old_idx:
            self._reposition_entry(new_idx, old_idx)
            # The pop+insert may have shifted old_idx by one.
            old_idx = next((i for i, e in enumerate(self._entries)
                            if not e.is_separator and e.name == old_mod_name), -1)
            if old_idx < 0:
                return
        self._remove_mod(old_idx, skip_confirm=True)

    def _reposition_entry(self, src: int, dst: int) -> None:
        """Move `_entries[src]` (and its parallel `_check_vars` slot) so that
        afterwards the moved entry occupies the slot the entry at `dst`
        originally held. Caches/state are invalidated; persistence is left to
        the subsequent `_remove_mod` call which already saves and rebuilds."""
        if src == dst or not (0 <= src < len(self._entries)) or not (0 <= dst < len(self._entries)):
            return
        entry = self._entries.pop(src)
        var = self._check_vars.pop(src)
        # After pop, indices > src shift down by one.
        insert_at = dst - 1 if src < dst else dst
        self._entries.insert(insert_at, entry)
        self._check_vars.insert(insert_at, var)
        self._sel_idx = -1
        if isinstance(getattr(self, "_sel_set", None), set):
            self._sel_set.clear()
        self._compute_bundle_groups()
        self._invalidate_derived_caches()

    def _open_modio_mod_page(self, mod_name: str) -> None:
        """Open the mod.io page for a mod.io-tracked mod (BG3).

        The page is slug-based (.../m/<name_id>); the numeric id does NOT
        resolve in a browser.  We use the ``modioProfileUrl`` captured at
        install/update time, fetching it on the fly if it's missing.
        """
        import configparser
        import webbrowser
        meta_path = self._staging_root / mod_name / "meta.ini"
        if not meta_path.is_file():
            self._log(f"mod.io: No metadata for {mod_name}")
            return
        try:
            cp = configparser.ConfigParser(interpolation=None)
            cp.read(str(meta_path), encoding="utf-8")
            url = (cp.get("General", "modioProfileUrl", fallback="") or "").strip()
            mod_id = int(cp.get("General", "modioModId", fallback="0") or "0")
        except Exception as exc:
            self._log(f"mod.io: Could not read metadata — {exc}")
            return

        # Backfill the slug URL via the API if we don't have it stored yet.
        if not url and mod_id > 0:
            api_key = _load_bg3_modio("modio_key").load_modio_key() \
                if _modio_key_present(self._game) else ""
            if api_key:
                try:
                    api = _load_bg3_modio("modio_api").ModioAPI(api_key)
                    url = api.get_mod_profile_url(mod_id)
                    if url:
                        cp.set("General", "modioProfileUrl", url)
                        with open(meta_path, "w", encoding="utf-8") as f:
                            cp.write(f)
                except Exception as exc:
                    self._log(f"mod.io: Could not fetch page URL — {exc}")

        if not url:
            self._log(f"mod.io: No page URL for {mod_name} "
                      f"(run Check Updates with a mod.io key set).")
            return
        self._log(f"mod.io: Opening {url}")
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._log(f"mod.io: Could not open browser — {exc}")

    def _update_nexus_mod(self, mod_name: str) -> None:
        """Show the mod files overlay so the user can pick which file to install."""
        app = self.winfo_toplevel()
        if getattr(app, "_nexus_api", None) is None:
            self._warn_nexus_login_required()
            return
        if self._modlist_path is None:
            return
        staging_root = self._staging_root
        meta_path = staging_root / mod_name / "meta.ini"
        if not meta_path.is_file():
            self._log(f"Nexus: No metadata for {mod_name}")
            return
        try:
            meta = read_meta(meta_path)
        except Exception as exc:
            self._log(f"Nexus: Could not read metadata — {exc}")
            return
        game_name = app._topbar._game_var.get()
        game = _GAMES.get(game_name)
        if game is None or not game.is_configured():
            self._log("Nexus: No configured game selected.")
            return
        game_domain = meta.game_domain or game.nexus_game_domain
        if not meta.mod_id:
            self._log(f"Nexus: No mod ID in metadata for {mod_name}.")
            return

        api = app._nexus_api
        mod_panel = self
        log_fn = self._log

        def _fetch_files():
            files_resp = api.get_mod_files(game_domain, meta.mod_id)
            return files_resp.files

        def _on_install(file_id: int, file_name: str):
            self._download_and_install_nexus_file(
                mod_name=mod_name,
                game_domain=game_domain,
                meta=meta,
                meta_path=meta_path,
                game=game,
                file_id=file_id,
            )

        def _on_ignore(state: bool):
            try:
                m = read_meta(meta_path)
                m.ignore_update = state
                if state:
                    m.has_update = False
                    m.ignored_version = m.latest_version
                else:
                    m.ignored_version = ""
                write_meta(meta_path, m)
            except Exception as exc:
                log_fn(f"Nexus: Could not save ignore flag — {exc}")
            app.after(0, mod_panel._scan_update_flags)
            app.after(0, mod_panel._redraw)

        self._close_mod_files_overlay()

        def _build(popped_out: bool):
            # Always clear any existing host first (when docking, the overlay
            # inside the popout window was already cleaned up by the toggle).
            self._close_mod_files_overlay()

            def _on_rehost(going_to_popout):
                _build(going_to_popout)

            common = dict(
                mod_name=mod_name,
                game_domain=game_domain,
                mod_id=meta.mod_id,
                installed_file_id=meta.file_id,
                ignore_update=meta.ignore_update,
                on_install=_on_install,
                on_ignore=_on_ignore,
                on_close=self._close_mod_files_overlay,
                fetch_files_fn=_fetch_files,
                on_rehost=_on_rehost,
                is_popped_out=popped_out,
            )

            if popped_out:
                host = ctk.CTkToplevel(app, fg_color=BG_DEEP)
                host.title(f"Change Version — {mod_name}")
                try:
                    root = app.winfo_toplevel()
                    root.update_idletasks()
                    w = max(int(root.winfo_width() * 0.85), 820)
                    h = max(int(root.winfo_height() * 0.85), 560)
                    x = root.winfo_rootx() + (root.winfo_width() - w) // 2
                    y = root.winfo_rooty() + (root.winfo_height() - h) // 2
                    host.geometry(f"{w}x{h}+{max(x, 0)}+{max(y, 0)}")
                except Exception:
                    host.geometry("900x620")
                host.minsize(640, 420)
                host.grid_rowconfigure(0, weight=1)
                host.grid_columnconfigure(0, weight=1)
                # Closing the popout window closes the overlay entirely.
                host.protocol("WM_DELETE_WINDOW", self._close_mod_files_overlay)
                self._mod_files_window = host

                panel = ModFilesOverlay(parent=host, **common)
                panel.grid(row=0, column=0, sticky="nsew")
                self._mod_files_panel = panel
                try:
                    # Independent window (not transient/topmost) so the manager
                    # underneath stays usable.
                    host.attributes("-topmost", False)
                    host.lift()
                    host.focus_force()
                except Exception:
                    pass
            else:
                panel = ModFilesOverlay(parent=self, **common)
                panel.place(relx=0, rely=0, relwidth=1, relheight=1)
                self._mod_files_panel = panel

        _build(popped_out=False)

    def _close_mod_files_overlay(self):
        panel = getattr(self, "_mod_files_panel", None)
        if panel is not None:
            panel.cleanup()
            try:
                panel.place_forget()
            except Exception:
                pass
            panel.destroy()
            self._mod_files_panel = None
        win = getattr(self, "_mod_files_window", None)
        if win is not None:
            self._mod_files_window = None
            try:
                win.destroy()
            except Exception:
                pass

    def _download_and_install_nexus_file(
        self,
        mod_name: str,
        game_domain: str,
        meta,
        meta_path: Path,
        game,
        file_id: int,
        on_done=None,
        auto_overwrite_name: str = "",
    ) -> None:
        """Download a specific Nexus file and install it.

        *on_done*, if given, is called on the Tk thread once the install
        finishes (success or failure) — used to chain sequential batch updates.

        *auto_overwrite_name*, if given, forces the install into that mod folder
        (``preferred_name``) and auto-confirms Replace-All, so Quick Update
        never raises the "Mod Already Exists" / "Remove previous version"
        dialogs for an already-confirmed name match."""
        app = self.winfo_toplevel()
        log_fn = self._log
        mod_panel = self
        cancel_event = self.get_download_cancel_event()
        self.show_download_progress(f"Updating: {mod_name}", cancel=cancel_event)

        def _worker():
            api = app._nexus_api
            downloader = app._nexus_downloader

            is_premium = False
            try:
                user = api.validate()
                is_premium = user.is_premium
            except Exception:
                pass

            if not is_premium:
                files_url = f"https://www.nexusmods.com/{game_domain}/mods/{meta.mod_id}?tab=files"

                def _fallback():
                    mod_panel.hide_download_progress(cancel=cancel_event)
                    open_url(files_url)
                    log_fn("Nexus: Premium required for direct download.")
                    log_fn("Nexus: Opened files page — click \"Download with Mod Manager\" there.")
                    if on_done is not None:
                        on_done()

                app.after(0, _fallback)
                return

            mod_info = None
            file_info = None
            try:
                mod_info = api.get_mod(game_domain, meta.mod_id)
                files_resp = api.get_mod_files(game_domain, meta.mod_id)
                for f in files_resp.files:
                    if f.file_id == file_id:
                        file_info = f
                        break
            except Exception as exc:
                app.after(0, lambda e=exc: log_fn(f"Nexus: Could not fetch mod info — {e}"))
                app.after(0, lambda: mod_panel.hide_download_progress(cancel=cancel_event))
                if on_done is not None:
                    app.after(0, on_done)
                return

            result = downloader.download_file(
                game_domain=game_domain,
                mod_id=meta.mod_id,
                file_id=file_id,
                progress_cb=lambda cur, total: app.after(
                    0, lambda c=cur, t=total: mod_panel.update_download_progress(c, t, cancel=cancel_event)
                ),
                cancel=cancel_event,
                dest_dir=get_download_cache_dir_for_game(getattr(game, "name", "") or ""),
            )

            if result.success and result.file_path:
                status_bar = getattr(app, "_status", None)

                def _extract_progress(done: int, total: int, phase: str | None = None):
                    if status_bar is not None:
                        app.after(0, lambda d=done, t=total, p=phase:
                                  status_bar.set_progress(d, t, p, title="Extracting"))

                def _install_worker():
                    def _cleanup(is_fomod: bool = False,
                                 installed_mod_name: str | None = None):
                        # Quick Update forces the install into the existing folder
                        # (auto_overwrite_name), so there is never a separate
                        # previous version to offer to remove.
                        if (not auto_overwrite_name and installed_mod_name
                                and installed_mod_name != mod_name):
                            app.after(0, lambda new=installed_mod_name:
                                      mod_panel._prompt_remove_previous_version(mod_name, new))
                        from Utils.ui_config import (
                            load_clear_archive_after_install,
                            load_keep_fomod_archives,
                        )
                        if not load_clear_archive_after_install():
                            return
                        if is_fomod and load_keep_fomod_archives():
                            return
                        delete_archive_and_sidecar(Path(result.file_path))

                    try:
                        prebuilt = build_meta_from_download(
                            game_domain=game_domain,
                            mod_id=meta.mod_id,
                            file_id=file_id,
                            archive_name=result.file_name,
                            mod_info=mod_info,
                            file_info=file_info,
                        )
                        prebuilt.has_update = False
                    except Exception as exc:
                        log_fn(f"Nexus: Warning — could not build metadata: {exc}")
                        prebuilt = None

                    try:
                        install_mod_from_archive(
                            str(result.file_path), app, log_fn, game, mod_panel,
                            prebuilt_meta=prebuilt,
                            on_installed=_cleanup,
                            progress_fn=_extract_progress,
                            preferred_name=auto_overwrite_name,
                            overwrite_existing=True if auto_overwrite_name else None,
                            clear_progress_fn=lambda: app.after(
                                0, status_bar.clear_progress
                            ) if status_bar is not None else None,
                        )
                        app.after(0, mod_panel._scan_update_flags)
                        app.after(0, mod_panel._redraw)
                        log_fn(f"Nexus: {mod_name} updated successfully.")
                    finally:
                        if status_bar is not None:
                            app.after(0, status_bar.clear_progress)
                        if on_done is not None:
                            app.after(0, on_done)

                def _install():
                    try:
                        if app.grab_current() is not None:
                            app.after(500, _install)
                            return
                    except Exception:
                        pass
                    mod_panel.hide_download_progress(cancel=cancel_event)
                    log_fn(f"Nexus: Installing update for {mod_name}...")
                    threading.Thread(target=_install_worker, daemon=True).start()

                app.after(0, _install)
            else:
                def _fail():
                    mod_panel.hide_download_progress(cancel=cancel_event)
                    log_fn(f"Nexus: Update download failed — {result.error}")
                    if on_done is not None:
                        on_done()

                app.after(0, _fail)

        threading.Thread(target=_worker, daemon=True).start()

    def _quick_update_mods(self, mod_names: list[str]) -> None:
        """Auto-install the latest name-matched version for each update-flagged
        mod. Mods whose latest file isn't a name match (the orange-highlighted
        file in Change Version) are skipped — the user must update them manually."""
        app = self.winfo_toplevel()
        if getattr(app, "_nexus_api", None) is None:
            self._warn_nexus_login_required()
            return
        game = self._game
        if game is None or not game.is_configured():
            self._log("Nexus: No configured game selected.")
            return
        # Defensive: only mods that actually have an update pending.
        targets = [m for m in mod_names if m in self._update_mods]
        if not targets:
            self._log("Nexus: No mods with a pending update to quick-update.")
            return

        api = app._nexus_api
        log_fn = self._log
        staging_root = self._staging_root
        # Plan: resolve each mod's name-matched file_id up front (network,
        # parallel), then pipeline downloads + installs collection-style.
        self._log(f"Nexus: Quick Update — checking {len(targets)} mod(s)...")

        def _resolve_one(mod_name: str) -> tuple:
            """Returns ("queued", payload) or ("skipped", reason)."""
            meta_path = staging_root / mod_name / "meta.ini"
            if not meta_path.is_file():
                return ("skipped", "no Nexus metadata")
            try:
                meta = read_meta(meta_path)
            except Exception as exc:
                return ("skipped", f"could not read metadata ({exc})")
            if not meta.mod_id:
                return ("skipped", "no Nexus mod id in metadata")
            game_domain = meta.game_domain or game.nexus_game_domain
            try:
                files = api.get_mod_files(game_domain, meta.mod_id).files
            except Exception as exc:
                return ("skipped", f"could not fetch file list ({exc})")
            fid, _old = resolve_latest_name_match(files, meta.file_id, mod_name)
            if fid <= 0 or fid == meta.file_id:
                return ("skipped", "no name-matched update — use Change Version")
            file_info = next((f for f in files if f.file_id == fid), None)
            return ("queued", (mod_name, game_domain, meta, fid, file_info))

        def _resolve_worker():
            import concurrent.futures as _cf
            queue: list[tuple] = []
            skipped: list[tuple[str, str]] = []   # (mod_name, reason)
            with _cf.ThreadPoolExecutor(max_workers=4) as pool:
                for mod_name, (status, payload) in zip(
                        targets, pool.map(_resolve_one, targets)):
                    if status == "queued":
                        queue.append(payload)
                    else:
                        skipped.append((mod_name, payload))
                        app.after(0, lambda m=mod_name, r=payload:
                                  log_fn(f"Nexus: {m} — {r}, skipped."))
            app.after(0, lambda: self._quick_update_run(queue, skipped))

        threading.Thread(target=_resolve_worker, daemon=True).start()

    def _quick_update_run(self, queue: list[tuple],
                          skipped: list[tuple[str, str]]) -> None:
        """Download the resolved name-matched files in parallel (same worker
        count as collection installs) and install each as its download lands.
        Installs run on a single consumer thread so modlist/plugins/index
        writes never race."""
        app = self.winfo_toplevel()
        total = len(queue)
        if total == 0:
            self._quick_update_finish(0, [], skipped)
            return

        api = app._nexus_api
        downloader = app._nexus_downloader
        game = self._game
        log_fn = self._log
        mod_panel = self
        status_bar = getattr(app, "_status", None)

        is_premium = False
        try:
            is_premium = api.validate().is_premium
        except Exception:
            pass
        if not is_premium:
            log_fn("Nexus: Premium required for Quick Update direct downloads.")
            self._quick_update_finish(
                0, [(item[0], "Premium required for direct download") for item in queue],
                skipped)
            return

        from Utils.ui_config import load_collection_settings
        try:
            dl_workers = max(1, int(load_collection_settings().get("max_concurrent", 3)))
        except Exception:
            dl_workers = 3

        import queue as _qmod
        install_q: _qmod.Queue = _qmod.Queue()
        _SENTINEL = None
        failed: list[tuple[str, str]] = []
        failed_lock = threading.Lock()

        # One shared popup for the whole batch — per-download popups stack up
        # fast with many mods. Progress is the aggregate byte count across all
        # downloads; totals are seeded from the Nexus file sizes and corrected
        # by each download's own reported total.
        batch_cancel = mod_panel.get_download_cancel_event()
        mod_panel.show_download_progress(
            f"Quick Update: downloading {total} mod(s)", cancel=batch_cancel)
        progress_lock = threading.Lock()
        progress: dict[str, list[int]] = {}   # mod_name → [cur_bytes, total_bytes]
        for _item in queue:
            _fi = _item[4]
            _size = 0
            if _fi is not None:
                _size = (getattr(_fi, "size_in_bytes", None)
                         or (getattr(_fi, "size_kb", 0) or 0) * 1024 or 0)
            progress[_item[0]] = [0, int(_size)]

        def _post_aggregate():
            with progress_lock:
                cur = sum(c for c, _t in progress.values())
                tot = sum(t for _c, t in progress.values())
            app.after(0, lambda c=cur, t=tot:
                      mod_panel.update_download_progress(c, t, cancel=batch_cancel))

        def _extract_progress(done: int, total_b: int, phase: str | None = None):
            if status_bar is not None:
                app.after(0, lambda d=done, t=total_b, p=phase:
                          status_bar.set_progress(d, t, p, title="Extracting"))

        def _download_one(item):
            mod_name, game_domain, meta, file_id, file_info = item
            try:
                if batch_cancel.is_set():
                    with failed_lock:
                        failed.append((mod_name, "cancelled"))
                    return
                try:
                    mod_info = api.get_mod(game_domain, meta.mod_id)
                except Exception as exc:
                    with failed_lock:
                        failed.append((mod_name, f"could not fetch mod info ({exc})"))
                    return

                def _on_progress(cur, tot, _m=mod_name):
                    with progress_lock:
                        slot = progress[_m]
                        slot[0] = cur
                        if tot:
                            slot[1] = tot
                    _post_aggregate()

                result = downloader.download_file(
                    game_domain=game_domain,
                    mod_id=meta.mod_id,
                    file_id=file_id,
                    progress_cb=_on_progress,
                    cancel=batch_cancel,
                    dest_dir=get_download_cache_dir_for_game(getattr(game, "name", "") or ""),
                )
                if not (result.success and result.file_path):
                    with failed_lock:
                        failed.append((mod_name, f"download failed — {result.error}"))
                    return
                with progress_lock:
                    slot = progress[mod_name]
                    slot[1] = slot[1] or slot[0]
                    slot[0] = slot[1]
                _post_aggregate()
                try:
                    prebuilt = build_meta_from_download(
                        game_domain=game_domain,
                        mod_id=meta.mod_id,
                        file_id=file_id,
                        archive_name=result.file_name,
                        mod_info=mod_info,
                        file_info=file_info,
                    )
                    prebuilt.has_update = False
                except Exception as exc:
                    app.after(0, lambda e=exc:
                              log_fn(f"Nexus: Warning — could not build metadata: {e}"))
                    prebuilt = None
                install_q.put((mod_name, result, prebuilt))
            except Exception as exc:
                with failed_lock:
                    failed.append((mod_name, f"download error ({exc})"))

        def _install_consumer():
            updated = 0
            while True:
                item = install_q.get()
                if item is _SENTINEL:
                    break
                mod_name, result, prebuilt = item
                app.after(0, lambda m=mod_name:
                          log_fn(f"Nexus: Installing update for {m}..."))
                # One pooled progress popup for the whole batch — created on
                # the first install, relabelled per mod, cleared after the
                # sentinel. Per-mod create/destroy churn made popups lose
                # their stacked position above the download popup.
                if status_bar is not None:
                    app.after(0, lambda m=mod_name: status_bar.set_progress(
                        0, 0, phase=f"Installing {m}…", title="Quick Update"))

                def _cleanup(is_fomod: bool = False,
                             installed_mod_name: str | None = None,
                             _path=result.file_path):
                    from Utils.ui_config import (
                        load_clear_archive_after_install,
                        load_keep_fomod_archives,
                    )
                    if not load_clear_archive_after_install():
                        return
                    if is_fomod and load_keep_fomod_archives():
                        return
                    delete_archive_and_sidecar(Path(_path))

                try:
                    install_mod_from_archive(
                        str(result.file_path), app, log_fn, game, mod_panel,
                        prebuilt_meta=prebuilt,
                        on_installed=_cleanup,
                        progress_fn=_extract_progress,
                        preferred_name=mod_name,
                        overwrite_existing=True,
                        suppress_notification=True,
                        skip_reload=True,
                    )
                    updated += 1
                    app.after(0, lambda m=mod_name:
                              log_fn(f"Nexus: {m} updated successfully."))
                except Exception as exc:
                    with failed_lock:
                        failed.append((mod_name, f"install failed ({exc})"))
            if status_bar is not None:
                app.after(0, status_bar.clear_progress)
            # Per-install reloads were suppressed (skip_reload=True) to avoid
            # one full UI rebuild per mod, which lags on large modlists. Do a
            # single reload now that every update has been installed.
            if updated and mod_panel is not None:
                app.after(0, mod_panel.reload_after_install)
            app.after(0, lambda n=updated:
                      self._quick_update_finish(n, failed, skipped))

        def _download_all():
            import concurrent.futures as _cf
            with _cf.ThreadPoolExecutor(max_workers=dl_workers) as pool:
                list(pool.map(_download_one, queue))
            app.after(0, lambda:
                      mod_panel.hide_download_progress(cancel=batch_cancel))
            install_q.put(_SENTINEL)

        threading.Thread(target=_install_consumer, daemon=True).start()
        threading.Thread(target=_download_all, daemon=True).start()

    def _quick_update_finish(self, updated: int,
                             failed: list[tuple[str, str]],
                             skipped: list[tuple[str, str]]) -> None:
        """Log the batch summary and alert the user about mods Quick Update
        couldn't handle (no name match, download/install failure)."""
        self._scan_update_flags()
        self._redraw()
        self._log(
            f"Nexus: Quick Update — {updated} updated, "
            f"{len(skipped)} skipped (no name match), {len(failed)} failed."
        )
        for name, reason in failed:
            self._log(f"Nexus: Quick Update — {name}: {reason}")
        app = self.winfo_toplevel()
        if updated:
            _show_mod_notification(
                app, f"Quick Update: updated {updated} mod(s)")
        problems = len(skipped) + len(failed)
        if not problems:
            return
        parts = []
        if skipped:
            parts.append(f"{len(skipped)} had no name-matched file")
        if failed:
            parts.append(f"{len(failed)} failed to download or install")
        CTkAlert(
            state="warning",
            title="Quick Update",
            body_text=(
                f"{problems} mod(s) could not be quick-updated: "
                + " and ".join(parts) + ".\n\n"
                "See the log for the full list. Use Update → Change Version "
                "on each mod to update it manually."
            ),
            btn1="OK", btn2="",
            parent=app,
        )

    def _reinstall_mod(self, mod_name: str, archive_path: Path) -> None:
        """Reinstall a mod from its recorded installation archive."""
        app = self.winfo_toplevel()
        topbar = getattr(app, "_topbar", None)
        game = _GAMES.get(topbar._game_var.get()) if topbar else None
        if game is None or not game.is_configured():
            self._log("Reinstall: No configured game selected.")
            return
        if not archive_path.is_file():
            self._log(f"Reinstall: Archive not found — {archive_path}")
            return
        self._log(f"Reinstalling '{mod_name}' from {archive_path.name}…")
        status_bar = getattr(app, "_status", None)

        def _extract_progress(done: int, total: int, phase: str | None = None):
            if status_bar is not None:
                app.after(0, lambda d=done, t=total, p=phase:
                          status_bar.set_progress(d, t, p, title="Extracting"))

        def _worker():
            try:
                install_mod_from_archive(
                    str(archive_path), app, self._log, game, mod_panel=self,
                    progress_fn=_extract_progress,
                    clear_progress_fn=lambda: app.after(
                        0, status_bar.clear_progress
                    ) if status_bar is not None else None,
                )
            finally:
                if status_bar is not None:
                    app.after(0, status_bar.clear_progress)

        threading.Thread(target=_worker, daemon=True).start()

    def _run_check_updates(self, target_names: set[str], up_to_date_msg: str,
                           log_clean_reqs: bool):
        """Shared body for _on_check_updates and _on_check_updates_for_mods.
        target_names is the set of mod names to query; up_to_date_msg is logged
        when the API reports nothing pending; log_clean_reqs controls whether
        the "All mod requirements satisfied" line is emitted on a clean run
        (only the all-mods variant logs it)."""
        app = self.winfo_toplevel()
        game = self._game
        if game is None or not game.is_configured():
            self._log("No configured game selected.")
            return
        # mod.io (BG3) can run without a Nexus login.  Only bail for "needs
        # Nexus login" when there's also no mod.io key to fall back on.
        _has_modio = _modio_key_present(game)
        if app._nexus_api is None and not _has_modio:
            self._warn_nexus_login_required()
            return

        staging = game.get_effective_mod_staging_path()
        self._update_btn.configure(text="Checking...", state="disabled")
        log_fn = self._log

        _notif = CTkNotification(
            app, state="info",
            message=f"Checking {len(target_names)} mod(s) for updates...",
        )

        def _close_notif():
            try:
                if _notif.winfo_exists():
                    _notif.destroy()
            except Exception:
                pass

        def _worker():
            try:
                _have_nexus = app._nexus_api is not None

                # Run the mod.io check (BG3) in parallel with the Nexus check —
                # they hit different APIs and write disjoint meta.ini keys.  A
                # mod on both sites could in theory race on the same meta.ini,
                # but both writers preserve unknown keys, so the worst case is a
                # one-check-stale cached "latest" value that self-heals on the
                # next run — never corruption.  Both log via the thread-safe wrapper.
                _modio_box = {"results": []}

                def _modio_work():
                    _modio_box["results"] = _check_modio_updates(
                        game, staging,
                        lambda m: app.after(0, lambda msg=m: log_fn(msg)),
                        only_names=target_names)

                _modio_thread = threading.Thread(target=_modio_work, daemon=True)
                _modio_thread.start()

                if _have_nexus:
                    results, missing = check_for_updates(
                        app._nexus_api, staging,
                        game_domain=game.nexus_game_domain,
                        progress_cb=lambda m: app.after(0, lambda msg=m: log_fn(msg)),
                        enabled_only=target_names,
                    )
                else:
                    results, missing = [], []

                _modio_thread.join()
                modio_results = _modio_box["results"]

                def _done():
                    _close_notif()
                    self._update_btn.configure(text="Check Updates", state="normal")
                    if _have_nexus:
                        if results:
                            log_fn(f"Nexus: {len(results)} update(s) available!")
                            for u in results:
                                log_fn(f"  ↑ {u.mod_name}: {u.installed_version} → {u.latest_version}")
                        else:
                            log_fn(up_to_date_msg)
                    if modio_results:
                        log_fn(f"mod.io: {len(modio_results)} update(s) available!")
                        for u in modio_results:
                            if u.unknown:
                                log_fn(f"  ? {u.mod_name}: installed version unknown "
                                       f"(latest {u.latest_version})")
                            else:
                                log_fn(f"  ↑ {u.mod_name}: {u.installed_version} → {u.latest_version}")
                    elif not _have_nexus:
                        log_fn("mod.io: All tracked mods are up to date.")
                    if missing:
                        log_fn(f"Nexus: {len(missing)} mod(s) have missing requirements!")
                        for m in missing:
                            names = ", ".join(r.mod_name for r in m.missing[:3])
                            suffix = f" (+{len(m.missing) - 3} more)" if len(m.missing) > 3 else ""
                            log_fn(f"  ⚠ {m.mod_name}: needs {names}{suffix}")
                    elif log_clean_reqs and _have_nexus:
                        log_fn("Nexus: All mod requirements satisfied.")
                    self._scan_meta_flags_async()

                app.after(0, _done)
            except Exception as exc:
                app.after(0, lambda e=exc: (
                    _close_notif(),
                    self._update_btn.configure(text="Check Updates", state="normal"),
                    log_fn(f"Nexus: Check failed — {e}"),
                ))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_check_updates(self):
        """Check for updates across every mod in the current modlist."""
        all_names = {e.name for e in self._entries if not e.is_separator}
        self._run_check_updates(
            all_names, "Nexus: All mods are up to date.", log_clean_reqs=True,
        )

    def _on_check_updates_for_mods(self, mod_names: list[str]):
        """Check updates for a specific subset (right-click menu)."""
        if not mod_names:
            return
        self._run_check_updates(
            set(mod_names), "Nexus: Selected mod(s) are up to date.",
            log_clean_reqs=False,
        )

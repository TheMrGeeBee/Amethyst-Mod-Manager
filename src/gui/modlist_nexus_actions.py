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

from Utils.config_paths import get_download_cache_dir_for_game
from Utils.xdg import open_url
from gui.ctk_components import CTkAlert, CTkNotification
from gui.game_helpers import _GAMES
from gui.install_mod import install_mod_from_archive
from gui.mod_files_overlay import ModFilesOverlay
from Nexus.nexus_download import delete_archive_and_sidecar
from Nexus.nexus_meta import build_meta_from_download, read_meta, write_meta
from Nexus.nexus_update_checker import check_for_updates


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
        panel = ModFilesOverlay(
            parent=self,
            mod_name=mod_name,
            game_domain=game_domain,
            mod_id=meta.mod_id,
            installed_file_id=meta.file_id,
            ignore_update=meta.ignore_update,
            on_install=_on_install,
            on_ignore=_on_ignore,
            on_close=self._close_mod_files_overlay,
            fetch_files_fn=_fetch_files,
        )
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._mod_files_panel = panel

    def _close_mod_files_overlay(self):
        panel = getattr(self, "_mod_files_panel", None)
        if panel is not None:
            panel.cleanup()
            panel.place_forget()
            panel.destroy()
            self._mod_files_panel = None

    def _download_and_install_nexus_file(
        self,
        mod_name: str,
        game_domain: str,
        meta,
        meta_path: Path,
        game,
        file_id: int,
    ) -> None:
        """Download a specific Nexus file and install it."""
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
                    def _cleanup(is_fomod: bool = False):
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
                            clear_progress_fn=lambda: app.after(
                                0, status_bar.clear_progress
                            ) if status_bar is not None else None,
                        )
                    finally:
                        if status_bar is not None:
                            app.after(0, status_bar.clear_progress)
                    app.after(0, mod_panel._scan_update_flags)
                    app.after(0, mod_panel._redraw)
                    log_fn(f"Nexus: {mod_name} updated successfully.")

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

                app.after(0, _fail)

        threading.Thread(target=_worker, daemon=True).start()

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
        if app._nexus_api is None:
            self._warn_nexus_login_required()
            return
        game = self._game
        if game is None or not game.is_configured():
            self._log("No configured game selected.")
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
                results, missing = check_for_updates(
                    app._nexus_api, staging,
                    game_domain=game.nexus_game_domain,
                    progress_cb=lambda m: app.after(0, lambda msg=m: log_fn(msg)),
                    enabled_only=target_names,
                )

                def _done():
                    _close_notif()
                    self._update_btn.configure(text="Check Updates", state="normal")
                    if results:
                        log_fn(f"Nexus: {len(results)} update(s) available!")
                        for u in results:
                            log_fn(f"  ↑ {u.mod_name}: {u.installed_version} → {u.latest_version}")
                    else:
                        log_fn(up_to_date_msg)
                    if missing:
                        log_fn(f"Nexus: {len(missing)} mod(s) have missing requirements!")
                        for m in missing:
                            names = ", ".join(r.mod_name for r in m.missing[:3])
                            suffix = f" (+{len(m.missing) - 3} more)" if len(m.missing) > 3 else ""
                            log_fn(f"  ⚠ {m.mod_name}: needs {names}{suffix}")
                    elif log_clean_reqs:
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

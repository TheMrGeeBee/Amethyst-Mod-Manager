"""Wizard to install Tale of Two Wastelands on Fallout New Vegas via the native
Linux MPI installer (https://github.com/SulfurNitride/TTW_Linux_Installer).

The installer is a native Linux binary (no Proton). Flow: download the binary →
confirm FNV/FO3 paths + pick the .mpi → restore the game to vanilla → run the
installer → register the output as the 'Tale of Two Wastelands' mod, set up its
profile INIs + FalloutCustom.ini, and seed its recommended Nexus requirements.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from Utils.xdg import open_url
from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD,
)

_GITHUB_API_URL = (
    "https://api.github.com/repos/SulfurNitride/TTW_Linux_Installer/releases/latest"
)
# Project page for the native Linux installer — linked in Step 1 for credit.
_GITHUB_REPO_URL = "https://github.com/SulfurNitride/TTW_Linux_Installer"
_EXE_NAME    = "mpi_installer"
_APP_DIR     = "TTW"
_OUTPUT_NAME = "Tale of Two Wastelands"
_MODPUB_URL  = "https://mod.pub/ttw/133/files"

# Nexus (newvegas) mod ids the TTW setup recommends/requires. Seeded into the
# TTW mod's meta.ini missing_requirements so they surface through the standard
# "missing requirements" flag (red marker → install panel). Names are left blank
# here and resolved live by the missing-reqs panel via each mod's own page.
_TTW_REQUIRED_MOD_IDS = [
    57174, 68714, 82540, 70801, 65906, 77415, 58277, 66927,
    72541, 66537, 66347, 80993, 71973, 84823, 80666, 71336,
]

# Fallout 3 Steam app ids (vanilla + GOTY) used to auto-locate the FO3 install.
_FO3_STEAM_IDS = ("22300", "22370")
_FO3_EXE_NAME  = "Fallout3.exe"

# Vanilla master + DLC plugins TTW xdelta-patches. These must exist (pristine)
# in each game's Data/ folder or the install fails near the end with
# "Source file not found". The FNV list is read from the game object at runtime;
# the FO3 list is fixed (the wizard runs under FNV, so there's no FO3 game object
# to query).
_FO3_REQUIRED_ESMS = [
    "Fallout3.esm",
    "Anchorage.esm", "ThePitt.esm", "BrokenSteel.esm",
    "PointLookout.esm", "Zeta.esm",
]

_OK    = "#6bc76b"
_ERR   = "#e06c6c"
_DONE  = "#2d7a2d"


def _get_applications_dir(game: "BaseGame") -> Path:
    return game.get_mod_staging_path().parent / "Applications" / _APP_DIR


def find_ttw_installer(game: "BaseGame") -> Path | None:
    p = _get_applications_dir(game) / _EXE_NAME
    return p if p.is_file() else None


def _find_fo3_install() -> Path | None:
    """Locate the Fallout 3 install folder via Steam libraries, or None."""
    try:
        from Utils.steam_finder import find_game_by_steam_id, find_steam_libraries
        libs = find_steam_libraries()
        for sid in _FO3_STEAM_IDS:
            hit = find_game_by_steam_id(libs, sid, _FO3_EXE_NAME)
            if hit is not None:
                return hit
    except Exception:
        pass
    return None


def _missing_vanilla_esms(game_root: Path, esms: "list[str]") -> list[str]:
    """Return the *esms* not present in ``<game_root>/Data`` (case-insensitive)."""
    data = game_root / "Data"
    present: set[str] = set()
    try:
        present = {p.name.lower() for p in data.iterdir() if p.is_file()}
    except OSError:
        # Data dir unreadable/absent → everything is missing.
        return list(esms)
    return [e for e in esms if e.lower() not in present]


def _fnv_required_esms(game: "BaseGame") -> list[str]:
    """Vanilla master + DLC .esm files TTW patches (from the game's plugin lists)."""
    plugins = list(getattr(game, "vanilla_plugins", []) or []) + \
        list(getattr(game, "vanilla_dlc_plugins", []) or [])
    return [p for p in plugins if p.lower().endswith(".esm")]


class TTWInstallerWizard(ctk.CTkFrame):
    """Step-by-step wizard to install Tale of Two Wastelands via the native
    Linux MPI installer and register the result as a managed mod."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
        **_kwargs,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game        = game
        self._log         = log_fn or (lambda msg: None)
        self._exe         = find_ttw_installer(game)

        self._mpi_path: Path | None = None
        self._fo3_path: Path | None = _find_fo3_install()
        self._fnv_path: Path | None = game.get_game_path()
        self._force_rebuild: bool = False

        # Bind staging/modlist resolution to the selected profile up front, so
        # "already installed" detection and seeding target the right profile
        # (staging can be per-profile).
        self._sync_active_profile()

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text=f"Install Tale of Two Wastelands — {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_close_cb,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_download()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _safe_after(self, ms: int, fn):
        try:
            if self.winfo_exists():
                self.after(ms, fn)
        except Exception:
            pass

    def _set_label(self, attr: str, text: str, color: str = TEXT_DIM):
        def _apply(t=text, c=color):
            try:
                widget = getattr(self, attr, None)
                if widget is not None and widget.winfo_exists():
                    widget.configure(text=t, text_color=c)
            except Exception:
                pass
        self.after(0, _apply)

    def _append_output(self, text: str) -> None:
        def _apply():
            try:
                box = getattr(self, "_run_output", None)
                if box is None or not box.winfo_exists():
                    return
                box.configure(state="normal")
                box.insert("end", text + "\n")
                box.configure(state="disabled")
                box.see("end")
            except Exception:
                pass
        self.after(0, _apply)

    def _on_done(self):
        try:
            topbar = self.winfo_toplevel()._topbar
        except Exception:
            topbar = None
        self._on_close_cb()
        if topbar is not None:
            try:
                topbar.after(0, topbar._reload_mod_panel)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Step 1 — Download + extract the installer (skipped if already present)
    # ------------------------------------------------------------------

    def _show_step_download(self):
        # If TTW is already built, jump straight to the already-installed choice —
        # the setup-only path needs neither the installer binary nor a rebuild.
        if not getattr(self, "_force_rebuild", False) and self._ttw_mod_dir() is not None:
            self._show_step_already_installed()
            return

        if find_ttw_installer(self._game) is not None:
            self._show_step_paths()
            return

        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Install the TTW MPI Installer",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "The native Linux TTW installer will be downloaded from GitHub\n"
                "and placed in this game's Applications folder.\n\n"
                "Click Install to begin."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 8))

        # Credit: the native Linux installer is a third-party project.
        ctk.CTkLabel(
            self._body,
            text="Installer by SulfurNitride (TTW_Linux_Installer)",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 4))
        ctk.CTkButton(
            self._body, text="View on GitHub", width=200, height=30,
            font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=lambda: open_url(_GITHUB_REPO_URL),
        ).pack(pady=(0, 16))

        self._download_status = ctk.CTkLabel(
            self._body, text="",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._download_status.pack(pady=(0, 8))

        self._install_btn = ctk.CTkButton(
            self._body, text="Install", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._start_install,
        )
        self._install_btn.pack(side="bottom")

    def _start_install(self):
        try:
            self._install_btn.configure(state="disabled")
        except Exception:
            pass
        self._set_label("_download_status", "Contacting GitHub…")
        threading.Thread(target=self._do_install, daemon=True).start()

    def _do_install(self):
        import tempfile

        from wizards.script_extender import _extract_archive

        try:
            req = urllib.request.Request(
                _GITHUB_API_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "ModManager/1.0",
                },
            )
            from Utils.ca_bundle import get_ssl_context
            with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
                data = json.loads(resp.read().decode())

            tag = data.get("tag_name", "unknown")
            url = None
            for asset in data.get("assets", []):
                name = asset.get("name", "").lower()
                if "linux" in name and name.endswith((".zip", ".tar.gz")):
                    url = asset["browser_download_url"]
                    break
            if not url:
                raise RuntimeError(
                    f"No Linux installer asset found in the latest TTW release ({tag})."
                )

            self._log(f"TTW Wizard: downloading TTW installer {tag} from {url}")
            self._set_label("_download_status", f"Downloading TTW installer {tag}…")

            tmp_dir = Path(tempfile.mkdtemp())
            archive = tmp_dir / Path(url).name
            try:
                from Utils.ca_bundle import download_file
                download_file(url, archive)

                dest = _get_applications_dir(self._game)
                dest.mkdir(parents=True, exist_ok=True)

                self._set_label("_download_status", "Extracting installer…")
                self._log(f"TTW Wizard: extracting {archive.name} → {dest}")
                paths = _extract_archive(archive, dest)
                file_count = len([p for p in paths if p.is_file()])
                self._log(f"TTW Wizard: extracted {file_count} file(s).")
            finally:
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)

            exe = dest / _EXE_NAME
            if not exe.is_file():
                raise RuntimeError(
                    f"{_EXE_NAME} not found after extraction at {dest}."
                )
            # Defensive: ensure the binary is executable even if the archive lost
            # the bit (some extractors don't preserve unix modes).
            try:
                import os
                os.chmod(exe, 0o755)
            except OSError:
                pass
            self._exe = exe

            self._set_label("_download_status", "Installer ready.", color=_OK)
            self._safe_after(400, self._show_step_paths)

        except Exception as exc:
            self._set_label("_download_status", f"Install error: {exc}", color=_ERR)
            self._log(f"TTW Wizard: install error: {exc}")
            def _reenable():
                try:
                    self._install_btn.configure(state="normal")
                except Exception:
                    pass
            self._safe_after(0, _reenable)

    # ------------------------------------------------------------------
    # Step 2 — Confirm game paths + pick the MPI package
    # ------------------------------------------------------------------

    def _ttw_mod_dir(self) -> "Path | None":
        """Path to the already-installed TTW mod in staging, or None."""
        try:
            staging = self._game.get_effective_mod_staging_path()
        except Exception:
            staging = None
        if staging is None:
            return None
        mod_dir = staging / _OUTPUT_NAME
        # Treat it as installed only if the key merged plugin is present, so a
        # stray empty folder doesn't trip the skip path.
        if (mod_dir / "TaleOfTwoWastelands.esm").is_file():
            return mod_dir
        return None

    def _show_step_paths(self):
        self._clear_body()

        # If TTW is already built (mod present in staging) and the user hasn't
        # chosen to rebuild, offer to skip the ~18 GB build.
        if not getattr(self, "_force_rebuild", False) and self._ttw_mod_dir() is not None:
            self._show_step_already_installed()
            return

        self._build_paths_form()

    def _build_paths_form(self):
        ctk.CTkLabel(
            self._body, text="Step 2: Game folders & TTW package",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            self._body,
            text=(
                "TTW merges assets from both Fallout 3 and Fallout New Vegas, so "
                "both games must be installed. Confirm the folders below, then "
                "select the TTW .mpi package.\n\n"
                "Get the latest TTW .mpi from mod.pub (free account required) — "
                "extract the download and the .mpi is inside."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=380,
        ).pack(pady=(0, 8))

        ctk.CTkButton(
            self._body, text="Open mod.pub TTW page", width=220, height=32,
            font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=lambda: open_url(_MODPUB_URL),
        ).pack(pady=(0, 12))

        # Fallout New Vegas path row
        self._fnv_label = self._path_row(
            "Fallout New Vegas:", self._fnv_path,
            lambda: self._browse_folder("Select the Fallout New Vegas folder", "_fnv"),
        )
        # Fallout 3 path row
        self._fo3_label = self._path_row(
            "Fallout 3:", self._fo3_path,
            lambda: self._browse_folder("Select the Fallout 3 folder", "_fo3"),
        )
        # MPI package row
        self._mpi_label = self._path_row(
            "TTW .mpi package:", self._mpi_path,
            self._browse_mpi, browse_text="Choose .mpi…",
        )

        self._next_btn = ctk.CTkButton(
            self._body, text="Continue", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._validate_and_run,
        )
        self._next_btn.pack(side="bottom", pady=(8, 0))

        self._paths_status = ctk.CTkLabel(
            self._body, text="",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=380,
        )
        self._paths_status.pack(side="bottom", pady=(8, 4))

    # ------------------------------------------------------------------
    # "Already installed" branch — skip the rebuild
    # ------------------------------------------------------------------

    def _show_step_already_installed(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Tale of Two Wastelands is already installed",
            font=FONT_BOLD, text_color=TEXT_MAIN, wraplength=400, justify="center",
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                f"The '{_OUTPUT_NAME}' mod is already in your mod list, so the "
                "~18 GB build can be skipped.\n\n"
                "• Re-apply setup only — re-runs the profile INI + FalloutCustom.ini "
                "setup without rebuilding (fast).\n\n"
                "• Rebuild from scratch — restores to vanilla and runs the full "
                "installer again (needs the .mpi + both games)."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="left", wraplength=400,
        ).pack(pady=(0, 16))

        ctk.CTkButton(
            self._body, text="Re-apply setup only", width=240, height=40,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._show_step_setup_only,
        ).pack(pady=(0, 8))

        ctk.CTkButton(
            self._body, text="Rebuild from scratch", width=240, height=36,
            font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._show_step_paths_force,
        ).pack(pady=(0, 4))

    def _show_step_paths_force(self):
        """Rebuild despite TTW being installed: route through download (fetches
        the binary if missing); _force_rebuild skips the already-installed jump."""
        self._force_rebuild = True
        self._show_step_download()

    def _show_step_setup_only(self):
        """Run only the post-install setup (no rebuild) and show completion."""
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Re-applying TTW setup",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._run_status = ctk.CTkLabel(
            self._body, text="Working…",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=480,
        )
        self._run_status.pack(pady=(0, 6))

        self._run_output = ctk.CTkTextbox(
            self._body, height=160, font=("Courier New", 11),
            fg_color=BG_PANEL, text_color=TEXT_MAIN, state="disabled",
        )
        self._run_output.pack(fill="both", expand=True, pady=(4, 10))

        self._done_btn = ctk.CTkButton(
            self._body, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color=_DONE, hover_color="#3a9e3a", text_color="white",
            command=self._on_done, state="disabled",
        )
        self._done_btn.pack(side="bottom")

        threading.Thread(
            target=lambda: self._run_post_install_setup(rebuilt=False),
            daemon=True,
        ).start()

    def _path_row(self, label: str, value: Path | None, browse_cmd, browse_text="Browse…"):
        # Stacked: label + browse button on one row, path on a full-width line
        # below — keeps the button visible at the fixed ~440px window width.
        row = ctk.CTkFrame(self._body, fg_color=BG_PANEL, corner_radius=6)
        row.pack(fill="x", pady=4, ipady=4)

        header = ctk.CTkFrame(row, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(4, 2))
        ctk.CTkLabel(
            header, text=label, font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left")
        ctk.CTkButton(
            header, text=browse_text, width=110, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=browse_cmd,
        ).pack(side="right")

        val_label = ctk.CTkLabel(
            row, text=str(value) if value else "— not set —",
            font=FONT_NORMAL, text_color=(TEXT_DIM if value else _ERR),
            anchor="w", justify="left", wraplength=380,
        )
        val_label.pack(fill="x", padx=8, pady=(0, 2))
        return val_label

    def _browse_folder(self, title: str, attr: str):
        from Utils.portal_filechooser import pick_folder

        def _on_picked(p: Path | None):
            if p is None:
                return
            setattr(self, f"{attr}_path", p)
            label = getattr(self, f"{attr}_label", None)
            if label is not None and label.winfo_exists():
                label.configure(text=str(p), text_color=TEXT_DIM)

        pick_folder(title, lambda p: self.after(0, lambda: _on_picked(p)))

    def _browse_mpi(self):
        from Utils.portal_filechooser import pick_file

        def _on_picked(p: Path | None):
            if p is None:
                return
            self._mpi_path = p
            if self._mpi_label.winfo_exists():
                self._mpi_label.configure(text=str(p), text_color=TEXT_DIM)

        pick_file(
            "Select the TTW .mpi package",
            lambda p: self.after(0, lambda: _on_picked(p)),
            filters=[
                ("TTW Package (*.mpi)", ["*.mpi"]),
                ("All files", ["*"]),
            ],
        )

    def _validate_and_run(self):
        if self._mpi_path is None or not self._mpi_path.is_file():
            self._set_label("_paths_status", "Please select the TTW .mpi package.", color=_ERR)
            return
        if self._fnv_path is None or not self._fnv_path.is_dir():
            self._set_label("_paths_status", "Fallout New Vegas folder is not set.", color=_ERR)
            return
        if self._fo3_path is None or not self._fo3_path.is_dir():
            self._set_label(
                "_paths_status",
                "Fallout 3 folder is not set. TTW requires Fallout 3 to be installed.",
                color=_ERR,
            )
            return
        # The vanilla-esm pre-flight runs in _do_run, *after* the restore step —
        # if mods are deployed, the vanilla masters live in Data_Core/ until the
        # restore moves them back, so checking here would give a false failure.
        self._show_step_run()

    # ------------------------------------------------------------------
    # Step 3 — Run the installer + register output as a mod
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Building Tale of Two Wastelands",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "The game is first restored to a vanilla state, then the installer\n"
                "merges Fallout 3 and Fallout New Vegas assets. This produces ~18 GB\n"
                "of output and can take a long while — please leave it running.\n"
                f"Output is written directly into your mod list as the '{_OUTPUT_NAME}' mod."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=480,
        ).pack(pady=(0, 12))

        self._run_status = ctk.CTkLabel(
            self._body, text="Starting…",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=480,
        )
        self._run_status.pack(pady=(0, 6))

        # Scrolling log box showing the installer's live output.
        self._run_output = ctk.CTkTextbox(
            self._body, height=160, font=("Courier New", 11),
            fg_color=BG_PANEL, text_color=TEXT_MAIN, state="disabled",
        )
        self._run_output.pack(fill="both", expand=True, pady=(4, 10))

        self._done_btn = ctk.CTkButton(
            self._body, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color=_DONE, hover_color="#3a9e3a", text_color="white",
            command=self._on_done, state="disabled",
        )
        self._done_btn.pack(side="bottom")

        threading.Thread(target=self._do_run, daemon=True).start()

    def _current_profile(self) -> str:
        """The profile currently selected in the top bar."""
        try:
            return self.winfo_toplevel()._topbar._profile_var.get()
        except Exception:
            return self._game.get_last_deployed_profile() or "default"

    def _sync_active_profile(self) -> None:
        """Point the game's active profile dir at the selected profile so
        staging/modlist paths resolve correctly (staging can be per-profile)."""
        try:
            self._game.set_active_profile_dir(
                self._game.get_profile_root() / "profiles" / self._current_profile()
            )
            self._game.load_paths()
        except Exception:
            pass

    def _restore_to_vanilla(self) -> bool:
        """Restore the game to vanilla, mirroring the top-bar Restore button
        (top_bar._on_restore) exactly. Keep in sync with it — diverging from that
        flow previously deleted vanilla files. Returns True on success."""
        game = self._game
        if not hasattr(game, "restore"):
            return False

        def _rlog(m: str) -> None:
            self._log(f"TTW Wizard: {m}")
            self._append_output(m)

        current_profile = self._current_profile()
        success = True
        try:
            from Utils.deploy_pipeline import check_paths_mounted
            mount_err = check_paths_mounted(game)
            if mount_err:
                _rlog(f"Restore aborted: {mount_err}")
                return False

            last_deployed = game.get_last_deployed_profile()
            if last_deployed:
                game.set_active_profile_dir(
                    game.get_profile_root() / "profiles" / last_deployed
                )
                # Re-resolve paths for the last-deployed profile in case it
                # overrides game_path/prefix (per-profile paths).
                game.load_paths()
            game_root = game.get_game_path()
            if game_root is not None:
                # Keep the post-restore esm check + installer on the same root.
                self._fnv_path = game_root

            game.restore(log_fn=_rlog)

            from Utils.deploy import restore_root_folder
            root_folder_dir = game.get_effective_root_folder_path()
            if root_folder_dir.is_dir() and game_root:
                restore_root_folder(root_folder_dir, game_root, log_fn=_rlog)
        except Exception as exc:
            success = False
            self._log(f"TTW Wizard: restore error: {exc}")
            self._append_output(f"Restore error: {exc}")
        finally:
            # Always put the active profile back to the currently-selected one
            # (mirrors the Restore button's finally block).
            try:
                game.set_active_profile_dir(
                    game.get_profile_root() / "profiles" / current_profile
                )
                game.load_paths()
            except Exception:
                pass
            if success:
                try:
                    game.clear_deploy_active()
                except Exception:
                    pass
        return success

    def _do_run(self):
        exe = self._exe
        if exe is None or not exe.is_file():
            self._set_label(
                "_run_status",
                "Installer binary is missing. Restart the wizard and let it install first.",
                color=_ERR,
            )
            return

        # Restore to vanilla first so TTW sees clean Data/. Runs the same flow as
        # the Restore button and leaves the active profile on the selected one.
        self._set_label("_run_status", "Restoring game to vanilla…", color=TEXT_DIM)
        self._append_output("Restoring game to a vanilla state before install…")
        if not self._restore_to_vanilla():
            self._set_label(
                "_run_status",
                "Restore failed — see the log. Fix the issue (or restore manually "
                "via the Restore button) and retry.",
                color=_ERR,
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            return

        # Resolve staging AFTER restore so it reflects the selected profile (the
        # restore step leaves the active profile context on _current_profile()).
        staging = self._game.get_effective_mod_staging_path()
        if staging is None:
            self._set_label("_run_status", "Mod staging path is not configured.", color=_ERR)
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            return
        dest = staging / _OUTPUT_NAME

        # Pre-flight (post-restore): the vanilla masters/DLC esms must be present
        # in Data/, or the build fails near the end with "Source file not found".
        fnv_missing = _missing_vanilla_esms(self._fnv_path, _fnv_required_esms(self._game))
        fo3_missing = _missing_vanilla_esms(self._fo3_path, _FO3_REQUIRED_ESMS)
        if fnv_missing or fo3_missing:
            parts: list[str] = []
            if fnv_missing:
                parts.append("Fallout New Vegas: " + ", ".join(fnv_missing))
            if fo3_missing:
                parts.append("Fallout 3: " + ", ".join(fo3_missing))
            detail = "\n".join(parts)
            self._log(f"TTW Wizard: missing vanilla esms after restore — {detail}")
            self._append_output("ERROR: missing vanilla plugin files:\n" + detail)
            self._set_label(
                "_run_status",
                "Missing vanilla plugin files even after restoring to vanilla — "
                "these were never backed up.\nIn Steam, right-click each game → "
                "Properties → Installed Files → Verify integrity of game files, "
                "then retry.\n\n" + detail,
                color=_ERR,
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            return

        cmd = [
            str(exe), "install",
            "--mpi", str(self._mpi_path),
            "--fo3", str(self._fo3_path),
            "--fnv", str(self._fnv_path),
            "--dest", str(dest),
        ]
        self._log("TTW Wizard: running " + " ".join(cmd))

        self._set_label("_run_status", "Installing… (see log below)", color=TEXT_DIM)
        # The installer has no percentage; surface its phase lines in the status.
        activity_re = re.compile(r"\b(Building ready BSA|Extracting|Patching|Cleaning up)[^\r\n]*")

        try:
            dest.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                cmd,
                cwd=str(exe.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color=_ERR)
            self._log(f"TTW Wizard: launch error: {exc}")
            return

        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip("\n")
                if not line:
                    continue
                self._log(f"TTW: {line}")
                self._append_output(line)
                m = activity_re.search(line)
                if m:
                    self._set_label("_run_status", m.group(0).strip() + "…", color=TEXT_DIM)
        except Exception as exc:
            self._log(f"TTW Wizard: error reading installer output: {exc}")

        rc = proc.wait()
        if rc != 0:
            self._set_label(
                "_run_status",
                f"Installer exited with error (code {rc}). See the log for details.",
                color=_ERR,
            )
            self._log(f"TTW Wizard: installer exited with code {rc}.")
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            return

        self._set_label("_run_status", "Install complete — registering mod…", color=_OK)
        self._log("TTW Wizard: install complete.")
        self._register_output(dest)

    def _register_output(self, dest: Path):
        from wizards._install_as_mod import index_installed_mod, register_as_mod

        try:
            # The installer writes a Data/-rooted folder, so this is a normal
            # Data-relative mod (not a rootFolder mod).
            register_as_mod(
                self._game,
                _OUTPUT_NAME,
                archive=None,
                parent_widget=self,
                log_fn=self._log,
                root_folder=False,
            )
            index_installed_mod(self._game, _OUTPUT_NAME, log_fn=self._log)
        except Exception as exc:
            self._set_label(
                "_run_status",
                f"Install finished but registering the mod failed: {exc}",
                color=_ERR,
            )
            self._log(f"TTW Wizard: register error: {exc}")
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            return

        self._run_post_install_setup(rebuilt=True)

    def _run_post_install_setup(self, *, rebuilt: bool) -> None:
        """Post-build steps (no rebuild): profile INIs + FalloutCustom.ini setup,
        seed recommended mods, then the completion message. Shared by a fresh
        build and the 'already installed' re-apply path."""
        # Bind to the selected profile so staging/INI/modlist paths resolve there.
        self._sync_active_profile()
        profile = self._current_profile()

        setup = getattr(self._game, "setup_ttw_custom_ini", None)
        if callable(setup):
            def _ini_log(m: str) -> None:
                self._log(f"TTW Wizard: {m}")
                self._append_output(m)
            try:
                self._append_output("Setting up profile INIs + FalloutCustom.ini for TTW…")
                setup(profile, log_fn=_ini_log)
            except Exception as exc:
                self._log(f"TTW Wizard: FalloutCustom.ini setup failed: {exc}")
                self._append_output(f"FalloutCustom.ini setup failed: {exc}")

        # Seed recommended/required Nexus mods into the TTW mod's meta.ini so
        # they surface through the standard "missing requirements" flag.
        try:
            self._seed_required_mods()
        except Exception as exc:
            self._log(f"TTW Wizard: seeding requirements failed: {exc}")
            self._append_output(f"Seeding requirements failed: {exc}")

        if rebuilt:
            done_msg = (
                f"Done! '{_OUTPUT_NAME}' was added to your mod list. Enable it and deploy."
            )
        else:
            done_msg = (
                f"Setup re-applied for the existing '{_OUTPUT_NAME}' mod. "
                "Enable it and deploy."
            )
        self._set_label(
            "_run_status",
            done_msg + "\n\n"
            "TTW needs several supporting mods (script extender plugins, patches, "
            "etc.). These are flagged on the TTW mod via the red 'missing "
            "requirements' marker — click it to install them, then deploy.",
            color=_OK,
        )
        self.after(0, lambda: self._done_btn.configure(state="normal"))

    def _seed_required_mods(self) -> None:
        """Write the full recommended-mod id list into the TTW mod's meta.ini
        ``missing_requirements``. The marker/panel filter it live against
        installed mods, so satisfied entries hide and reappear on removal."""
        from Nexus.nexus_meta import read_meta, write_meta

        mod_dir = self._ttw_mod_dir()
        if mod_dir is None:
            return
        meta_path = mod_dir / "meta.ini"
        if not meta_path.is_file():
            self._log("TTW Wizard: TTW meta.ini not found — skipping requirement seeding.")
            return

        meta = read_meta(meta_path)
        meta.missing_requirements = ";".join(f"{mid}:" for mid in _TTW_REQUIRED_MOD_IDS)
        write_meta(meta_path, meta)

        self._log(
            f"TTW Wizard: seeded {len(_TTW_REQUIRED_MOD_IDS)} recommended mod(s) "
            "into the TTW requirements list."
        )
        self._append_output(
            "Recommended Nexus mods are flagged on the TTW mod via the "
            "'missing requirements' marker (installed ones are hidden "
            "automatically)."
        )


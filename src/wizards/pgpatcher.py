"""
pgpatcher.py
Wizard for running PGPatcher with Skyrim Special Edition.

Workflow
--------
1. Auto-download the latest PGPatcher release from GitHub and extract to
   Profiles/<game>/Applications/PGPatcher/.
2. User picks the Proton version; PGPatcher gets its own isolated prefix
   (prefix_<ProtonName>/ next to the exe), independent of the game's Proton.
3. Install d3dcompiler_47 and .NET 8 into that prefix (skipped if already done).
4. Apply PGPatcher's config (bootstrap cfg/settings.json via exe_args_builder).
5. Prompt the user to delete any previous PGPatcher output, then deploy the modlist.
6. Run PGPatcher.exe via Proton. Before launch the game's Installed Path is
   seeded into the prefix registry (Skyrim only writes it to the game prefix on
   first launch) and the profile's plugins.txt is symlinked into the prefix
   AppData so PGPatcher can read the load order.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD,
)

from Utils.protontricks import (
    D3D_DEP_KEY as _D3D_DEP_KEY,
    dotnet_dep_key,
    is_dep_installed as _is_dep_installed,
    mark_dep_installed as _mark_dep_installed,
)

_GITHUB_API     = "https://api.github.com/repos/hakasapl/PGPatcher/releases/latest"
_PATCHER_EXE    = "PGPatcher.exe"
_PATCHER_DIR    = "PGPatcher"
_NET8_URL       = "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/8.0.25/windowsdesktop-runtime-8.0.25-win-x64.exe"
_NET8_FILENAME  = "windowsdesktop-runtime-8.0.25-win-x64.exe"
_NET8_DEP_KEY   = dotnet_dep_key("8")


def _get_applications_dir(game: "BaseGame") -> Path:
    return game.get_mod_staging_path().parent / "Applications" / _PATCHER_DIR


def _patcher_exe_path(game: "BaseGame") -> Path | None:
    p = _get_applications_dir(game) / _PATCHER_EXE
    return p if p.is_file() else None


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------

def _flatten_subdirs(dest: Path) -> None:
    """Repeatedly collapse single-subdirectory wrappers inside *dest* until
    the contents are at the top level.

    Ignores loose files at the top level when deciding whether to flatten,
    so an archive like:
      dest/some_readme.ini
      dest/PGPatcher/PGPatcher.exe
    still gets flattened to:
      dest/some_readme.ini
      dest/PGPatcher.exe
    """
    import shutil
    while True:
        all_entries = [e for e in dest.iterdir() if e.name != "__MACOSX"]
        subdirs = [e for e in all_entries if e.is_dir()]
        if len(subdirs) == 1 and not (dest / _PATCHER_EXE).is_file():
            wrapper = subdirs[0]
            tmp = dest.parent / (dest.name + "_flatten_tmp")
            wrapper.rename(tmp)
            for item in tmp.iterdir():
                shutil.move(str(item), str(dest / item.name))
            tmp.rmdir()
        else:
            break


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

from wizards._proton_prefix import ProtonPrefixStepMixin, shutdown_prefix_wineserver


class PGPatcherWizard(ProtonPrefixStepMixin, ctk.CTkFrame):
    """Step-by-step wizard to set up and run PGPatcher for Skyrim SE."""

    _tool_exe_name      = _PATCHER_EXE
    _tool_display_name  = "PGPatcher"
    _proton_step_title  = "Step 2: Choose Proton Version"
    _exe_missing_text   = (
        f"{_PATCHER_EXE} was not found.\n"
        "Please restart the wizard and download PGPatcher first."
    )

    def _proton_next_step(self):
        self._show_step_deps()

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
        self._exe         = _patcher_exe_path(game)
        self._proton_name = ""
        # Per-mod conflict resolution via a dummy MO2 instance (opt-in).
        self._mo2_dummy_dir: "Path | None" = None
        self._mo2_game_type: int = 0
        self._use_mo2_parity = False

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text=f"Run PGPatcher \u2014 {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_close_cb,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_download()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _on_done(self):
        """Close the wizard and refresh the modlist panel."""
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

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _set_label(self, attr: str, text: str, color: str = TEXT_DIM):
        def _apply(t=text, c=color):
            try:
                widget = getattr(self, attr, None)
                if widget is not None and widget.winfo_exists():
                    widget.configure(text=t, text_color=c)
            except Exception:
                pass
        self.after(0, _apply)

    # ------------------------------------------------------------------
    # Step 1 — Auto-download PGPatcher from GitHub (skipped if already extracted)
    # ------------------------------------------------------------------

    def _show_step_download(self):
        if self._exe is not None:
            self._show_step_proton()
            return

        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download PGPatcher",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._dl_status = ctk.CTkLabel(
            self._body, text="Fetching latest release from GitHub\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._dl_status.pack(pady=(0, 12))

        threading.Thread(target=self._do_auto_download, daemon=True).start()

    def _do_auto_download(self):
        from wizards.script_extender import _fetch_latest_github_asset, _extract_archive

        try:
            self._set_label("_dl_status", "Fetching latest release from GitHub\u2026")
            tag, dl_url = _fetch_latest_github_asset(_GITHUB_API, ["pgpatcher"])

            self._set_label("_dl_status", f"Downloading {tag}\u2026")
            self._log(f"PGPatcher Wizard: downloading {tag} from {dl_url}")

            suffix = Path(dl_url).suffix or ".zip"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = Path(tmp.name)

            from Utils.ca_bundle import download_file
            download_file(dl_url, tmp_path)

            dest = _get_applications_dir(self._game)
            dest.mkdir(parents=True, exist_ok=True)

            self._set_label("_dl_status", "Extracting\u2026")
            self._log("PGPatcher Wizard: download complete, extracting\u2026")

            paths = _extract_archive(tmp_path, dest)
            tmp_path.unlink(missing_ok=True)

            file_count = len([p for p in paths if p.is_file()])
            _flatten_subdirs(dest)

            exe = dest / _PATCHER_EXE
            if not exe.is_file():
                raise RuntimeError(f"{_PATCHER_EXE} not found after extraction.")
            self._exe = exe

            self._log(f"PGPatcher Wizard: extracted {file_count} file(s).")
            self._set_label(
                "_dl_status",
                f"Downloaded and extracted {tag}.",
                color="#6bc76b",
            )
            self.after(500, self._show_step_proton)

        except Exception as exc:
            self._set_label("_dl_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"PGPatcher Wizard: download error: {exc}")

    # ------------------------------------------------------------------
    # Step 3 — Install d3dcompiler_47 and .NET 8
    # ------------------------------------------------------------------

    def _show_step_deps(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Install Dependencies",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._d3d_status = ctk.CTkLabel(
            self._body, text="Checking d3dcompiler_47\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._d3d_status.pack(pady=(0, 6))

        self._net8_status = ctk.CTkLabel(
            self._body, text="",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._net8_status.pack(pady=(0, 6))

        threading.Thread(target=self._do_install_deps, daemon=True).start()

    def _do_install_deps(self):
        import urllib.request
        from Utils.config_paths import get_dotnet_cache_dir
        from Utils.protontricks import install_d3dcompiler_47

        self._set_label("_d3d_status", "Preparing PGPatcher's Wine prefix…")
        proton_script, env, compat_data = self._get_tool_env()

        if proton_script is None:
            self._set_label(
                "_d3d_status",
                f"Could not find Proton '{self._proton_name}' — "
                "check that it is installed in Steam, then reopen this wizard.",
                color="#e06c6c",
            )
            return

        prefix_path = compat_data / "pfx"

        # --- d3dcompiler_47 ---
        if _is_dep_installed(prefix_path, _D3D_DEP_KEY):
            self._set_label("_d3d_status", "d3dcompiler_47 already installed — skipping.", color="#6bc76b")
        else:
            self._set_label("_d3d_status", "Installing d3dcompiler_47\u2026 (may take a minute)")
            # steam_id deliberately omitted: the protontricks fallback installs
            # by app id into the game prefix, not this tool prefix.
            ok = install_d3dcompiler_47(
                "",
                log_fn=lambda msg: self._log(f"PGPatcher Wizard: {msg}"),
                prefix_path=prefix_path,
            )
            if ok:
                self._set_label("_d3d_status", "d3dcompiler_47 installed.", color="#6bc76b")
            else:
                self._set_label("_d3d_status", "d3dcompiler_47 install failed — continuing anyway.", color="#e0a83c")

        # --- .NET 8 ---
        self._set_label("_net8_status", "Checking .NET 8\u2026")

        if _is_dep_installed(prefix_path, _NET8_DEP_KEY):
            self._set_label("_net8_status", ".NET 8 already installed — skipping.", color="#6bc76b")
            self.after(500, self._show_step_config)
            return

        cache_path = get_dotnet_cache_dir() / _NET8_FILENAME

        try:
            if not cache_path.is_file():
                self._set_label("_net8_status", "Downloading .NET 8 runtime\u2026")
                self._log("PGPatcher Wizard: downloading .NET 8 runtime \u2026")
                from Utils.ca_bundle import download_file
                download_file(_NET8_URL, cache_path)
                self._log("PGPatcher Wizard: .NET 8 download complete.")
            else:
                self._log("PGPatcher Wizard: using cached .NET 8 installer.")

            self._set_label(
                "_net8_status",
                "Installing .NET 8 into PGPatcher's prefix\u2026\n(this may take a few minutes)",
            )
            self._log("PGPatcher Wizard: launching .NET 8 installer in PGPatcher's prefix \u2026")

            proc = subprocess.run(
                ["python3", str(proton_script), "run", str(cache_path), "/quiet", "/norestart"],
                env=env,
                cwd=str(cache_path.parent),
            )

            # Exit codes from the .NET desktop runtime installer:
            #   0    = installed successfully
            #   102  = already installed / no-op
            #   1638 = another version already installed
            #   3010 = installed, reboot required
            _ok_codes = {0, 102, 1638, 3010}
            if proc.returncode not in _ok_codes:
                raise RuntimeError(f".NET 8 installer exited with code {proc.returncode}.")

            _mark_dep_installed(prefix_path, _NET8_DEP_KEY)
            self._set_label("_net8_status", ".NET 8 installed successfully.", color="#6bc76b")
            self.after(500, self._show_step_config)

        except Exception as exc:
            self._set_label("_net8_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"PGPatcher Wizard: .NET 8 install error: {exc}")

    # ------------------------------------------------------------------
    # Step 4 — Apply PGPatcher config
    # ------------------------------------------------------------------

    def _show_step_config(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Apply PGPatcher Config",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._config_status = ctk.CTkLabel(
            self._body, text="Applying config\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._config_status.pack(pady=(0, 12))

        threading.Thread(target=self._do_apply_config, daemon=True).start()

    def _do_apply_config(self):
        exe = _patcher_exe_path(self._game)
        if exe is None:
            self._set_label(
                "_config_status",
                f"{_PATCHER_EXE} not found — please restart the wizard.",
                color="#e06c6c",
            )
            return

        game_path   = self._game.get_game_path()
        staging     = self._game.get_effective_mod_staging_path()

        try:
            from Utils.exe_args_builder import _bootstrap_pgpatcher_settings
            _bootstrap_pgpatcher_settings(
                exe,
                game_path,
                staging,
                log_fn=lambda msg: self._log(f"PGPatcher Wizard: {msg}"),
                update=True,
            )
            self._set_label("_config_status", "Config applied.", color="#6bc76b")
            self.after(500, self._show_step_deploy)
        except Exception as exc:
            self._set_label("_config_status", f"Config error: {exc}", color="#e06c6c")
            self._log(f"PGPatcher Wizard: config error: {exc}")

    # ------------------------------------------------------------------
    # Step 5 — Delete previous output, then deploy
    # ------------------------------------------------------------------

    def _show_step_deploy(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 5: Deploy Modlist",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "Before deploying, please delete any output from a previous\n"
                "PGPatcher run (the 'PGPatcher_output' mod in your mod list / staging folder).\n\n"
                "Once you have done this, click Deploy."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 20))

        self._mo2_parity_var = ctk.BooleanVar(value=self._use_mo2_parity)
        ctk.CTkCheckBox(
            self._body,
            text="Per-mod conflict resolution (MO2 parity)",
            variable=self._mo2_parity_var,
            font=FONT_NORMAL, text_color=TEXT_DIM,
        ).pack(pady=(0, 4))
        ctk.CTkLabel(
            self._body,
            text=(
                "Builds a synthetic MO2 instance so PGPatcher attributes\n"
                "conflicts per-mod, matching a real MO2 setup. Experimental."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 12))

        self._deploy_status = ctk.CTkLabel(
            self._body, text="",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._deploy_status.pack(pady=(0, 8))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Skip", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_DIM,
            command=self._skip_deploy,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Deploy", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._start_deploy,
        ).pack(side="left")

    def _skip_deploy(self):
        # Honour the parity checkbox even when the user skips deploying.
        self._use_mo2_parity = bool(self._mo2_parity_var.get())
        if self._use_mo2_parity:
            self._set_label("_deploy_status", "Building MO2 instance…")
            threading.Thread(target=self._build_dummy_then_run, daemon=True).start()
        else:
            self._show_step_run()

    def _build_dummy_then_run(self):
        try:
            self._maybe_build_mo2_dummy()
        except Exception as exc:
            self._set_label("_deploy_status", f"MO2 instance error: {exc}", color="#e06c6c")
            self._log(f"PGPatcher Wizard: MO2 dummy error: {exc}")
            return
        self.after(0, self._show_step_run)

    def _start_deploy(self):
        self._use_mo2_parity = bool(self._mo2_parity_var.get())
        from gui.dialogs import confirm_deploy_appdata
        if not confirm_deploy_appdata(self.winfo_toplevel(), self._game):
            self._set_label("_deploy_status", "Deploy cancelled — AppData folder missing.", color="#e06c6c")
            return
        for w in self._body.winfo_children():
            if isinstance(w, ctk.CTkButton):
                w.configure(state="disabled")
        self._set_label("_deploy_status", "Deploying\u2026")
        threading.Thread(target=self._do_deploy, daemon=True).start()

    def _do_deploy(self):
        try:
            from Utils.filemap import build_filemap
            from Utils.deploy import (
                LinkMode,
                deploy_root_folder,
                restore_root_folder,
                load_per_mod_strip_prefixes,
            )
            from Utils.profile_state import read_excluded_mod_files

            game      = self._game
            game_root = game.get_game_path()

            try:
                root_win = self.winfo_toplevel()
                profile  = root_win._topbar._profile_var.get()
            except Exception:
                profile = "default"

            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            # Activate the last-deployed profile for restore so rescued
            # runtime files land in *that* profile's overwrite/ — not
            # the shared default. Critical for profile_specific_mods.
            last_deployed = game.get_last_deployed_profile()
            if last_deployed:
                game.set_active_profile_dir(
                    game.get_profile_root() / "profiles" / last_deployed
                )

            if getattr(game, "restore_before_deploy", True) and hasattr(game, "restore"):
                try:
                    game.restore(log_fn=_tlog)
                except RuntimeError:
                    pass
            restore_rf = game.get_effective_root_folder_path()
            if restore_rf.is_dir() and game_root:
                restore_root_folder(restore_rf, game_root, log_fn=_tlog)

            game.set_active_profile_dir(
                game.get_profile_root() / "profiles" / profile
            )

            profile_root = game.get_profile_root()
            staging      = game.get_effective_mod_staging_path()
            modlist_path = profile_root / "profiles" / profile / "modlist.txt"
            filemap_out  = staging.parent / "filemap.txt"

            if modlist_path.is_file():
                exc_raw = read_excluded_mod_files(modlist_path.parent, None)
                exc = {k: set(v) for k, v in exc_raw.items()} if exc_raw else None
                build_filemap(
                    modlist_path, staging, filemap_out,
                    strip_prefixes=game.mod_folder_strip_prefixes or None,
                    per_mod_strip_prefixes=load_per_mod_strip_prefixes(modlist_path.parent),
                    allowed_extensions=game.mod_install_extensions or None,
                    root_deploy_folders=game.mod_root_deploy_folders or None,
                    excluded_mod_files=exc,
                    conflict_ignore_filenames=getattr(game, "conflict_ignore_filenames", None) or None,
                )

            deploy_mode = game.get_deploy_mode() if hasattr(game, "get_deploy_mode") else LinkMode.HARDLINK
            game.deploy(log_fn=_tlog, profile=profile, mode=deploy_mode)

            from Utils.wine_dll_config import deploy_game_wine_dll_overrides
            pfx = game.get_prefix_path()
            if pfx and pfx.is_dir():
                deploy_game_wine_dll_overrides(game.name, pfx, game.wine_dll_overrides, log_fn=_tlog)

            game.save_last_deployed_profile(profile)

            target_rf  = game.get_effective_root_folder_path()
            rf_allowed = getattr(game, "root_folder_deploy_enabled", True)
            if rf_allowed and target_rf.is_dir() and game_root:
                deploy_root_folder(target_rf, game_root, mode=deploy_mode, log_fn=_tlog)

            self._set_label("_deploy_status", "Deploy complete.", color="#6bc76b")
            self._refresh_topbar_deploy_state()

            if self._use_mo2_parity:
                self._set_label("_deploy_status", "Building MO2 instance…")
                self._maybe_build_mo2_dummy()

            self.after(0, self._show_step_run)

        except Exception as exc:
            self._set_label("_deploy_status", f"Deploy error: {exc}", color="#e06c6c")
            self._log(f"PGPatcher Wizard: deploy error: {exc}")

    def _maybe_build_mo2_dummy(self):
        """Build the dummy MO2 instance for the active profile and remember its
        path so config + launch can switch PGPatcher into MO2 mode."""
        if not self._use_mo2_parity:
            self._mo2_dummy_dir = None
            return

        from wizards._mo2_dummy import build_mo2_dummy_instance

        try:
            root_win = self.winfo_toplevel()
            profile  = root_win._topbar._profile_var.get()
        except Exception:
            profile = "default"

        apps_dir = _get_applications_dir(self._game)
        game_pfx = self._game.get_prefix_path()
        # The dummy's Z: paths resolve through the *tool* prefix; GOG detection
        # inspects the *game* prefix's AppData folders.
        tool_pfx = None
        try:
            _ps, _env, compat = self._get_tool_env()
            if compat is not None:
                tool_pfx = compat / "pfx"
        except Exception:
            tool_pfx = None
        self._mo2_dummy_dir, self._mo2_game_type = build_mo2_dummy_instance(
            self._game, apps_dir, profile,
            prefix=tool_pfx or game_pfx,
            game_prefix=game_pfx,
            log_fn=lambda msg: self._log(f"PGPatcher Wizard: {msg}"),
        )

    # ------------------------------------------------------------------
    # Step 6 — Run PGPatcher
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 6: Run PGPatcher",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        exe = self._exe
        if exe is None:
            ctk.CTkLabel(
                self._body,
                text=(
                    f"{_PATCHER_EXE} was not found.\n"
                    "Please restart the wizard and install PGPatcher first."
                ),
                font=FONT_NORMAL, text_color="#e06c6c", justify="center",
            ).pack(pady=(0, 16))
            ctk.CTkButton(
                self._body, text="Close", width=120, height=36,
                font=FONT_BOLD,
                fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
                command=self._on_close_cb,
            ).pack(side="bottom")
            return

        self._run_status = ctk.CTkLabel(
            self._body, text="Launching PGPatcher\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._run_status.pack(pady=(0, 12))

        self._done_btn = ctk.CTkButton(
            self._body, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_done, state="disabled",
        )
        self._done_btn.pack(side="bottom")

        threading.Thread(target=lambda: self._do_run(exe), daemon=True).start()

    def _do_run(self, exe: Path):
        proton_script, env, compat_data = self._get_tool_env()
        if proton_script is None:
            self._set_label(
                "_run_status",
                f"Could not find Proton '{self._proton_name}' — "
                "check that it is installed in Steam.",
                color="#e06c6c",
            )
            return

        # Seed the game's Installed Path into the prefix registry — Skyrim only
        # writes it to the game prefix on first launch, so a fresh tool prefix
        # never has it (idempotent, marker-guarded).
        from Utils.bethesda_registry import maybe_register_for_game
        maybe_register_for_game(
            prefix_dir=compat_data,
            proton_script=proton_script,
            env=env,
            game=self._game,
            log_fn=lambda msg: self._log(f"PGPatcher Wizard: {msg}"),
        )

        self._link_plugins_txt(compat_data / "pfx")

        # Re-apply settings.json now that we know whether MO2 parity is on and
        # where the dummy instance lives.  Config step ran before the dummy was
        # built, so this is the authoritative write of modmanager.type.
        try:
            from Utils.exe_args_builder import _bootstrap_pgpatcher_settings
            _bootstrap_pgpatcher_settings(
                exe,
                self._game.get_game_path(),
                self._game.get_effective_mod_staging_path(),
                log_fn=lambda msg: self._log(f"PGPatcher Wizard: {msg}"),
                update=True,
                pfx=compat_data / "pfx",
                mo2_instance_dir=self._mo2_dummy_dir,
                game_type=self._mo2_game_type if self._mo2_dummy_dir is not None else None,
            )
        except Exception as exc:
            self._log(f"PGPatcher Wizard: settings re-apply error: {exc}")

        # MO2 mode requires either real USVFS or this bypass flag on Linux.
        launch_cmd = ["python3", str(proton_script), "run", str(exe)]
        if self._mo2_dummy_dir is not None:
            launch_cmd.append("--ignore-mo2vfscheck")

        self._log(f"PGPatcher Wizard: launching {exe} via Proton")
        try:
            proc = subprocess.Popen(
                launch_cmd,
                env=env,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._set_label(
                "_run_status",
                "PGPatcher is running.\nWait for it to finish, then click Done.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            proc.wait()
            shutdown_prefix_wineserver(
                proton_script, compat_data,
                log_fn=lambda m: self._log(f"PGPatcher Wizard: {m}"),
            )
            self._log("PGPatcher Wizard: PGPatcher closed.")
            self._set_label("_run_status", "PGPatcher finished.", color="#6bc76b")
            self.after(0, self._on_done)
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"PGPatcher Wizard: launch error: {exc}")

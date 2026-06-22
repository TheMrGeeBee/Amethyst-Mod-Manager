"""
pandora.py
Wizard for running Pandora Behaviour Engine+.

Unlike other wizards, Pandora is installed as a regular mod (not into
Applications/), so this wizard only appears when
"Pandora Behaviour Engine+.exe" can be found under the mod staging folder.

Workflow
--------
1. User is prompted to delete any previous Pandora output mod, then deploy.
2. User picks the Proton version to run Pandora with. Pandora gets its own
   isolated Wine prefix (prefix_<ProtonName>/ next to the exe) so the
   choice is independent of the game's Proton version. The pick is saved
   as the per-exe Proton override shared with the Mod Files exe launcher.
3. Silently install .NET 10 desktop runtime into that prefix
   (skipped if already installed).
4. Run Pandora Behaviour Engine+.exe via Proton with:
     --tesv:<game_path>

   The output folder (<staging>/Pandora_output) is configured by rewriting
   Pandora's Settings.json inside the Wine prefix, because newer
   Pandora builds ignore the ``--output:`` CLI flag.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from gui.path_utils import _to_wine_path

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD,
)
from Utils.protontricks import dotnet_dep_key as _dotnet_dep_key

_EXE_NAME = "Pandora Behaviour Engine+.exe"

_NET10_URL      = "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/10.0.0/windowsdesktop-runtime-10.0.0-win-x64.exe"
_NET10_FILENAME = "windowsdesktop-runtime-10.0.0-win-x64.exe"
_NET10_DEP_KEY  = _dotnet_dep_key("10")


def find_pandora_exe(game: "BaseGame") -> Path | None:
    """Search the mod staging directory for Pandora Behaviour Engine+.exe."""
    staging = game.get_effective_mod_staging_path()
    if not staging.is_dir():
        return None
    for candidate in staging.rglob(_EXE_NAME):
        if candidate.is_file():
            return candidate
    return None


from wizards._proton_prefix import ProtonPrefixStepMixin, shutdown_prefix_wineserver


class PandoraWizard(ProtonPrefixStepMixin, ctk.CTkFrame):
    """Wizard to deploy mods and run Pandora Behaviour Engine+."""

    _tool_exe_name      = _EXE_NAME
    _tool_display_name  = "Pandora"
    _proton_step_title  = "Step 2: Choose Proton Version"
    _exe_missing_text   = (
        f"'{_EXE_NAME}' was not found in your mod staging folder.\n\n"
        "Install Pandora Behaviour Engine+ as a mod, then reopen this wizard."
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
        self._exe         = find_pandora_exe(game)
        self._proton_name = ""

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text=f"Run Pandora \u2014 {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_close_cb,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_deploy()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
    # Step 1 — Delete previous output + deploy
    # ------------------------------------------------------------------

    def _show_step_deploy(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Deploy Modlist",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "Before deploying, please delete any output from a previous\n"
                "Pandora run (the 'Pandora_output' mod in your mod list).\n\n"
                "Once you have done this, click Deploy."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 20))

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
            command=self._show_step_proton,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            btn_frame, text="Deploy", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._start_deploy,
        ).pack(side="left")

    def _start_deploy(self):
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
            self._safe_after(0, self._show_step_proton)

        except Exception as exc:
            self._set_label("_deploy_status", f"Deploy error: {exc}", color="#e06c6c")
            self._log(f"Pandora Wizard: deploy error: {exc}")

    # ------------------------------------------------------------------
    # Step 3 — Install .NET 10 (silent)
    # ------------------------------------------------------------------

    def _show_step_deps(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Install Dependencies",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._net10_status = ctk.CTkLabel(
            self._body, text="Checking .NET 10\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._net10_status.pack(pady=(0, 6))

        threading.Thread(target=self._do_install_deps, daemon=True).start()

    def _do_install_deps(self):
        import urllib.request
        from Utils.config_paths import get_dotnet_cache_dir
        from Utils.protontricks import (
            is_dep_installed as _is_dep_installed,
            mark_dep_installed as _mark_dep_installed,
        )

        self._set_label("_net10_status", "Preparing Pandora's Wine prefix\u2026")
        proton_script, env, compat_data = self._get_tool_env()

        if proton_script is None:
            self._set_label(
                "_net10_status",
                f"Could not find Proton '{self._proton_name}' \u2014 "
                "check that it is installed in Steam, then reopen this wizard.",
                color="#e06c6c",
            )
            return

        prefix_path = compat_data / "pfx"

        if _is_dep_installed(prefix_path, _NET10_DEP_KEY):
            self._set_label("_net10_status", ".NET 10 already installed \u2014 skipping.", color="#6bc76b")
            self._safe_after(500, self._show_step_run)
            return

        cache_path = get_dotnet_cache_dir() / _NET10_FILENAME

        try:
            if not cache_path.is_file():
                self._set_label("_net10_status", "Downloading .NET 10 runtime\u2026")
                self._log("Pandora Wizard: downloading .NET 10 runtime \u2026")
                from Utils.ca_bundle import download_file
                download_file(_NET10_URL, cache_path)
                self._log("Pandora Wizard: .NET 10 download complete.")
            else:
                self._log("Pandora Wizard: using cached .NET 10 installer.")

            self._set_label(
                "_net10_status",
                "Installing .NET 10 into Pandora's prefix\u2026\n(this may take a few minutes)",
            )
            self._log("Pandora Wizard: launching .NET 10 installer in Pandora's prefix \u2026")

            proc = subprocess.run(
                ["python3", str(proton_script), "run", str(cache_path), "/quiet", "/norestart"],
                env=env,
                cwd=str(cache_path.parent),
            )

            # Exit codes from the .NET desktop runtime installer:
            #   0    = installed successfully
            #   1602 = user cancel
            #   1638 = another version already installed (success, no-op)
            #   3010 = installed, reboot required (success)
            #   102  = already installed / no-op (success)
            _ok_codes = {0, 102, 1638, 3010}
            if proc.returncode not in _ok_codes:
                raise RuntimeError(f".NET 10 installer exited with code {proc.returncode}.")

            _mark_dep_installed(prefix_path, _NET10_DEP_KEY)
            self._set_label("_net10_status", ".NET 10 installed successfully.", color="#6bc76b")
            self._safe_after(500, self._show_step_run)

        except Exception as exc:
            self._set_label("_net10_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"Pandora Wizard: .NET 10 install error: {exc}")

    # ------------------------------------------------------------------
    # Step 4 — Run Pandora
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Run Pandora",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        exe = self._exe
        if exe is None:
            ctk.CTkLabel(
                self._body,
                text=(
                    f"'{_EXE_NAME}' was not found in your mod staging folder.\n\n"
                    "Install Pandora Behaviour Engine+ as a mod, then reopen this wizard."
                ),
                font=FONT_NORMAL, text_color="#e06c6c", justify="center", wraplength=460,
            ).pack(pady=(0, 16))
            ctk.CTkButton(
                self._body, text="Close", width=120, height=36,
                font=FONT_BOLD,
                fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
                command=self._on_close_cb,
            ).pack(side="bottom")
            return

        self._run_status = ctk.CTkLabel(
            self._body, text="Launching Pandora\u2026",
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

        game_path = self._game.get_game_path()
        if game_path is None:
            self._set_label("_run_status", "Game path not configured.", color="#e06c6c")
            return

        staging = self._game.get_effective_mod_staging_path()

        # Seed the Bethesda registry key in the fresh prefix so tools that
        # look the game path up via the registry keep working (idempotent).
        from Utils.bethesda_registry import maybe_register_for_game
        maybe_register_for_game(
            prefix_dir=compat_data,
            proton_script=proton_script,
            env=env,
            game=self._game,
            log_fn=self._log,
        )

        from Utils.exe_args_builder import _bootstrap_pandora_settings
        _bootstrap_pandora_settings(
            getattr(self._game, "game_id", None),
            game_path,
            staging,
            compat_data,
            self._log,
        )

        pfx = compat_data / "pfx"
        game_arg = f'--tesv:{_to_wine_path(game_path, pfx)}'

        # Unset .NET environment variables that can prevent Pandora from launching
        # when the host has a .NET runtime installed (e.g. via Bottles/MO2).
        env.pop("DOTNET_ROOT", None)
        env.pop("DOTNET_BUNDLE_EXTRACT_BASE_DIR", None)

        # WPF rendering over DXVK produces a double title bar / frame glitch
        # in Proton. Forcing the WineD3D GDI renderer bypasses the Vulkan path
        # entirely and gives a single, properly-decorated window.
        # PROTON_USE_WINED3D is required — WINE_D3D_CONFIG only takes effect
        # when WineD3D (not DXVK) is actually handling the d3d calls.
        env["PROTON_USE_WINED3D"] = "1"
        env["WINE_D3D_CONFIG"] = "renderer=gdi"

        cmd = ["python3", str(proton_script), "run", str(exe), game_arg]
        self._log(f"Pandora Wizard: launching {exe} via Proton")
        self._log(f"  cmd: {' '.join(cmd)}")
        self._log(
            "  env: "
            f"PROTON_USE_WINED3D={env.get('PROTON_USE_WINED3D', '<unset>')} "
            f"WINE_D3D_CONFIG={env.get('WINE_D3D_CONFIG', '<unset>')} "
            f"STEAM_COMPAT_DATA_PATH={env.get('STEAM_COMPAT_DATA_PATH', '<unset>')}"
        )
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._set_label(
                "_run_status",
                "Pandora is running.\nClose it when you are done, then click Done.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            _stdout, stderr_bytes = proc.communicate()
            rc = proc.returncode
            shutdown_prefix_wineserver(
                proton_script, compat_data,
                log_fn=lambda m: self._log(f"Pandora Wizard: {m}"),
            )
            self._log(f"Pandora Wizard: Pandora exited (code {rc}).")
            if stderr_bytes:
                for line in stderr_bytes.decode(errors="replace").splitlines():
                    self._log(f"  Pandora stderr: {line}")
            if rc != 0:
                self._set_label(
                    "_run_status",
                    f"Pandora exited with error (code {rc}).\nSee the log for details. Click Done to close.",
                    color="#e06c6c",
                )
            else:
                self._set_label("_run_status", "Pandora finished. Click Done to close.", color="#6bc76b")
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"Pandora Wizard: launch error: {exc}")

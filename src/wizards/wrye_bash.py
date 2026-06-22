"""
wrye_bash.py
Wizard for installing and running Wrye Bash.

Auto-downloads the latest Standalone Executable release from GitHub.
Extracts to Profiles/<game>/Applications/Wrye Bash/ and runs via Proton in
its own isolated prefix (prefix_<ProtonName>/ next to the exe) with the
game's Installed Path seeded into the registry and the profile's plugins.txt
and the game prefix's My Games folder linked in.
"""

from __future__ import annotations

import shutil
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
    TEXT_DIM, TEXT_MAIN, TEXT_ON_ACCENT,
    FONT_NORMAL, FONT_BOLD,
)

_GITHUB_API = "https://api.github.com/repos/wrye-bash/wrye-bash/releases/latest"
_EXE_NAME   = "Wrye Bash.exe"
_APP_DIR    = "Wrye Bash"


def _get_applications_dir(game: "BaseGame") -> Path:
    return game.get_mod_staging_path().parent / "Applications" / _APP_DIR


def _wrye_bash_exe_path(game: "BaseGame") -> Path | None:
    p = _get_applications_dir(game) / _EXE_NAME
    return p if p.is_file() else None


def _flatten_subdirs(dest: Path, exe_name: str) -> None:
    while True:
        all_entries = [e for e in dest.iterdir() if e.name != "__MACOSX"]
        subdirs = [e for e in all_entries if e.is_dir()]
        if len(subdirs) == 1 and not (dest / exe_name).is_file():
            wrapper = subdirs[0]
            tmp = dest.parent / (dest.name + "_flatten_tmp")
            wrapper.rename(tmp)
            for item in tmp.iterdir():
                shutil.move(str(item), str(dest / item.name))
            tmp.rmdir()
        else:
            break


from wizards._proton_prefix import ProtonPrefixStepMixin, shutdown_prefix_wineserver


class WryeBashWizard(ProtonPrefixStepMixin, ctk.CTkFrame):
    """Wizard to download, install and run Wrye Bash."""

    _tool_exe_name      = _EXE_NAME
    _tool_display_name  = "Wrye Bash"
    _proton_step_title  = "Step 3: Choose Proton Version"
    _exe_missing_text   = (
        f"{_EXE_NAME!r} was not found.\n"
        "Please restart the wizard to reinstall Wrye Bash."
    )

    def _proton_next_step(self):
        self._show_step_run()

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
        self._exe         = _wrye_bash_exe_path(game)
        self._proton_name = ""

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text=f"Run Wrye Bash \u2014 {game.name}",
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
    # Step 1 — Auto-download from GitHub (skipped if already installed)
    # ------------------------------------------------------------------

    def _show_step_download(self):
        if self._exe is not None:
            self._show_step_deploy()
            return

        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Download Wrye Bash",
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
            tag, dl_url = _fetch_latest_github_asset(
                _GITHUB_API, ["standalone", "executable"]
            )
            self._set_label("_dl_status", f"Downloading {tag}\u2026")
            self._log(f"Wrye Bash Wizard: downloading {tag} from {dl_url}")

            suffix = Path(dl_url).suffix or ".7z"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = Path(tmp.name)

            from Utils.ca_bundle import download_file
            download_file(dl_url, tmp_path)
            self._set_label("_dl_status", "Extracting\u2026")
            self._log("Wrye Bash Wizard: download complete, extracting\u2026")

            dest = _get_applications_dir(self._game)
            dest.mkdir(parents=True, exist_ok=True)
            paths = _extract_archive(tmp_path, dest)
            tmp_path.unlink(missing_ok=True)

            file_count = len([p for p in paths if p.is_file()])
            _flatten_subdirs(dest, _EXE_NAME)

            if not (dest / _EXE_NAME).is_file():
                raise RuntimeError(f"{_EXE_NAME!r} not found after extraction.")
            self._exe = dest / _EXE_NAME

            self._log(f"Wrye Bash Wizard: extracted {file_count} file(s).")
            self._set_label("_dl_status", f"Downloaded and extracted {tag}.", color="#6bc76b")
            self.after(500, self._show_step_deploy)

        except Exception as exc:
            self._set_label("_dl_status", f"Error: {exc}", color="#e06c6c")
            self._log(f"Wrye Bash Wizard: download error: {exc}")

    # ------------------------------------------------------------------
    # Step 2 — Deploy modlist
    # ------------------------------------------------------------------

    def _show_step_deploy(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Deploy Modlist",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self._body,
            text=(
                "Deploy the modlist so Wrye Bash sees your mods and the\n"
                "Bashed Patch it creates lands in the modded Data folder.\n"
                "On restore, the new plugin is moved to Overwrite."
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
        self._set_label("_deploy_status", "Deploying…")
        threading.Thread(target=self._do_deploy, daemon=True).start()

    def _do_deploy(self):
        try:
            from Utils.deploy_pipeline import run_deploy_pipeline

            game = self._game
            try:
                root_win = self.winfo_toplevel()
                profile  = root_win._topbar._profile_var.get()
            except Exception:
                profile = "default"

            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            success = run_deploy_pipeline(
                game, profile, log_fn=_tlog,
            )

            if success:
                self._set_label("_deploy_status", "Deploy complete.", color="#6bc76b")
                self._refresh_topbar_deploy_state()
                self.after(0, self._show_step_proton)
            else:
                self._set_label("_deploy_status", "Deploy failed — see log.", color="#e06c6c")

        except Exception as exc:
            self._set_label("_deploy_status", f"Deploy error: {exc}", color="#e06c6c")
            self._log(f"Wrye Bash Wizard: deploy error: {exc}")

    # ------------------------------------------------------------------
    # Step 4 — Run Wrye Bash
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Run Wrye Bash",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        exe = self._exe
        if exe is None:
            ctk.CTkLabel(
                self._body,
                text=(
                    f"{_EXE_NAME!r} was not found.\n"
                    "Please restart the wizard to reinstall Wrye Bash."
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
            self._body, text="Launching Wrye Bash\u2026",
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
        pfx = compat_data / "pfx"

        # WB reads the game's Installed Path from the registry, the load order
        # from plugins.txt in AppData and the game INIs from My Games — a fresh
        # tool prefix has none of them.
        from Utils.bethesda_registry import maybe_register_for_game
        maybe_register_for_game(
            prefix_dir=compat_data,
            proton_script=proton_script,
            env=env,
            game=self._game,
            log_fn=lambda msg: self._log(f"Wrye Bash Wizard: {msg}"),
        )
        self._link_plugins_txt(pfx)
        self._link_mygames(pfx)

        # WB derives its .wbtemp dir from the drive letter of the -o path.
        # Z:\ (Wine's Linux root mapping) is not writable, so we symlink the
        # game folder into drive_c/wb_games/ and pass a C:\ path instead.
        game_arg = []
        if game_path:
            real_game = game_path.resolve()
            c_games = pfx / "drive_c" / "wb_games"
            c_games.mkdir(parents=True, exist_ok=True)
            link = c_games / real_game.name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(real_game)
            game_arg = ["-o", f"C:\\wb_games\\{real_game.name}"]

        self._log(f"Wrye Bash Wizard: launching {exe} via Proton" + (f" with -o C:\\wb_games\\{game_path.resolve().name}" if game_path else ""))
        try:
            proc = subprocess.Popen(
                ["python3", str(proton_script), "run", str(exe)] + game_arg,
                env=env,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._set_label(
                "_run_status",
                "Wrye Bash is running.\nClose it when you are done, then click Done.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            proc.wait()
            shutdown_prefix_wineserver(
                proton_script, compat_data,
                log_fn=lambda m: self._log(f"Wrye Bash Wizard: {m}"),
            )
            self._log("Wrye Bash Wizard: Wrye Bash closed.")
            self._set_label("_run_status", "Wrye Bash finished.", color="#6bc76b")
            self.after(0, self._on_done)
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"Wrye Bash Wizard: launch error: {exc}")

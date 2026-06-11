"""
bodyslide.py
Wizards for running BodySlide.exe and OutfitStudio.exe (older versions used the
"BodySlide x64.exe" / "OutfitStudio x64.exe" names, still supported).

Both tools are installed as regular mods (not into Applications/).  The wizard
only appears when the relevant exe is found under the mod staging folder.

Key requirement: the exe must be launched with cwd set to the game's Data
folder so it can locate game assets correctly.

Workflow
--------
1. Deploy the modlist.
2. User picks the Proton version. The tool gets its own isolated prefix
   (prefix_<ProtonName>/ next to the *staged* exe — never inside the game's
   Data folder), independent of the game's Proton version. BodySlide and
   Outfit Studio share one prefix when set to the same version.
3. Run the deployed exe from <game_path>/Data via Proton, after seeding the
   game's Installed Path into the prefix registry.
"""

from __future__ import annotations

import re
import subprocess
import threading
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


def _as_names(exe_name) -> tuple[str, ...]:
    """Accept a single name or an iterable of candidate names."""
    if isinstance(exe_name, str):
        return (exe_name,)
    return tuple(exe_name)


def find_mod_exe(game: "BaseGame", exe_name) -> Path | None:
    """Search the mod staging directory for exe_name (used to decide whether to show the wizard)."""
    staging = game.get_effective_mod_staging_path()
    if not staging.is_dir():
        return None
    for name in _as_names(exe_name):
        for candidate in staging.rglob(name):
            if candidate.is_file():
                return candidate
    return None


def find_deployed_exe(game: "BaseGame", exe_name) -> Path | None:
    """Search the deployed Data directory for exe_name (used at launch time after deploy)."""
    data_path = game.get_mod_data_path()
    if data_path is None or not data_path.is_dir():
        return None
    for name in _as_names(exe_name):
        for candidate in data_path.rglob(name):
            if candidate.is_file():
                return candidate
    return None


# ---------------------------------------------------------------------------
# Base wizard
# ---------------------------------------------------------------------------

from wizards._proton_prefix import ProtonPrefixStepMixin, shutdown_prefix_wineserver


class _BodySlideBaseWizard(ProtonPrefixStepMixin, ctk.CTkFrame):

    _wizard_title    = ""   # overridden by subclasses
    _exe_name        = ""   # overridden by subclasses
    _output_mod_name = ""   # overridden by subclasses — empty mod created to capture output

    _proton_step_title = "Step 2: Choose Proton Version"
    _proton_deps_note  = "Each version gets its own prefix."

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

        # The prefix is anchored to the staged exe (prefix_* dirs in staging
        # are excluded from filemap scans); the deployed copy is what runs.
        self._exe         = find_mod_exe(game, self._exe_name)
        self._proton_name = ""
        self._tool_exe_name     = self._exe.name if self._exe is not None else _as_names(self._exe_name)[0]
        self._tool_display_name = self._wizard_title
        self._exe_missing_text  = (
            f"'{_as_names(self._exe_name)[0]}' was not found in your mod staging folder.\n\n"
            f"Install {self._wizard_title} as a mod, then reopen this wizard."
        )

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text=f"{self._wizard_title} \u2014 {game.name}",
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

    @staticmethod
    def _to_wine_path(p: Path) -> str:
        s = str(p).replace("/", "\\")
        if not s.startswith("\\"):
            s = "\\" + s
        return "Z:" + s + "\\"

    def _ensure_output_mod(self) -> Path:
        staging = self._game.get_effective_mod_staging_path()
        mod_dir = staging / self._output_mod_name
        mod_dir.mkdir(parents=True, exist_ok=True)

        try:
            root_win = self.winfo_toplevel()
            profile  = root_win._topbar._profile_var.get()
        except Exception:
            profile = "default"

        modlist_path = self._game.get_profile_root() / "profiles" / profile / "modlist.txt"
        if modlist_path.is_file():
            from Utils.modlist import read_modlist, prepend_mod
            entries = read_modlist(modlist_path)
            if not any(e.name == self._output_mod_name for e in entries):
                prepend_mod(modlist_path, self._output_mod_name, enabled=True)

        return mod_dir

    def _config_xml_path(self, base: Path) -> Path | None:
        direct = base / "CalienteTools" / "BodySlide" / "Config.xml"
        if direct.is_file():
            return direct
        for cand in base.rglob("Config.xml"):
            if cand.is_file() and cand.parent.name.lower() == "bodyslide":
                return cand
        return None

    # Maps a game's synthesis_registry_name to its BodySlide GameDataPaths child
    # tag and TargetGame index (see the comment at the top of Config.xml).
    _BODYSLIDE_GAMES = {
        "Fallout3":                ("Fallout3", 0),
        "FalloutNewVegas":         ("FalloutNewVegas", 1),
        "Skyrim":                  ("Skyrim", 2),
        "Fallout4":                ("Fallout4", 3),
        "Skyrim Special Edition":  ("SkyrimSpecialEdition", 4),
        "Fallout 4 VR":            ("Fallout4VR", 5),
        "Skyrim VR":               ("SkyrimVR", 6),
    }

    def _bodyslide_game(self) -> "tuple[str, int] | None":
        return self._BODYSLIDE_GAMES.get(
            getattr(self._game, "synthesis_registry_name", None)
        )

    def _set_gamedatapaths_child(self, text: str, tag: str, value: str) -> str:
        """Set <tag> inside the <GameDataPaths> block (creating it if absent)."""
        block = re.search(r"<GameDataPaths>(.*?)</GameDataPaths>", text, flags=re.DOTALL)
        if not block:
            return text
        inner = block.group(1)
        new_child = f"<{tag}>{value}</{tag}>"
        child_pat = rf"<{tag}>.*?</{tag}>"
        if re.search(child_pat, inner, flags=re.DOTALL):
            new_inner = re.sub(child_pat, lambda _m: new_child, inner, count=1, flags=re.DOTALL)
        else:
            new_inner = inner + f"        {new_child}\n    "
        return text[: block.start(1)] + new_inner + text[block.end(1) :]

    def _set_config_tag(self, text: str, tag: str, value: str) -> str:
        new_tag = f"<{tag}>{value}</{tag}>"
        pattern = rf"<{tag}>.*?</{tag}>"
        if re.search(pattern, text, flags=re.DOTALL):
            return re.sub(pattern, lambda _m: new_tag, text, count=1, flags=re.DOTALL)
        return text.replace("</Config>", f"    {new_tag}\n</Config>", 1)

    def _update_output_path_in_config(self, config_path: Path, output_dir: Path) -> bool:
        try:
            text = config_path.read_text(encoding="utf-8")
        except OSError:
            return False

        updated = self._set_config_tag(
            text, "OutputDataPath", self._to_wine_path(output_dir)
        )

        data_path = self._game.get_mod_data_path()
        if data_path is not None:
            wine_data = self._to_wine_path(data_path)
            updated = self._set_config_tag(updated, "GameDataPath", wine_data)

            mapping = self._bodyslide_game()
            if mapping is not None:
                tag, target = mapping
                updated = self._set_gamedatapaths_child(updated, tag, wine_data)
                updated = self._set_config_tag(updated, "TargetGame", str(target))

        if updated == text:
            return True
        try:
            config_path.write_text(updated, encoding="utf-8")
        except OSError:
            return False
        return True

    def _apply_output_redirect(self, *, post_deploy: bool) -> None:
        try:
            output_mod = self._ensure_output_mod()
        except OSError as exc:
            self._log(f"{self._wizard_title} Wizard: could not create '{self._output_mod_name}': {exc}")
            return

        staging = self._game.get_effective_mod_staging_path()
        source_cfg = None
        for sub in staging.iterdir() if staging.is_dir() else []:
            if not sub.is_dir():
                continue
            cand = self._config_xml_path(sub)
            if cand is not None:
                source_cfg = cand
                break

        if source_cfg is not None:
            if self._update_output_path_in_config(source_cfg, output_mod):
                self._log(
                    f"{self._wizard_title} Wizard: set OutputDataPath → "
                    f"{self._to_wine_path(output_mod)} (source)"
                )

        if post_deploy:
            data_path = self._game.get_mod_data_path()
            if data_path is not None and data_path.is_dir():
                deployed_cfg = self._config_xml_path(data_path)
                if deployed_cfg is not None and (
                    source_cfg is None or deployed_cfg.resolve() != source_cfg.resolve()
                ):
                    self._update_output_path_in_config(deployed_cfg, output_mod)

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
    # Step 1 — Deploy
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
                f"{self._wizard_title} must be run from the deployed Data folder.\n\n"
                "Deploy your modlist first, then click Run."
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
            from Utils.deploy_pipeline import run_deploy_pipeline

            game = self._game
            try:
                root_win = self.winfo_toplevel()
                profile  = root_win._topbar._profile_var.get()
            except Exception:
                profile = "default"

            def _tlog(msg):
                self.after(0, lambda m=msg: self._log(m))

            # Materialize the output-capture mod (and enable it in the modlist
            # + point Config.xml's OutputDataPath at it) before the filemap is
            # built, so it gets picked up by the deploy.
            success = run_deploy_pipeline(
                game, profile,
                log_fn=_tlog,
                on_pre_filemap=lambda: self._apply_output_redirect(post_deploy=False),
            )

            if success:
                self._set_label("_deploy_status", "Deploy complete.", color="#6bc76b")
                self.after(0, self._show_step_proton)
            else:
                self._set_label("_deploy_status", "Deploy failed — see log.", color="#e06c6c")

        except Exception as exc:
            self._set_label("_deploy_status", f"Deploy error: {exc}", color="#e06c6c")
            self._log(f"{self._wizard_title} Wizard: deploy error: {exc}")

    # ------------------------------------------------------------------
    # Step 3 — Run
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text=f"Step 3: Run {self._wizard_title}",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        exe = find_deployed_exe(self._game, self._exe_name)
        if exe is None:
            ctk.CTkLabel(
                self._body,
                text=(
                    f"'{self._exe_name[0]}' was not found in the deployed Data folder.\n\n"
                    f"Deploy your modlist first, then reopen this wizard."
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
            self._body, text=f"Launching {self._wizard_title}\u2026",
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
        # Re-apply in case the user skipped deploy, and patch the deployed
        # copy directly when deploy mode produced an independent file.
        try:
            self._apply_output_redirect(post_deploy=True)
        except Exception as exc:
            self._log(f"{self._wizard_title} Wizard: output redirect failed: {exc}")

        proton_script, env, compat_data = self._get_tool_env()
        if proton_script is None:
            self._set_label(
                "_run_status",
                f"Could not find Proton '{self._proton_name}' — "
                "check that it is installed in Steam.",
                color="#e06c6c",
            )
            return

        # Proton's Xalia UI-automation helper destabilises BodySlide / Outfit
        # Studio (wxWidgets): it floods the app with window-handle queries and
        # crashes with "Invalid window handle" when the Preview child window
        # opens ("Fatal exception has occurred"). Disabling it fixes the crash.
        env["PROTON_DISABLE_XALIA"] = "1"

        # BodySlide x64 / Outfit Studio x64 autofill the Data folder from
        # the Bethesda Softworks registry key — a fresh tool prefix never
        # has it, so seed it before launch (idempotent, marker-guarded).
        try:
            from Utils.bethesda_registry import maybe_register_for_game
            maybe_register_for_game(
                prefix_dir=compat_data,
                proton_script=Path(proton_script),
                env=env,
                game=self._game,
                log_fn=self._log,
            )
        except Exception as exc:
            self._log(f"{self._wizard_title} Wizard: registry write skipped: {exc}")

        self._log(f"{self._wizard_title} Wizard: launching {exe} via Proton (cwd={exe.parent})")
        try:
            proc = subprocess.Popen(
                ["python3", str(proton_script), "run", str(exe)],
                env=env,
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._set_label(
                "_run_status",
                f"{self._wizard_title} is running.\nClose it when you are done, then click Done.",
                color="#6bc76b",
            )
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            proc.wait()
            shutdown_prefix_wineserver(
                proton_script, compat_data,
                log_fn=lambda m: self._log(f"{self._wizard_title} Wizard: {m}"),
            )
            self._log(f"{self._wizard_title} Wizard: {exe.name} closed.")
            self._set_label("_run_status", f"{self._wizard_title} finished.", color="#6bc76b")
            self.after(0, self._on_done)
        except Exception as exc:
            self._set_label("_run_status", f"Launch error: {exc}", color="#e06c6c")
            self._log(f"{self._wizard_title} Wizard: launch error: {exc}")


# ---------------------------------------------------------------------------
# Concrete wizards
# ---------------------------------------------------------------------------

class BodySlideWizard(_BodySlideBaseWizard):
    _wizard_title    = "BodySlide"
    _exe_name        = ("BodySlide.exe", "BodySlide x64.exe")
    _output_mod_name = "BodySlide_files"


class OutfitStudioWizard(_BodySlideBaseWizard):
    _wizard_title    = "Outfit Studio"
    _exe_name        = ("OutfitStudio.exe", "OutfitStudio x64.exe")
    _output_mod_name = "OutfitStudio_files"

"""
dtkit_patch.py
Wizard for patching Darktide's bundle database with dtkit-patch.

The Darktide Mod Loader (DML) mod ships the current Windows ``dtkit-patch.exe``
in its ``tools/`` folder.  Earlier versions of this wizard downloaded a separate
native-Linux dtkit-patch build, but that build lags behind and breaks after game
updates.  Instead we now run the exe that ships with the user's DML install,
under the game's Proton prefix — mirroring DML's ``toggle_darktide_mods.bat``:

    cd <game>
    .\\tools\\dtkit-patch --toggle .\\bundle

Workflow:
  1. Deploy the modlist (same as the Deploy button) so DML's files — including
     tools/dtkit-patch.exe and the bundle/ folder — land in the game directory.
  2. Run dtkit-patch.exe --toggle bundle under Proton from the game folder,
     showing live output.  --toggle flips the patched/unpatched state.

Re-run after every game update (updates revert the patch).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import customtkinter as ctk

from Utils.dtkit_patch_helper import find_deployed_dtkit_exe, run_dtkit_patch_proton

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)


# ============================================================================
# Wizard dialog
# ============================================================================

class DtkitPatchWizard(ctk.CTkFrame):
    """Two-step wizard: deploy the modlist, then toggle the bundle patch."""

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

        # --- Title bar ---
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Patch Game (dtkit-patch) — {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_deploy()

    def _on_cancel(self):
        self._on_close_cb()

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _set_label(self, attr: str, text: str, color: str = TEXT_DIM):
        widget = getattr(self, attr, None)
        if widget is None:
            return
        try:
            self.after(0, lambda: widget.configure(text=text, text_color=color))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Step 1 — Deploy the modlist
    # ------------------------------------------------------------------

    def _show_step_deploy(self):
        self._clear_body()

        # If DML is already deployed (exe present in the game folder) we can skip
        # straight to running the patcher.
        if find_deployed_dtkit_exe(self._game.get_game_path()) is not None:
            self._show_step_run()
            return

        ctk.CTkLabel(
            self._body, text="Step 1: Deploy mods",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            self._body,
            text=(
                "dtkit-patch.exe ships with the Darktide Mod Loader and runs under "
                "Proton, so it always matches your installed version.\n\n"
                "Your mods will be deployed first so the patcher and the bundle "
                "database are present in the game folder."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM,
            justify="center", wraplength=500,
        ).pack(pady=(0, 12))

        self._deploy_status = ctk.CTkLabel(
            self._body, text="", font=FONT_SMALL, text_color=TEXT_DIM,
            justify="center", wraplength=500,
        )
        self._deploy_status.pack(pady=(0, 8))

        self._deploy_progress = ctk.CTkProgressBar(self._body, width=420, mode="indeterminate")

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._deploy_btn = ctk.CTkButton(
            btn_frame, text="Deploy →", width=140, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_deploy,
        )
        self._deploy_btn.pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Cancel", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right")

    def _on_deploy(self):
        """Run the exact same flow as pressing the Deploy button.

        We delegate to top_bar._run_deploy (same path Run EXE uses) instead of
        calling run_deploy_pipeline ourselves, so deploy serialization, the
        AppData confirm, root-folder state, the CET prompt, the post-deploy
        mod-panel reload and the deploy-warnings popup all behave identically.
        On success it chains _on_deploy_done, which advances to the run step.
        """
        game_path = self._game.get_game_path()
        if game_path is None or not game_path.is_dir():
            self._set_label("_deploy_status", "Game path not configured.", color="#e06c6c")
            return

        try:
            topbar = self.winfo_toplevel()._topbar
        except AttributeError:
            self._set_label(
                "_deploy_status",
                "Top bar unavailable — cannot deploy from the wizard.",
                color="#e06c6c",
            )
            return

        # Keep the button enabled: top_bar._run_deploy only fires on_complete on
        # success, so on a failed/coalesced deploy the user can simply press
        # Deploy again (re-runs are coalesced safely by _run_deploy). The status
        # bar shows real progress; the wizard spinner is just a busy hint.
        self._deploy_progress.pack(pady=(0, 12))
        self._deploy_progress.start()
        self._set_label("_deploy_status", "Deploying mods… (see the status bar for progress)")

        profile = topbar._profile_var.get()
        topbar._run_deploy(self._game, profile, on_complete=self._on_deploy_done)

    def _on_deploy_done(self):
        """Main-thread callback fired by top_bar._run_deploy on a successful deploy."""
        try:
            self._deploy_progress.stop()
            self._deploy_progress.pack_forget()
        except Exception:
            pass

        if find_deployed_dtkit_exe(self._game.get_game_path()) is None:
            self._set_label(
                "_deploy_status",
                "Deploy finished, but tools/dtkit-patch.exe was not found in the "
                "game folder.\nMake sure the Darktide Mod Loader mod is enabled.",
                color="#e06c6c",
            )
            return

        self._show_step_run()

    # ------------------------------------------------------------------
    # Step 2 — Run dtkit-patch.exe under Proton
    # ------------------------------------------------------------------

    def _show_step_run(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Toggle bundle patch",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        game_path = self._game.get_game_path()
        exe = find_deployed_dtkit_exe(game_path)

        ctk.CTkLabel(
            self._body,
            text=(
                f"Patcher:\n{exe}\n\n"
                f"Game folder (cwd):\n{game_path}\n\n"
                "Toggle flips the patch on or off (same as the Mod Loader's\n"
                "toggle_darktide_mods.bat). Patch to enable mods; toggle again\n"
                "to disable. Re-run after every game update."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM,
            justify="center", wraplength=500,
        ).pack(pady=(0, 12))

        self._run_output = ctk.CTkTextbox(
            self._body, height=140, font=("Courier New", 12),
            fg_color=BG_PANEL, text_color=TEXT_MAIN,
            state="disabled",
        )
        self._run_output.pack(fill="x", pady=(0, 8))

        self._run_status = ctk.CTkLabel(
            self._body, text="", font=FONT_NORMAL, text_color=TEXT_DIM,
            wraplength=500, justify="center",
        )
        self._run_status.pack(pady=(0, 4))

        btn_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._done_btn = ctk.CTkButton(
            btn_frame, text="Done", width=120, height=36,
            font=FONT_BOLD,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._on_cancel, state="disabled",
        )
        self._done_btn.pack(side="right", padx=(8, 0))

        self._toggle_btn = ctk.CTkButton(
            btn_frame, text="Toggle Patch", width=140, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_toggle,
        )
        self._toggle_btn.pack(side="right")

    def _append_output(self, text: str) -> None:
        try:
            self.after(0, lambda: (
                self._run_output.configure(state="normal"),
                self._run_output.insert("end", text + "\n"),
                self._run_output.configure(state="disabled"),
                self._run_output.see("end"),
            ))
        except Exception:
            pass

    def _on_toggle(self):
        self.after(0, lambda: self._toggle_btn.configure(state="disabled"))
        self._set_label("_run_status", "Running dtkit-patch — toggle…")
        threading.Thread(target=self._do_toggle, daemon=True).start()

    def _do_toggle(self):
        try:
            ok = run_dtkit_patch_proton(
                self._game,
                flag="--toggle",
                log_fn=self._log,
                line_fn=self._append_output,
            )
        except Exception as exc:
            self._set_label("_run_status", f"Error running dtkit-patch: {exc}", color="#e06c6c")
            self._log(f"dtkit-patch wizard: run error: {exc}")
            ok = False
        finally:
            self.after(0, lambda: self._done_btn.configure(state="normal"))
            self.after(0, lambda: self._toggle_btn.configure(state="normal"))

        if ok:
            self._set_label(
                "_run_status",
                "Done. The bundle patch state was toggled.\n"
                "Launch the game to verify mods load (toggle again to disable).",
                color="#6bc76b",
            )
        else:
            self._set_label(
                "_run_status",
                "dtkit-patch did not complete successfully.\nCheck the output above and the log.",
                color="#e06c6c",
            )

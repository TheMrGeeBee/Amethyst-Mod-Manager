"""
_proton_prefix.py
Shared wizard support for running tools in their own isolated Proton prefix.

The prefix lives at prefix_<ProtonName>/ next to the tool exe (created by
gui.dialogs._get_tool_prefix_env) and the chosen Proton version persists as
the per-exe override (__proton_override_<exe>) shared with the Mod Files
exe launcher, so wizard and direct exe runs use the same prefix.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import customtkinter as ctk

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_HEADER, BG_PANEL,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD,
)

_LAUNCH_MODE_FILE = "exe_launch_mode.json"


def load_saved_proton(game, exe_name: str) -> str:
    """Return the saved per-exe Proton override for exe_name ('' if none)."""
    from Utils.config_paths import get_game_config_dir
    p = get_game_config_dir(game.name) / _LAUNCH_MODE_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(f"__proton_override_{exe_name}") or ""
    except (OSError, ValueError):
        return ""


def save_saved_proton(game, exe_name: str, proton_name: str) -> None:
    """Persist the Proton pick as the per-exe override shared with the exe launcher."""
    from Utils.config_paths import get_game_config_dir
    p = get_game_config_dir(game.name) / _LAUNCH_MODE_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        data = {}
    data[f"__proton_override_{exe_name}"] = proton_name
    try:
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def shutdown_prefix_wineserver(proton_script: Path, compat_data: Path, log_fn=None) -> None:
    """Kill leftover wine processes still attached to a tool prefix.

    Proton sidecars (xalia.exe, services.exe, explorer.exe) can keep the
    prefix's wineserver alive indefinitely after the tool itself closes;
    they outlive the app and linger until the desktop session ends.
    """
    try:
        proton_dir = Path(proton_script).parent
        bin_dir = next(
            (proton_dir / d / "bin" for d in ("files", "dist")
             if (proton_dir / d / "bin" / "wineserver").is_file()),
            None,
        )
        if bin_dir is None:
            return
        env = os.environ.copy()
        env["WINEPREFIX"] = str(Path(compat_data) / "pfx")
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        subprocess.run(
            [str(bin_dir / "wineserver"), "-k"],
            env=env, timeout=15,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if log_fn is not None:
            log_fn("tool prefix wineserver shut down")
    except Exception:
        pass


class ProtonPrefixStepMixin:
    """Choose-Proton-Version wizard step + tool prefix env resolution.

    Host class must provide _game, _exe, _body, _log, _set_label, _clear_body,
    _on_close_cb, and set the _tool_* class attributes below. Wire the step in
    by routing to _show_step_proton and overriding _proton_next_step.
    """

    _tool_exe_name: str = ""
    _tool_display_name: str = "This tool"
    _proton_step_title: str = "Choose Proton Version"
    _exe_missing_text: str = "The tool's exe was not found.\nReopen this wizard."
    _proton_deps_note: str = (
        "Each version gets its own prefix; dependencies are installed "
        "into it automatically on the next step."
    )
    _proton_name: str = ""

    def _proton_next_step(self):
        raise NotImplementedError

    def _safe_after(self, delay: int, fn):
        def _run():
            try:
                if self.winfo_exists():
                    fn()
            except Exception:
                pass
        self.after(delay, _run)

    def _get_tool_env(self):
        """Resolve (proton_script, env, compat_data) for the tool's own prefix.

        Creates and initialises prefix_<ProtonName>/ next to the exe on first
        use (synchronous wineboot) — only call from a worker thread.
        """
        if self._exe is None:
            return None, None, None
        from gui.dialogs import _get_tool_prefix_env
        name = self._proton_name or load_saved_proton(self._game, self._tool_exe_name)
        result = _get_tool_prefix_env(self._exe, name)
        if result is None:
            return None, None, None
        proton_script, compat_data, env = result
        return proton_script, env, compat_data

    def _link_plugins_txt(self, pfx: Path):
        """Symlink the deployed profile's plugins.txt into the tool prefix.

        No-op for games without the Bethesda plugins.txt machinery.
        """
        game = self._game
        if not hasattr(game, "_symlink_plugins_txt"):
            return
        profile = ""
        try:
            profile = game.get_last_deployed_profile() or ""
        except Exception:
            pass
        try:
            game._symlink_plugins_txt(
                profile or "default",
                lambda msg: self._log(f"{self._tool_display_name} Wizard: {msg}"),
                prefix_root=pfx,
            )
        except Exception as exc:
            self._log(f"{self._tool_display_name} Wizard: plugins.txt link failed: {exc}")

    def _link_mygames(self, pfx: Path):
        """Symlink the game prefix's My Games/<Game> dir into the tool prefix.

        Gives tools that read the game INIs (xEdit needs Skyrim.ini or it
        exits with a fatal error) the same files the game itself uses.
        """
        game = self._game
        game_pfx = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
        docs = getattr(game, "_MYGAMES_DOCS", None)
        sub  = getattr(game, "_MYGAMES_SUBPATH", None)
        if game_pfx is None or docs is None or sub is None:
            return
        src = game_pfx / docs / sub
        if not src.is_dir():
            self._log(
                f"{self._tool_display_name} Wizard: game-prefix My Games folder "
                f"not found ({src}) — skipping link."
            )
            return
        dst = pfx / docs / sub
        if dst.is_symlink() or dst.exists():
            return
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src, target_is_directory=True)
            self._log(f"{self._tool_display_name} Wizard: linked My Games → {dst}")
        except OSError as exc:
            self._log(f"{self._tool_display_name} Wizard: My Games link failed: {exc}")

    # ------------------------------------------------------------------
    # Choose Proton version step
    # ------------------------------------------------------------------

    def _show_step_proton(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text=self._proton_step_title,
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        if self._exe is None:
            ctk.CTkLabel(
                self._body, text=self._exe_missing_text,
                font=FONT_NORMAL, text_color="#e06c6c", justify="center", wraplength=460,
            ).pack(pady=(0, 16))
            ctk.CTkButton(
                self._body, text="Close", width=120, height=36,
                font=FONT_BOLD,
                fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
                command=self._on_close_cb,
            ).pack(side="bottom")
            return

        from Utils.steam_finder import find_proton_for_game, list_installed_proton
        versions = [p.parent.name for p in list_installed_proton()]
        if not versions:
            ctk.CTkLabel(
                self._body,
                text=(
                    "No Proton versions were found.\n\n"
                    "Install a Proton version in Steam, then reopen this wizard."
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

        ctk.CTkLabel(
            self._body,
            text=(
                f"{self._tool_display_name} runs in its own Wine prefix, stored next to its "
                "exe and separate from the game's prefix, so you can pick any "
                "Proton version without affecting the game.\n\n"
                + self._proton_deps_note
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 20))

        # Default: saved per-exe override, else the game's own Proton version.
        saved = load_saved_proton(self._game, self._tool_exe_name)
        if not saved:
            steam_id = getattr(self._game, "steam_id", "")
            script = find_proton_for_game(steam_id) if steam_id else None
            if script is not None:
                saved = script.parent.name
        initial = saved if saved in versions else None
        if initial is None and saved:
            saved_lower = saved.lower()
            initial = next((v for v in versions if v.lower().startswith(saved_lower)), None)
        if initial is None:
            initial = versions[0]

        row = ctk.CTkFrame(self._body, fg_color="transparent")
        row.pack(pady=(0, 6))

        self._proton_menu = ctk.CTkOptionMenu(
            row, values=versions, width=280,
            font=FONT_NORMAL, fg_color=BG_PANEL, text_color=TEXT_MAIN,
            button_color=BG_HEADER, button_hover_color="#3d3d3d",
            dropdown_fg_color=BG_PANEL, dropdown_text_color=TEXT_MAIN,
            command=lambda _v: self._update_prefix_delete_state(),
        )
        self._proton_menu.set(initial)
        self._proton_menu.pack(side="left")

        self._delete_prefix_btn = ctk.CTkButton(
            row, text="Delete Prefix", width=110, height=28,
            font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color="#7a2d2d", text_color=TEXT_MAIN,
            command=self._on_delete_prefix,
        )
        self._delete_prefix_btn.pack(side="left", padx=(8, 0))

        self._prefix_status = ctk.CTkLabel(
            self._body, text="",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._prefix_status.pack(pady=(0, 8))

        self._update_prefix_delete_state()

        ctk.CTkButton(
            self._body, text="Continue", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_proton_chosen,
        ).pack(side="bottom", pady=(8, 0))

    def _on_proton_chosen(self):
        self._proton_name = self._proton_menu.get()
        save_saved_proton(self._game, self._tool_exe_name, self._proton_name)
        self._log(
            f"{self._tool_display_name} Wizard: using {self._proton_name} "
            "with an isolated prefix next to the exe."
        )
        self._proton_next_step()

    # ------------------------------------------------------------------
    # Delete Prefix button
    # ------------------------------------------------------------------

    def _selected_prefix_dir(self) -> Path | None:
        if self._exe is None:
            return None
        name = self._proton_menu.get().strip()
        if not name:
            return None
        return self._exe.parent / f"prefix_{name}"

    def _update_prefix_delete_state(self):
        self._confirm_delete = False
        d = self._selected_prefix_dir()
        exists = d is not None and d.is_dir()
        try:
            self._delete_prefix_btn.configure(
                text="Delete Prefix", fg_color=BG_HEADER, hover_color="#7a2d2d",
                state="normal" if exists else "disabled",
            )
        except Exception:
            pass
        self._set_label(
            "_prefix_status",
            f"A prefix already exists for this version. Delete it if {self._tool_display_name}\n"
            "has issues — it is recreated automatically on the next step."
            if exists else "",
        )

    def _on_delete_prefix(self):
        d = self._selected_prefix_dir()
        if d is None or not d.is_dir():
            self._update_prefix_delete_state()
            return
        if not self._confirm_delete:
            self._confirm_delete = True
            self._delete_prefix_btn.configure(
                text="Confirm Delete", fg_color="#7a2d2d", hover_color="#9e3a3a",
            )
            self._set_label("_prefix_status", f"Click again to delete '{d.name}'.")
            return
        self._confirm_delete = False
        self._delete_prefix_btn.configure(state="disabled", text="Deleting…")
        self._set_label("_prefix_status", f"Deleting '{d.name}'…")
        threading.Thread(target=lambda: self._do_delete_prefix(d), daemon=True).start()

    def _do_delete_prefix(self, d: Path):
        import shutil
        try:
            if not d.name.startswith("prefix_"):
                raise RuntimeError(f"refusing to delete non-prefix dir: {d}")
            shutil.rmtree(d)
            self._log(f"{self._tool_display_name} Wizard: deleted prefix {d}")
            self._set_label(
                "_prefix_status",
                "Prefix deleted — a fresh one is created on the next step.",
                color="#6bc76b",
            )
        except Exception as exc:
            self._set_label("_prefix_status", f"Could not delete prefix: {exc}", color="#e06c6c")
            self._log(f"{self._tool_display_name} Wizard: prefix delete error: {exc}")
        def _reset():
            try:
                self._delete_prefix_btn.configure(
                    text="Delete Prefix", fg_color=BG_HEADER, hover_color="#7a2d2d",
                    state="normal" if d.is_dir() else "disabled",
                )
            except Exception:
                pass
        self._safe_after(0, _reset)

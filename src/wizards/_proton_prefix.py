"""
_proton_prefix.py
Shared wizard support for running tools in a Proton prefix.

Three prefix placements (chosen per-exe on the Choose-Proton step):

  * isolated (default) — prefix_<ProtonName>/ next to the tool exe.
  * shared              — wine_prefixes/shared_<ProtonName>/ in the app config,
                          one per Proton version, reused by every wizard tool
                          that ticks "Use shared prefix".
  * game                — the game's own prefix (no new prefix created; the
                          Proton version follows the game's Steam setting).

The chosen Proton version persists as the per-exe override
(__proton_override_<exe>) shared with the Mod Files exe launcher; the
placement persists as __prefix_mode_<exe>. Both live in the game's
exe_launch_mode.json. The isolated/shared prefixes are created and initialised
by gui.dialogs._get_tool_prefix_env.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from pathlib import Path

import customtkinter as ctk

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_HEADER, BG_PANEL, BORDER,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)

_LAUNCH_MODE_FILE = "exe_launch_mode.json"
_LAUNCH_ENV_FILE = "launch_env.json"
_ENV_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# Prefix-placement modes persisted per-exe alongside the Proton override.
PREFIX_MODE_ISOLATED = "isolated"  # prefix_<Proton>/ next to the exe (default)
PREFIX_MODE_SHARED = "shared"      # wine_prefixes/shared_<Proton>/, one per Proton
PREFIX_MODE_GAME = "game"          # reuse the game's own prefix


def shared_prefix_dir(proton_dir_name: str) -> Path:
    """Return the shared tool prefix dir for a Proton version (one per version).

    Lives under the app config ``wine_prefixes/`` folder so it is shared by
    every wizard tool that opts into the shared prefix and survives Clear Cache.
    """
    from Utils.config_paths import get_wine_prefixes_dir
    return get_wine_prefixes_dir() / f"shared_{proton_dir_name}"


def load_prefix_mode(game, exe_name: str) -> str:
    """Return the saved prefix-placement mode for exe_name (isolated default)."""
    from Utils.config_paths import get_game_config_dir
    p = get_game_config_dir(game.name) / _LAUNCH_MODE_FILE
    try:
        val = json.loads(p.read_text(encoding="utf-8")).get(f"__prefix_mode_{exe_name}")
    except (OSError, ValueError):
        val = None
    return val if val in (PREFIX_MODE_SHARED, PREFIX_MODE_GAME) else PREFIX_MODE_ISOLATED


def save_prefix_mode(game, exe_name: str, mode: str) -> None:
    """Persist the prefix-placement mode for exe_name."""
    from Utils.config_paths import get_game_config_dir
    p = get_game_config_dir(game.name) / _LAUNCH_MODE_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        data = {}
    key = f"__prefix_mode_{exe_name}"
    if mode in (PREFIX_MODE_SHARED, PREFIX_MODE_GAME):
        data[key] = mode
    else:
        data.pop(key, None)
    try:
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_tool_launch_env(exe: "Path | None") -> str:
    """Return the saved env-var string for this exe ('' if none)."""
    if exe is None:
        return ""
    p = exe.parent / _LAUNCH_ENV_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(exe.name) or ""
    except (OSError, ValueError):
        return ""


def save_tool_launch_env(exe: "Path | None", text: str) -> None:
    """Persist the env-var string in launch_env.json next to the exe."""
    if exe is None:
        return
    p = exe.parent / _LAUNCH_ENV_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        data = {}
    if text:
        data[exe.name] = text
    else:
        data.pop(exe.name, None)
    try:
        if data:
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        elif p.is_file():
            p.unlink()
    except OSError:
        pass


def parse_env_overrides(text: str) -> dict:
    """Parse a space-separated KEY=VALUE string into a dict (bad tokens skipped)."""
    import shlex
    text = (text or "").strip()
    if not text:
        return {}
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    out: dict = {}
    for token in tokens:
        if _ENV_VAR_RE.match(token):
            k, v = token.split("=", 1)
            out[k] = v
    return out


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


def link_plugins_txt(game, pfx: Path, log_fn) -> None:
    """Symlink the deployed profile's plugins.txt into a tool prefix.

    No-op for games without the Bethesda plugins.txt machinery.
    """
    if not hasattr(game, "_symlink_plugins_txt"):
        return
    profile = ""
    try:
        profile = game.get_last_deployed_profile() or ""
    except Exception:
        pass
    try:
        game._symlink_plugins_txt(profile or "default", log_fn, prefix_root=pfx)
    except Exception as exc:
        log_fn(f"plugins.txt link failed: {exc}")


def link_mygames(game, pfx: Path, log_fn) -> None:
    """Symlink the game prefix's My Games/<Game> dir into a tool prefix.

    Gives tools that read the game INIs (xEdit needs Skyrim.ini or it
    exits with a fatal error) the same files the game itself uses.
    """
    game_pfx = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    docs = getattr(game, "_MYGAMES_DOCS", None)
    sub  = getattr(game, "_MYGAMES_SUBPATH", None)
    if game_pfx is None or docs is None or sub is None:
        return
    src = game_pfx / docs / sub
    if not src.is_dir():
        log_fn(f"game-prefix My Games folder not found ({src}) — skipping link.")
        return
    dst = pfx / docs / sub
    if dst.is_symlink() or dst.exists():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src, target_is_directory=True)
        log_fn(f"linked My Games → {dst}")
    except OSError as exc:
        log_fn(f"My Games link failed: {exc}")


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


def refresh_topbar_deploy_state(widget):
    """Refresh deploy-dependent UI (profile dropdown colour, framework
    banners) after a wizard deploy."""
    def _apply():
        try:
            app = widget.winfo_toplevel()
        except Exception:
            return
        try:
            app._topbar._update_profile_menu_color()
        except Exception:
            pass
        try:
            app._plugin_panel._refresh_framework_banners()
        except Exception:
            pass
    try:
        widget.after(0, _apply)
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
    _prefix_mode: str = PREFIX_MODE_ISOLATED

    # Set False on host wizards whose prefix is not anchored next to the game's
    # exe (e.g. staged-exe tools) so the "Use game prefix" option is hidden.
    _allow_game_prefix: bool = True

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

    def _refresh_topbar_deploy_state(self):
        refresh_topbar_deploy_state(self)

    def _get_tool_env(self):
        """Resolve (proton_script, env, compat_data) for the tool's own prefix.

        Honours the chosen prefix mode:
          * isolated — creates/initialises prefix_<ProtonName>/ next to the exe
          * shared   — creates/initialises wine_prefixes/shared_<ProtonName>/
          * game     — reuses the game's own prefix (no init)
        First use of an isolated/shared prefix runs a synchronous wineboot —
        only call from a worker thread.
        """
        if self._exe is None:
            return None, None, None

        mode = self._prefix_mode
        if mode == PREFIX_MODE_GAME:
            return self._get_game_prefix_env()

        from gui.dialogs import _get_tool_prefix_env
        name = self._proton_name or load_saved_proton(self._game, self._tool_exe_name)
        target = None
        if mode == PREFIX_MODE_SHARED:
            from Utils.steam_finder import find_any_installed_proton
            proton_script = find_any_installed_proton(name)
            if proton_script is None:
                return None, None, None
            target = shared_prefix_dir(proton_script.parent.name)
        result = _get_tool_prefix_env(self._exe, name, prefix_dir=target)
        if result is None:
            return None, None, None
        proton_script, compat_data, env = result
        extra = parse_env_overrides(load_tool_launch_env(self._exe))
        if extra:
            env.update(extra)
            self._log(
                f"{self._tool_display_name} Wizard: applying saved env vars: "
                + " ".join(f"{k}={v}" for k, v in extra.items())
            )
        return proton_script, env, compat_data

    def _get_game_prefix_env(self):
        """Resolve (proton_script, env, compat_data) for the game's own prefix.

        Reuses the existing game prefix (already initialised by the game), so no
        wineboot is run. Picks the Proton version Steam assigns to the game.
        """
        from Utils.steam_finder import (
            find_proton_for_game, game_steam_id, find_steam_root_for_proton_script,
        )
        pfx = self._game.get_prefix_path() if hasattr(self._game, "get_prefix_path") else None
        if pfx is None or not Path(pfx).is_dir():
            self._log(
                f"{self._tool_display_name} Wizard: game prefix not found — "
                "deploy/launch the game once, or pick a different prefix option."
            )
            return None, None, None
        steam_id = game_steam_id(self._game)
        proton_script = find_proton_for_game(steam_id) if steam_id else None
        if proton_script is None:
            self._log(
                f"{self._tool_display_name} Wizard: could not resolve the game's "
                "Proton version — pick a different prefix option."
            )
            return None, None, None
        steam_root = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            return None, None, None
        compat_data = Path(pfx).parent
        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
        extra = parse_env_overrides(load_tool_launch_env(self._exe))
        if extra:
            env.update(extra)
            self._log(
                f"{self._tool_display_name} Wizard: applying saved env vars: "
                + " ".join(f"{k}={v}" for k, v in extra.items())
            )
        return proton_script, env, compat_data

    def _link_plugins_txt(self, pfx: Path):
        """Symlink the deployed profile's plugins.txt into the tool prefix."""
        link_plugins_txt(
            self._game, pfx,
            lambda msg: self._log(f"{self._tool_display_name} Wizard: {msg}"),
        )

    def _link_mygames(self, pfx: Path):
        """Symlink the game prefix's My Games/<Game> dir into the tool prefix."""
        link_mygames(
            self._game, pfx,
            lambda msg: self._log(f"{self._tool_display_name} Wizard: {msg}"),
        )

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

        from Utils.steam_finder import find_proton_for_game, game_steam_id, list_installed_proton
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
        ).pack(pady=(0, 12))

        # Default: saved per-exe override, else the game's own Proton version.
        saved = load_saved_proton(self._game, self._tool_exe_name)
        if not saved:
            steam_id = game_steam_id(self._game)
            script = find_proton_for_game(steam_id) if steam_id else None
            if script is not None:
                saved = script.parent.name
        initial = saved if saved in versions else None
        if initial is None and saved:
            saved_lower = saved.lower()
            initial = next((v for v in versions if v.lower().startswith(saved_lower)), None)
        if initial is None:
            initial = versions[0]

        # ---- prefix mode checkboxes ----
        mode = load_prefix_mode(self._game, self._tool_exe_name)
        game_pfx_ok = self._game_prefix_available()
        if mode == PREFIX_MODE_GAME and not (self._allow_game_prefix and game_pfx_ok):
            mode = PREFIX_MODE_ISOLATED
        self._prefix_mode = mode
        self._shared_var = ctk.BooleanVar(value=(mode == PREFIX_MODE_SHARED))
        self._game_pfx_var = ctk.BooleanVar(value=(mode == PREFIX_MODE_GAME))

        opts = ctk.CTkFrame(self._body, fg_color="transparent")
        opts.pack(pady=(0, 8))

        self._shared_chk = ctk.CTkCheckBox(
            opts, text="Use shared prefix",
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            variable=self._shared_var, command=self._on_shared_toggle,
        )
        self._shared_chk.pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(
            opts,
            text=(
                "Reuse one prefix (per Proton version) shared by every wizard "
                "tool, kept in the app config folder instead of next to the exe."
            ),
            font=FONT_SMALL, text_color=TEXT_DIM, justify="left", wraplength=440,
        ).pack(anchor="w", padx=(26, 0), pady=(0, 6))

        if self._allow_game_prefix and game_pfx_ok:
            self._game_chk = ctk.CTkCheckBox(
                opts, text="Use game prefix",
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
                variable=self._game_pfx_var, command=self._on_game_pfx_toggle,
            )
            self._game_chk.pack(anchor="w", pady=(0, 2))
            ctk.CTkLabel(
                opts,
                text=(
                    "Run inside the game's own prefix. No new prefix is created "
                    "and the Proton version follows the game's Steam setting."
                ),
                font=FONT_SMALL, text_color=TEXT_DIM, justify="left", wraplength=440,
            ).pack(anchor="w", padx=(26, 0), pady=(0, 0))
        else:
            self._game_chk = None

        row = ctk.CTkFrame(self._body, fg_color="transparent")
        row.pack(pady=(8, 6))
        self._proton_row = row

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

        self._update_proton_row_state()

        ctk.CTkLabel(
            self._body, text="Environment Variables (optional)",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(8, 2))
        ctk.CTkLabel(
            self._body,
            text=(
                "Space-separated KEY=VALUE pairs applied when the tool launches. "
                "Saved next to the exe and reapplied on every run."
            ),
            font=FONT_SMALL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 4))
        self._env_entry = ctk.CTkEntry(
            self._body, width=400, font=FONT_SMALL,
            fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
            placeholder_text="e.g. PROTON_USE_WINED3D=1 WINEDLLOVERRIDES=dinput8=n,b",
        )
        saved_env = load_tool_launch_env(self._exe)
        if saved_env:
            self._env_entry.insert(0, saved_env)
        self._env_entry.pack(pady=(0, 6))
        self._env_entry._entry.bind(
            "<Control-a>",
            lambda e: (self._env_entry._entry.select_range(0, "end"),
                       self._env_entry._entry.icursor("end"), "break")[2],
        )

        ctk.CTkButton(
            self._body, text="Continue", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_proton_chosen,
        ).pack(side="bottom", pady=(8, 0))

    def _game_prefix_available(self) -> bool:
        """True if the game exposes an existing on-disk prefix to reuse."""
        try:
            pfx = self._game.get_prefix_path() if hasattr(self._game, "get_prefix_path") else None
            return pfx is not None and Path(pfx).is_dir()
        except Exception:
            return False

    def _current_prefix_mode(self) -> str:
        if getattr(self, "_game_chk", None) is not None and self._game_pfx_var.get():
            return PREFIX_MODE_GAME
        if self._shared_var.get():
            return PREFIX_MODE_SHARED
        return PREFIX_MODE_ISOLATED

    def _on_shared_toggle(self):
        if self._shared_var.get() and getattr(self, "_game_chk", None) is not None:
            self._game_pfx_var.set(False)
        self._update_proton_row_state()

    def _on_game_pfx_toggle(self):
        if self._game_pfx_var.get():
            self._shared_var.set(False)
        self._update_proton_row_state()

    def _update_proton_row_state(self):
        """The game prefix has its own fixed Proton; grey the picker out then."""
        use_game = (
            getattr(self, "_game_chk", None) is not None and self._game_pfx_var.get()
        )
        try:
            self._proton_menu.configure(state="disabled" if use_game else "normal")
        except Exception:
            pass
        if use_game:
            try:
                self._delete_prefix_btn.configure(state="disabled")
            except Exception:
                pass
            self._set_label(
                "_prefix_status",
                "Using the game's existing prefix — Proton version follows the "
                "game's Steam setting and no new prefix is created.",
            )
        else:
            self._update_prefix_delete_state()

    def _on_proton_chosen(self):
        self._prefix_mode = self._current_prefix_mode()
        self._proton_name = self._proton_menu.get()
        save_saved_proton(self._game, self._tool_exe_name, self._proton_name)
        save_prefix_mode(self._game, self._tool_exe_name, self._prefix_mode)
        try:
            save_tool_launch_env(self._exe, self._env_entry.get().strip())
        except Exception:
            pass
        if self._prefix_mode == PREFIX_MODE_GAME:
            self._log(
                f"{self._tool_display_name} Wizard: using the game's own prefix."
            )
        elif self._prefix_mode == PREFIX_MODE_SHARED:
            self._log(
                f"{self._tool_display_name} Wizard: using {self._proton_name} "
                "with a shared prefix in the app config folder."
            )
        else:
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
        if self._shared_var.get():
            return shared_prefix_dir(name)
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
            if not (d.name.startswith("prefix_") or d.name.startswith("shared_")):
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

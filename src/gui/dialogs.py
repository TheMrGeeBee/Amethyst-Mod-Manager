"""
Modal dialogs used by ModListPanel, PluginPanel, TopBar, and install_mod.
Uses theme, path_utils; does not import panels or App to avoid circular imports.
"""

import colorsys
from itertools import count
import json
import os
import re
import sys
import shutil
import subprocess
import threading
import tkinter as tk
import tkinter.messagebox
import tkinter.ttk as ttk
from pathlib import Path
from types import SimpleNamespace

from PIL import Image as _PilImage, ImageDraw as _PilDraw, ImageTk as _PilTk

import customtkinter as ctk

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BG_ROW,
    BORDER,
    FONT_BOLD,
    FONT_HEADER,
    FONT_MONO,
    FONT_NORMAL,
    FONT_SMALL,
    font_sized_px,
    scaled,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_MUTED,
    TEXT_SEP,
    TEXT_WHITE,
    TEXT_BLACK,
    BG_SELECT,
    BG_ROW_ALT,
    BG_OVERLAY_ERR,
    BG_OVERLAY_DEEP,
    STATUS_ERR_BRIGHT,
    STATUS_SUCCESS_SOLID,
    BTN_CANCEL,
    BTN_CANCEL_HOV,
    BTN_SUCCESS,
    BTN_SUCCESS_HOV,
    BTN_DANGER_ALT,
    BTN_DANGER_ALT_HOV,
    BTN_INFO_DEEP,
    BTN_INFO_DEEP_HOV,
    BTN_WARN_ORANGE,
    BTN_WARN_ORANGE_HOV,
    TONE_GREEN,
    TONE_RED,
    TONE_BLUE,
    BG_GREEN_TEXT,
    BG_RED_TEXT,
    TAG_BSA_ALT,
    FONT_FAMILY,
    TK_FONT_BOLD, TK_FONT_NORMAL, TK_FONT_SMALL,
)
import gui.theme as _theme
from gui.path_utils import _to_wine_path
from Utils.config_paths import get_exe_args_path, get_profile_exe_args_path, get_custom_game_images_dir, get_dotnet_cache_dir, get_custom_games_dir, get_config_dir

from gui.ctk_components import CTkAlert, CTkLoader, ICON_PATH
from gui.tk_tooltip import TkTooltip
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT, bind_scrollable_wheel
from Utils.xdg import xdg_open, open_url


def _resolve_exe_args_file(game) -> "Path":
    """Return the exe_args.json path to use for *game*'s active profile.

    For profiles with the ``profile_specific_mods`` flag the args are stored
    inside the profile directory so each profile can have independent tool
    output paths.  All other profiles share the global exe_args.json.
    """
    from pathlib import Path as _Path
    try:
        active_dir = getattr(game, "_active_profile_dir", None)
        if active_dir is not None:
            from gui.game_helpers import profile_uses_specific_mods  # type: ignore
            if profile_uses_specific_mods(active_dir):
                return get_profile_exe_args_path(_Path(active_dir))
    except Exception:
        pass
    return get_exe_args_path()


# ---------------------------------------------------------------------------
# Themed message helpers (replaces tk.messagebox which ignores dark theme)
# ---------------------------------------------------------------------------

def _center_dialog(dlg, parent, w: int, h: int | None = None):
    """Position dlg centered over parent, on the same monitor.

    Call *after* all widgets are packed so reqheight() is accurate when h=None.
    The dialog is withdrawn before geometry is set and deiconified after, which
    prevents it from briefly appearing on the wrong monitor.
    """
    dlg.withdraw()
    try:
        # CTkToplevel.geometry() scales W/H by window scaling but passes x/y
        # through unscaled, so w/h are design units while x/y are physical px.
        try:
            scale = float(dlg._get_window_scaling()) or 1.0
        except Exception:
            scale = 1.0
        dlg.update_idletasks()
        if h is None:
            # winfo_reqheight() returns physical px; convert to design units
            # so CTk doesn't scale the value a second time.
            h = round(dlg.winfo_reqheight() / scale)
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        x = px + (pw - round(w * scale)) // 2
        y = py + (ph - round(h * scale)) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        dlg.geometry(f"{w}x{h}" if h else f"{w}x200")
    dlg.deiconify()


def ask_yes_no(parent, message: str, title: str = "Confirm") -> bool:
    """Yes/No confirmation dialog using CTkAlert. Returns True if Yes clicked."""
    alert = CTkAlert(
        state="warning",
        title=title,
        body_text=message,
        btn1="Yes",
        btn2="No",
        parent=parent,
        width=520,
    )
    return alert.get() == "Yes"


def show_error(title: str, message: str, parent=None) -> None:
    """Error dialog using CTkAlert."""
    alert = CTkAlert(
        state="error",
        title=title,
        body_text=message,
        btn1="OK",
        btn2="",
        parent=parent,
        width=520,
    )
    alert.get()


def show_warning(title: str, message: str, parent=None, height: int = 220) -> None:
    """Warning dialog using CTkAlert.

    ``height`` is the design height; CTkAlert auto-sizes to content but caps at
    2×, so raise it for long bodies to keep the OK button from being clipped.
    """
    alert = CTkAlert(
        state="warning",
        title=title,
        body_text=message,
        btn1="OK",
        btn2="",
        parent=parent,
        width=520,
        height=height,
    )
    alert.get()


def confirm_deploy_appdata(parent, game) -> bool:
    """Warn the user if the game's in-prefix AppData folder is missing.

    Used before deploys that symlink plugins.txt into the Proton prefix —
    if the AppData subdir doesn't exist, the symlink target's parent will
    be created blindly, which usually means the game has never been run
    in the prefix and the symlink may not actually be picked up.

    Returns True to proceed, False if the user cancels.  Returns True for
    games without a configured _APPDATA_SUBPATH or prefix path.
    """
    appdata_sub = getattr(game, "_APPDATA_SUBPATH", None)
    if appdata_sub is None:
        return True
    # The warning is only meaningful for games that symlink plugins.txt into
    # the prefix. Games with no plugins.txt (e.g. Fallout 76, which loads mods
    # purely via sResourceArchive2List) have nothing for the missing AppData
    # folder to break — skip the prompt.
    targets_fn = getattr(game, "_plugins_txt_targets", None)
    if callable(targets_fn):
        try:
            if not targets_fn():
                return True
        except Exception:
            pass
    prefix = None
    try:
        prefix = game.get_prefix_path()
    except Exception:
        return True
    if prefix is None:
        return True
    # Let the game resolve its plugins.txt target (handles per-store variants
    # like Skyrim SE GOG's separate AppData folder). Fall back to the bare
    # _APPDATA_SUBPATH if the game doesn't expose a target resolver.
    appdata_dir: Path | None = None
    resolver = getattr(game, "_plugins_txt_target", None)
    if callable(resolver):
        try:
            target = resolver()
        except Exception:
            target = None
        if target is not None:
            appdata_dir = Path(target).parent
    if appdata_dir is None:
        appdata_dir = Path(prefix) / appdata_sub
    if appdata_dir.is_dir():
        return True
    alert = CTkAlert(
        state="warning",
        title="AppData folder missing",
        body_text=(
            f"The game's AppData folder was not found at:\n\n{appdata_dir}\n\n"
            "This usually means the game has not been launched in the Proton "
            "prefix yet, so the plugins.txt symlink may not be picked up.\n\n"
            "Deploy anyway?"
        ),
        btn1="Deploy anyway",
        btn2="Cancel",
        parent=parent,
        width=560,
    )
    return alert.get() == "Deploy anyway"


def confirm_cet_symlink(parent, game) -> bool:
    """Warn when Cyber Engine Tweaks is staged but deploy mode is symlink.

    CET's ASI loader refuses to load symlinked `cyber_engine_tweaks.asi`,
    so the mod silently fails. Scans the effective filemap for the asi and
    only warns for Cyberpunk 2077 with SYMLINK mode.

    Returns True to proceed, False if the user cancels.
    """
    if getattr(game, "name", "") != "Cyberpunk 2077":
        return True
    try:
        from Utils.deploy import LinkMode
        if not hasattr(game, "get_deploy_mode"):
            return True
        if game.get_deploy_mode() != LinkMode.SYMLINK:
            return True
    except Exception:
        return True
    try:
        filemap_path = game.get_effective_filemap_path()
    except Exception:
        return True
    if not filemap_path or not Path(filemap_path).is_file():
        return True
    found = False
    try:
        with Path(filemap_path).open(encoding="utf-8") as f:
            for line in f:
                if "\t" not in line:
                    continue
                rel_str, _ = line.rstrip("\n").split("\t", 1)
                if rel_str.lower().endswith("cyber_engine_tweaks.asi"):
                    found = True
                    break
    except Exception:
        return True
    if not found:
        return True
    alert = CTkAlert(
        state="warning",
        title="Cyber Engine Tweaks requires Hardlink mode",
        body_text=(
            "Cyber Engine Tweaks is enabled, but the deploy mode is set to "
            "Symlink.\n\nCET will not load from a symlinked "
            "cyber_engine_tweaks.asi — switch the deploy mode to Hardlink "
            "for CET to work.\n\nDeploy anyway?"
        ),
        btn1="Deploy anyway",
        btn2="Cancel",
        parent=parent,
        width=640,
    )
    return alert.get() == "Deploy anyway"


from gui.text_utils import build_tree_str as _build_tree_str
from gui.text_utils import truncate_text as _truncate_text


# Game picker — extracted to gui/game_picker_dialog.py
from gui.game_picker_dialog import (  # noqa: E402
    _GamePickerDialog,
    GamePickerPanel,
    _CUSTOM_HANDLERS_API_URL,
)


# ---------------------------------------------------------------------------
# PLACEHOLDER — the game picker classes used to live here; they've been moved
# to gui/game_picker_dialog.py.  The re-exports above keep all existing
# ``from gui.dialogs import _GamePickerDialog`` call sites working.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Name mod dialog
# ---------------------------------------------------------------------------
class NameModDialog(ctk.CTkToplevel):
    """
    Modal dialog that lets the user pick/edit the mod name before installing.
    result: str | None — the chosen name, or None if cancelled.
    """

    def __init__(self, parent, suggestions: list[str]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Name Mod")
        self.geometry("480x200")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._suggestions = suggestions

        self._build(suggestions)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self, suggestions: list[str]):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="Mod name:", font=FONT_NORMAL, text_color=TEXT_MAIN,
            anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._entry_var = tk.StringVar(value=suggestions[0] if suggestions else "")
        entry = ctk.CTkEntry(
            self, textvariable=self._entry_var,
            font=FONT_NORMAL, fg_color=BG_PANEL, text_color=TEXT_MAIN,
            border_color=BORDER
        )
        entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 4))
        entry.bind("<Return>", lambda _e: (self._on_ok(), "break")[1])

        if len(suggestions) > 1:
            ctk.CTkLabel(
                self, text="Or choose a suggestion:", font=FONT_SMALL,
                text_color=TEXT_DIM, anchor="w"
            ).grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 2))

            ctk.CTkOptionMenu(
                self, values=suggestions,
                font=FONT_SMALL, fg_color=BG_PANEL, text_color=TEXT_MAIN,
                button_color=BG_HEADER, button_hover_color=BG_HOVER,
                dropdown_fg_color=BG_PANEL, dropdown_text_color=TEXT_MAIN,
                command=lambda v: self._entry_var.set(v)
            ).grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))
            btn_row = 4
        else:
            btn_row = 2

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=btn_row, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=90, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Install", width=90, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok
        ).pack(side="right", padx=4, pady=8)

        self.update_idletasks()
        # reqheight is physical px; CTk geometry() wants design W/H but
        # physical x/y, so convert before mixing.
        scale = self._get_window_scaling() or 1
        req_h = self.winfo_reqheight()
        h = round(req_h / scale)
        owner = self.master
        px = owner.winfo_rootx()
        py = owner.winfo_rooty()
        pw = owner.winfo_width()
        ph = owner.winfo_height()
        x = px + (pw - round(480 * scale)) // 2
        y = py + (ph - req_h) // 2
        self.geometry(f"480x{h}+{x}+{y}")

    def _on_ok(self):
        name = self._entry_var.get().strip()
        if name:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _SeparatorNameDialog(ctk.CTkToplevel):
    """Small modal dialog that asks for a separator name."""

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Add Separator")
        self.geometry("360x130")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
        except Exception:
            pass
        self.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_out(self, _event):
        if self.focus_get() is None:
            self._on_cancel()

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="Separator name:", font=FONT_NORMAL,
            text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._var = tk.StringVar()
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.bind("<Return>", lambda _e: (self._on_ok(), "break")[1])

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Add", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        name = self._var.get().strip()
        if name:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _ModNameDialog(ctk.CTkToplevel):
    """Small modal dialog that asks for a new empty mod name."""

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Create Empty Mod")
        self.geometry("360x130")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
        except Exception:
            pass
        self.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_out(self, _event):
        if self.focus_get() is None:
            self._on_cancel()

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="Mod name:", font=FONT_NORMAL,
            text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._var = tk.StringVar()
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.bind("<Return>", lambda _e: (self._on_ok(), "break")[1])

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Create", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        name = self._var.get().strip()
        if name:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _RenameDialog(ctk.CTkToplevel):
    """Small modal dialog pre-filled with the current name for renaming a mod or separator."""

    def __init__(self, parent, current_name: str):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Rename")
        self.geometry("360x130")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._current = current_name
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
            self._entry.select_range(0, "end")
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="New name:", font=FONT_NORMAL,
            text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._var = tk.StringVar(value=self._current)
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.bind("<Return>", lambda _e: (self._on_ok(), "break")[1])

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Rename", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        name = self._var.get().strip()
        if name:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Post-install rename prompt
# ---------------------------------------------------------------------------
class _RenameAfterInstallDialog(ctk.CTkToplevel):
    """Dialog shown after a mod install (when the option is enabled) letting the
    user pick a cleaner name for the installed mod. A dropdown of suggestions
    is pre-populated by stripping metadata from the original archive stem."""

    def __init__(self, parent, current_name: str, suggestions: list[str]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Rename Mod")
        self.geometry("420x190")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None
        self._current = current_name
        # De-duplicate while preserving order, always including the current name.
        seen: set[str] = set()
        self._suggestions: list[str] = []
        for s in ([current_name] + list(suggestions or [])):
            if s and s not in seen:
                seen.add(s)
                self._suggestions.append(s)
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
            self._entry.select_range(0, "end")
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text=f"Rename '{self._current}':", font=FONT_NORMAL,
            text_color=TEXT_MAIN, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._var = tk.StringVar(value=self._suggestions[0] if self._suggestions else self._current)
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER,
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        self._entry.bind("<Return>", lambda _e: (self._on_ok(), "break")[1])

        ctk.CTkLabel(
            self, text="Suggestions:", font=FONT_SMALL,
            text_color=TEXT_DIM, anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=16, pady=(2, 2))

        self._menu = ctk.CTkOptionMenu(
            self, values=self._suggestions or [self._current],
            command=self._on_pick,
            fg_color=BG_PANEL, button_color=BG_HEADER, button_hover_color=BG_HOVER,
            text_color=TEXT_MAIN, font=FONT_NORMAL,
        )
        self._menu.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._menu.set(self._suggestions[0] if self._suggestions else self._current)

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=4, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Skip", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Rename", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=8)

    def _on_pick(self, value: str):
        self._var.set(value)
        try:
            self._entry.focus_set()
            self._entry.select_range(0, "end")
        except Exception:
            pass

    def _on_ok(self):
        name = self._var.get().strip()
        if name and name != self._current:
            self.result = name
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


# Module-level queue so multiple post-install rename prompts do not stack
# on top of each other. If an install completes while the dialog is open,
# the rename request is queued and processed once the current dialog closes.
_rename_after_install_queue: list[tuple] = []
_rename_after_install_active: bool = False
_rename_after_install_lock = threading.Lock()


def queue_rename_after_install(parent_window, mod_panel, mod_name: str,
                               suggestions: list[str]) -> None:
    """Enqueue a post-install rename prompt. Safe to call from any thread.

    Ensures only one rename dialog is visible at a time — further installs
    that complete while a dialog is open are queued and processed in order.
    """
    global _rename_after_install_active
    if parent_window is None or mod_panel is None or not mod_name:
        return
    with _rename_after_install_lock:
        _rename_after_install_queue.append((parent_window, mod_panel, mod_name, list(suggestions or [])))
        if _rename_after_install_active:
            return
        _rename_after_install_active = True
    try:
        parent_window.after(0, _process_next_rename_after_install)
    except Exception:
        with _rename_after_install_lock:
            _rename_after_install_active = False


def _process_next_rename_after_install() -> None:
    global _rename_after_install_active
    with _rename_after_install_lock:
        if not _rename_after_install_queue:
            _rename_after_install_active = False
            return
        parent_window, mod_panel, mod_name, suggestions = _rename_after_install_queue.pop(0)

    def _schedule_next():
        try:
            parent_window.after(0, _process_next_rename_after_install)
        except Exception:
            with _rename_after_install_lock:
                globals()["_rename_after_install_active"] = False

    try:
        # Verify the mod is still present before prompting (user may have
        # removed it in the meantime).
        rename_fn = getattr(mod_panel, "rename_mod_by_name", None)
        dlg = _RenameAfterInstallDialog(parent_window, mod_name, suggestions)
        parent_window.wait_window(dlg)
        new_name = dlg.result
        if new_name and rename_fn is not None:
            try:
                rename_fn(mod_name, new_name)
            except Exception:
                pass
    finally:
        _schedule_next()


class _PriorityDialog(ctk.CTkToplevel):
    """Modal dialog to set a mod's position in the modlist."""

    def __init__(self, parent, mod_name: str, total_mods: int):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Set Priority")
        self.geometry("380x160")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: int | None = None
        self._mod_name = mod_name
        self._total_mods = total_mods
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
            self._entry.select_range(0, "end")
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self,
            text=f"Set position for '{self._mod_name}'",
            font=FONT_NORMAL,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        ctk.CTkLabel(
            self,
            text=f"0 = bottom, highest number = top (e.g. {self._total_mods - 1} or higher = top).",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
            anchor="w",
            justify="left",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))

        self._var = tk.StringVar(value="")
        self._entry = ctk.CTkEntry(
            self,
            textvariable=self._var,
            font=FONT_NORMAL,
            fg_color=BG_PANEL,
            text_color=TEXT_MAIN,
            border_color=BORDER,
        )
        self._entry.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.bind("<Return>", lambda _e: (self._on_ok(), "break")[1])

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar,
            text="Cancel",
            width=80,
            height=28,
            font=FONT_NORMAL,
            fg_color=BG_HEADER,
            hover_color=BG_HOVER,
            text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar,
            text="Set",
            width=80,
            height=28,
            font=FONT_BOLD,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            text_color=TEXT_ON_ACCENT,
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        raw = self._var.get().strip()
        try:
            value = int(raw)
        except ValueError:
            show_error(
                "Invalid Value",
                "Please enter a whole number.",
                parent=self,
            )
            return
        if value < 0:
            show_error(
                "Invalid Value",
                "Please enter 0 or a positive number.",
                parent=self,
            )
            return
        self.result = value
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _on_cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class _DotNetVersionPanel(ctk.CTkFrame):
    """Inline overlay panel that asks which .NET version to install."""

    _VERSIONS = [
        ("10 (latest)", "10"),
        ("9", "9"),
        ("8 (LTS)", "8"),
        ("7", "7"),
        ("6 (LTS)", "6"),
        ("5", "5"),
    ]

    def __init__(self, parent, on_pick):
        """``on_pick(version: str | None)`` is called when the user selects a
        version or cancels (``None`` on cancel)."""
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_pick = on_pick
        self._build()

    def _build(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(
            title_bar, text="Install .NET — select version",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._cancel,
        ).pack(side="right", padx=4, pady=4)

        # Body
        body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        body.grid(row=1, column=0, sticky="nsew")

        inner = ctk.CTkFrame(body, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        btn_cfg = dict(width=260, height=34, font=FONT_BOLD,
                       fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT)

        for label, ver in self._VERSIONS:
            ctk.CTkButton(
                inner, text=f".NET {label}",
                command=lambda v=ver: self._pick(v),
                **btn_cfg,
            ).pack(pady=(0, 6))

    def _pick(self, version: str):
        self._dismiss()
        self._on_pick(version)

    def _cancel(self):
        self._dismiss()
        self._on_pick(None)

    def _dismiss(self):
        try:
            self.place_forget()
            self.destroy()
        except Exception:
            pass


class _ProtonToolsDialog(ctk.CTkToplevel):
    """Thin modal wrapper around ProtonToolsPanel."""

    def __init__(self, parent, game, log_fn):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Proton Tools")
        self.geometry("380x460")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        panel = ProtonToolsPanel(
            self, game, log_fn,
            on_done=lambda p: self._on_close(),
        )
        panel.grid(row=0, column=0, sticky="nsew")

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class ProtonToolsPanel(ctk.CTkFrame):
    """Inline panel for Proton tools — overlays the plugin panel while open."""

    def __init__(self, parent, game, log_fn, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._game = game
        self._log = log_fn
        self._on_done = on_done or (lambda p: None)
        self._build()

    def _build(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Proton Tools — {self._game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4, pady=4)

        # Body — scrollable so the categorised button list never gets clipped
        # on short windows / high UI scales.
        body = ctk.CTkScrollableFrame(self, fg_color=BG_DEEP, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(body, fg_color="transparent")
        inner.pack(anchor="center", pady=(14, 14))

        btn_cfg = dict(width=260, height=34, font=FONT_BOLD,
                       fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT)

        def _category(label: str, first: bool = False) -> None:
            ctk.CTkLabel(
                inner, text=label.upper(), font=FONT_SMALL,
                text_color=TEXT_MUTED, anchor="w",
            ).pack(anchor="w", fill="x", pady=((0 if first else 14), 4))
            ctk.CTkFrame(inner, fg_color=BORDER, height=1).pack(
                anchor="w", fill="x", pady=(0, 8))

        # --- Prefix tools ---------------------------------------------------
        _category("Prefix Tools", first=True)
        ctk.CTkButton(inner, text="Run winecfg",              command=self._run_winecfg,             **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Run protontricks",         command=self._run_protontricks,        **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Run EXE in this prefix …", command=self._run_exe,                 **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Open wine registry",       command=self._run_regedit,             **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Wine DLL Overrides",       command=self._open_wine_dll_overrides, **btn_cfg).pack(pady=(0, 6))

        # --- Installers -----------------------------------------------------
        _category("Installers")
        ctk.CTkButton(inner, text="Install VC++ Redistributable", command=self._run_install_vcredist,       **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Install d3dcompiler_47",       command=self._run_install_d3dcompiler_47, **btn_cfg).pack(pady=(0, 6))
        ctk.CTkButton(inner, text="Install .NET …",              command=self._run_install_dotnet,         **btn_cfg).pack(pady=(0, 6))

        # --- Folders --------------------------------------------------------
        _category("Open Folders")
        for label, opener in (
            ("Game folder",     self._open_game_folder),
            ("Prefix folder",   self._browse_prefix),
            ("My Games folder", self._open_mygames_folder),
            ("AppData folder",  self._open_appdata_folder),
            ("Staging folder",  self._open_staging_folder),
            ("Profile folder",  self._open_profile_folder),
            (".config folder",  self._open_config_folder),
        ):
            ctk.CTkButton(inner, text=label, command=opener, **btn_cfg).pack(pady=(0, 6))

        # On Tk 8.6 (AppImage) X11 wheel notches arrive as Button-4/5, which the
        # scrollable frame doesn't listen for — bind them so the wheel scrolls
        # over the buttons too. The helper re-walks descendants on <Enter>.
        bind_scrollable_wheel(body)

    def _get_proton_env(self):
        from Utils.steam_finder import (
            find_any_installed_proton,
            find_proton_for_game,
            game_steam_id,
            find_steam_root_for_proton_script,
        )
        prefix_path = self._game.get_prefix_path()
        if prefix_path is None or not prefix_path.is_dir():
            self._log("Proton Tools: prefix not configured for this game.")
            return None, None

        steam_id = game_steam_id(self._game)
        proton_script = find_proton_for_game(steam_id) if steam_id else None
        from gui.plugin_panel import _resolve_compat_data, _read_prefix_runner
        compat_data = _resolve_compat_data(prefix_path)

        if proton_script is None:
            # Heroic-managed prefixes have no Steam CompatToolMapping, but the
            # exact Proton build is recorded in GamesConfig/<app>.json — use it.
            try:
                from Utils.heroic_finder import find_heroic_proton_for_prefix
                proton_script = find_heroic_proton_for_prefix(prefix_path)
            except Exception:
                proton_script = None
            if proton_script is not None:
                self._log(f"Proton Tools: using Heroic-configured Proton "
                          f"{proton_script.parent.name}.")

        if proton_script is None:
            preferred_runner = _read_prefix_runner(compat_data)
            proton_script = find_any_installed_proton(preferred_runner)
            if proton_script is None:
                if steam_id:
                    self._log(f"Proton Tools: could not find Proton version for app {steam_id}, "
                              "and no installed Proton tool was found.")
                else:
                    self._log("Proton Tools: no Steam ID and no installed Proton tool was found.")
                return None, None
            self._log(f"Proton Tools: using fallback Proton tool {proton_script.parent.name} "
                      "(no per-game Steam mapping found).")

        steam_root = find_steam_root_for_proton_script(proton_script)
        if steam_root is None:
            self._log("Proton Tools: could not determine Steam root for the selected Proton tool.")
            return None, None

        env = os.environ.copy()
        env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
        game_path = self._game.get_game_path() if hasattr(self._game, "get_game_path") else None
        if game_path:
            env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)
        if steam_id:
            env.setdefault("SteamAppId", steam_id)
            env.setdefault("SteamGameId", steam_id)
        return proton_script, env

    def _close_and_run(self, fn):
        toplevel = self.winfo_toplevel()
        self._on_done(self)
        toplevel.after(50, fn)

    def _open_install_progress(self, title, worker):
        """Mount the InstallProgressPanel overlay on top of this panel and
        run *worker(log_fn) -> bool* in a background thread. The proton-tools
        panel stays mounted underneath, so closing the progress overlay
        returns the user to it."""
        app = self.winfo_toplevel()
        show = getattr(app, "show_install_progress", None)
        log = self._log
        prefixed_log = lambda msg: log(f"Proton Tools: {msg}")
        if show is None:
            self._close_and_run(
                lambda: threading.Thread(
                    target=lambda: worker(prefixed_log), daemon=True,
                ).start()
            )
            return
        show(title, worker, log_fn=prefixed_log)

    def _wine_tool_command(self, proton_script, env, tool):
        """Build a launch command for a wine tool (winecfg/regedit).

        Prefers the Proton dist's bundled ``wine`` binary directly (the way
        Heroic launches these), which avoids booting Proton's steam.exe shim —
        that shim asserts ``!status`` in steamclient_main.c and aborts when it
        can't reach a Steam client (e.g. Steam Flatpak / non-Steam Heroic
        prefix). Falls back to ``proton run`` if no wine binary is found.
        """
        log = self._log
        proton_dir = Path(proton_script).parent
        log(f"Proton Tools: resolving wine binary under {proton_dir}")
        if not proton_dir.is_dir():
            log(f"Proton Tools: WARNING — Proton dir does not exist: {proton_dir}")
        wine_bin = None
        checked = []
        # Prefer wine64: GE-Proton's `wine` is a 32-bit ELF whose missing
        # /lib/ld-linux.so.2 interpreter makes exec fail with ENOENT on
        # hosts without 32-bit glibc.
        for sub in ("files/bin/wine64", "dist/bin/wine64",
                    "files/bin/wine", "dist/bin/wine"):
            cand = proton_dir / sub
            checked.append(str(cand))
            if cand.is_file():
                wine_bin = cand
                break
        if wine_bin is None:
            log("Proton Tools: WARNING — no bundled wine binary found "
                f"(checked: {', '.join(checked)}); falling back to "
                "'proton run', which boots the steam.exe shim and may crash "
                "with an lsteamclient assertion if Steam is unavailable.")
            return ["python3", str(proton_script), "run", tool]
        log(f"Proton Tools: using bundled wine binary {wine_bin}")
        prefix_path = self._game.get_prefix_path()
        if prefix_path is not None:
            env["WINEPREFIX"] = str(prefix_path)
            log(f"Proton Tools: WINEPREFIX set to {prefix_path}")
            if not prefix_path.is_dir():
                log(f"Proton Tools: WARNING — WINEPREFIX path does not exist: {prefix_path}")
        else:
            log("Proton Tools: WARNING — no prefix path for this game; "
                "wine will use its default prefix (~/.wine).")
        return [str(wine_bin), tool]

    def _run_winecfg(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return
        log = self._log
        cmd = self._wine_tool_command(proton_script, env, "winecfg")
        def _launch():
            log("Proton Tools: launching winecfg …")
            try:
                subprocess.Popen(cmd, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"Proton Tools error: {e}")
        self._close_and_run(_launch)

    def _open_folder(self, path, descr):
        """Close the panel and open *path* in the file manager.

        ``path`` may be None / non-existent — in that case we just log why the
        folder couldn't be opened and leave the panel as-is.
        """
        if path is None:
            self._log(f"Proton Tools: {descr} is not configured for this game.")
            return
        path = Path(path)
        if not path.is_dir():
            self._log(f"Proton Tools: {descr} not found ({path}).")
            return
        log = self._log
        spath = str(path)
        def _launch():
            log(f"Proton Tools: opening {descr} …")
            try:
                xdg_open(spath)
            except Exception as e:
                log(f"Proton Tools error: {e}")
        self._close_and_run(_launch)

    def _browse_prefix(self):
        self._open_folder(self._game.get_prefix_path(), "prefix folder")

    def _open_game_folder(self):
        self._open_folder(self._game.get_game_path(), "game folder")

    def _open_mygames_folder(self):
        # Bethesda games expose the exact per-game My Games subpath; for other
        # engines fall back to the generic My Games folder inside the prefix.
        getter = getattr(self._game, "_mygames_path", None)
        path = getter() if callable(getter) else None
        if path is None:
            prefix = self._game.get_prefix_path()
            if prefix is not None:
                path = prefix / "drive_c/users/steamuser/Documents/My Games"
        self._open_folder(path, "My Games folder")

    def _open_appdata_folder(self):
        # The in-prefix per-game AppData/Local folder (Bethesda games expose the
        # exact subpath; otherwise fall back to AppData/Local in the prefix).
        prefix = self._game.get_prefix_path()
        if prefix is None:
            self._open_folder(None, "AppData folder")
            return
        sub = getattr(self._game, "_APPDATA_SUBPATH", None)
        if sub is not None:
            self._open_folder(prefix / sub, "AppData folder")
            return
        self._open_folder(
            prefix / "drive_c/users/steamuser/AppData/Local", "AppData folder")

    def _open_staging_folder(self):
        getter = getattr(self._game, "get_effective_mod_staging_path", None) \
            or getattr(self._game, "get_mod_staging_path", None)
        path = getter() if callable(getter) else None
        self._open_folder(path, "staging folder")

    def _open_profile_folder(self):
        # Prefer the active profile directory; fall back to the profiles/ root.
        path = getattr(self._game, "_active_profile_dir", None)
        if path is None:
            try:
                path = self._game.get_profile_root() / "profiles"
            except Exception:
                path = None
        self._open_folder(path, "profile folder")

    def _open_config_folder(self):
        # The app's top-level config dir: ~/.config/AmethystModManager/
        try:
            path = get_config_dir()
        except Exception:
            path = None
        self._open_folder(path, ".config folder")

    def _run_regedit(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return
        log = self._log
        cmd = self._wine_tool_command(proton_script, env, "regedit")
        def _launch():
            log("Proton Tools: launching wine registry editor …")
            try:
                subprocess.Popen(cmd, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"Proton Tools error: {e}")
        self._close_and_run(_launch)

    def _run_protontricks(self):
        from Utils.protontricks import (
            _bundled_winetricks,
            _get_proton_bin,
            cabextract_installed,
            install_cabextract,
            install_winetricks,
            winetricks_installed,
        )
        from Utils.steam_finder import game_steam_id
        steam_id = game_steam_id(self._game)
        prefix_path = getattr(self._game, "_prefix_path", None)
        log = self._log

        if shutil.which("protontricks") is not None and steam_id:
            cmd = ["protontricks", steam_id, "--gui"]
        elif shutil.which("flatpak") is not None and steam_id and subprocess.run(
            ["flatpak", "info", "com.github.Matoking.protontricks"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            cmd = ["flatpak", "run", "com.github.Matoking.protontricks", steam_id, "--gui"]
        elif prefix_path and prefix_path.is_dir():
            def _launch_winetricks():
                if not winetricks_installed():
                    log("Proton Tools: winetricks not found — downloading …")
                    if not install_winetricks(log_fn=lambda m: log(f"Proton Tools: {m}")):
                        return
                if not cabextract_installed():
                    log("Proton Tools: cabextract not found — downloading a portable copy …")
                    if not install_cabextract(log_fn=lambda m: log(f"Proton Tools: {m}")):
                        return
                wt = _bundled_winetricks()
                env = os.environ.copy()
                env["WINEPREFIX"] = str(prefix_path)
                path_prefix = str(wt.parent)
                proton_bin = _get_proton_bin()
                if proton_bin:
                    path_prefix = proton_bin + os.pathsep + path_prefix
                env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
                log("Proton Tools: launching winetricks GUI …")
                try:
                    subprocess.Popen([str(wt), "--gui"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                except Exception as e:
                    log(f"Proton Tools error: {e}")
            self._close_and_run(_launch_winetricks)
            return
        else:
            self._log("Proton Tools: protontricks is not installed and no prefix path is available.")
            return

        def _launch():
            log(f"Proton Tools: launching protontricks for app {steam_id}: It may take a while to open")
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"Proton Tools error: {e}")
        self._close_and_run(_launch)

    def _run_exe(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return
        log = self._log

        def _on_picked(exe_path):
            if exe_path is None:
                return
            if not exe_path.is_file():
                log(f"Proton Tools: file not found: {exe_path}")
                return
            log(f"Proton Tools: launching {exe_path.name} via {proton_script.parent.name} …")
            try:
                subprocess.Popen(["python3", str(proton_script), "run", str(exe_path)],
                                 env=env, cwd=exe_path.parent,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                log(f"Proton Tools error: {e}")

        def _launch():
            from Utils.portal_filechooser import pick_exe_file
            pick_exe_file("Select EXE to run in this prefix", _on_picked)

        self._close_and_run(_launch)

    def _run_install_vcredist(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return
        prefix_path = getattr(self._game, "_prefix_path", None)

        def _worker(plog):
            from Utils.protontricks import install_vcredist
            return install_vcredist(proton_script, env, log_fn=plog, prefix_path=prefix_path)

        self._open_install_progress("Installing VC++ Redistributable", _worker)

    def _run_install_d3dcompiler_47(self):
        from Utils.protontricks import install_d3dcompiler_47
        from Utils.steam_finder import game_steam_id
        steam_id = game_steam_id(self._game)
        prefix_path = getattr(self._game, "_prefix_path", None)

        def _worker(plog):
            return bool(install_d3dcompiler_47(
                steam_id,
                log_fn=plog,
                prefix_path=prefix_path,
            ))

        self._open_install_progress("Installing d3dcompiler_47", _worker)

    def _run_install_dotnet(self):
        proton_script, env = self._get_proton_env()
        if proton_script is None:
            return

        log = self._log
        container = self.master
        prefix_path = getattr(self._game, "_prefix_path", None)

        def _on_version_picked(version):
            if version is None:
                return
            cache_dir = get_dotnet_cache_dir()
            filename = f"windowsdesktop-runtime-{version}-win-x64.exe"
            cache_path = cache_dir / filename
            _DOTNET_URLS = {
                "5": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/5.0.17/windowsdesktop-runtime-5.0.17-win-x64.exe",
                "6": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/windowsdesktop-runtime-6.0.36-win-x64.exe",
                "7": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/7.0.20/windowsdesktop-runtime-7.0.20-win-x64.exe",
                "8": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/8.0.25/windowsdesktop-runtime-8.0.25-win-x64.exe",
                "9": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/9.0.14/windowsdesktop-runtime-9.0.14-win-x64.exe",
                "10": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/10.0.5/windowsdesktop-runtime-10.0.5-win-x64.exe",
            }
            dl_url = _DOTNET_URLS.get(version)
            if dl_url is None:
                log(f"Proton Tools: no download URL known for .NET {version}.")
                return

            def _worker(plog):
                import urllib.request
                from Utils.protontricks import dotnet_dep_key, mark_dep_installed
                try:
                    if not cache_path.is_file():
                        plog(f"Downloading .NET {version} runtime …")
                        urllib.request.urlretrieve(dl_url, cache_path)
                        plog("Download complete.")
                    else:
                        plog(f"Using cached .NET {version} installer.")
                    plog(f"Installing .NET {version} in game prefix (silent) — may take a few minutes …")
                    proc = subprocess.run(
                        ["python3", str(proton_script), "run",
                         str(cache_path), "/quiet", "/norestart"],
                        env=env, cwd=cache_path.parent,
                    )
                    # 0 = success, 102 = already installed/no-op, 1638 = newer present, 3010 = reboot required
                    ok_codes = {0, 102, 1638, 3010}
                    if proc.returncode in ok_codes:
                        plog(f".NET {version} installed (exit {proc.returncode}).")
                        if prefix_path and Path(prefix_path).is_dir():
                            mark_dep_installed(Path(prefix_path), dotnet_dep_key(version))
                        return True
                    plog(f"Installer exited with code {proc.returncode}.")
                    return False
                except Exception as e:
                    plog(f"Error: {e}")
                    return False

            self._open_install_progress(f"Installing .NET {version}", _worker)

        panel = _DotNetVersionPanel(container, on_pick=_on_version_picked)
        panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        panel.lift()

    def _open_wine_dll_overrides(self):
        app = self.winfo_toplevel()
        show_fn = getattr(app, "show_wine_dll_panel", None)
        game = self._game
        log = self._log
        if show_fn:
            self._on_done(self)
            app.after(10, lambda: show_fn(game, log))
        else:
            from gui.wine_dll_overrides_panel import WineDllOverridesPanel
            self._on_done(self)
            try:
                parent = app._plugin_panel_container
            except AttributeError:
                parent = app
            panel = WineDllOverridesPanel(parent, game, log,
                                          on_done=lambda p: p.place_forget() or p.destroy())
            panel.place(relx=0, rely=0, relwidth=1, relheight=1)
            panel.lift()

    def _on_close(self):
        self._on_done(self)


class _ProfileNameDialog(ctk.CTkToplevel):
    """Small modal dialog that asks for a new profile name."""

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("New Profile")
        self.geometry("360x175")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: tuple[str, bool] | None = None
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
            self._entry.focus_set()
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text="Profile name:", font=FONT_NORMAL,
            text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))

        self._var = tk.StringVar()
        self._entry = ctk.CTkEntry(
            self, textvariable=self._var, font=FONT_NORMAL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.bind("<Return>", lambda _e: (self._on_ok(), "break")[1])

        self._specific_mods_var = tk.BooleanVar(value=False)
        _specific_cb = ctk.CTkCheckBox(
            self,
            text="Use Profile Specific Mods",
            variable=self._specific_mods_var,
            font=FONT_NORMAL,
            text_color=TEXT_MAIN,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            border_color=BORDER,
            checkmark_color="white",
        )
        _specific_cb.grid(row=2, column=0, sticky="w", padx=16, pady=(0, 8))
        self._specific_tooltip = TkTooltip(
            self,
            bg=BG_OVERLAY_ERR,
            fg=STATUS_ERR_BRIGHT,
            font=(_theme.FONT_FAMILY, _theme.FS10),
        )
        self._specific_tooltip.attach(
            _specific_cb,
            "Profiles with this setting use their own mods folders",
            offset_x=scaled(12), offset_y=scaled(12),
        )

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Create", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        name = self._var.get().strip()
        if name:
            self.result = (name, self._specific_mods_var.get())
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


class _MewgenicsDeployChoiceDialog(ctk.CTkToplevel):
    """Thin modal wrapper around MewgenicsDeployChoicePanel.

    Callers access ``result`` (``"steam"`` | ``"repack"`` | ``None``).
    """

    def __init__(self, parent):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Mewgenics — Deploy method")
        self.geometry("420x220")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: str | None = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        def _on_choice(choice):
            self.result = choice
            self._on_cancel()

        panel = MewgenicsDeployChoicePanel(self, on_choice=_on_choice)
        panel.grid(row=0, column=0, sticky="nsew")

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class _MewgenicsLaunchCommandDialog(ctk.CTkToplevel):
    """Thin non-modal wrapper around MewgenicsLaunchCommandPanel."""

    def __init__(self, parent, launch_string: str, modpaths_file=None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Mewgenics — Steam / Lutris launch command")
        self.geometry("560x310")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        panel = MewgenicsLaunchCommandPanel(
            self,
            launch_string=launch_string,
            modpaths_file=modpaths_file,
            on_close=self.destroy,
        )
        panel.grid(row=0, column=0, sticky="nsew")


class MewgenicsDeployChoicePanel(tk.Frame):
    """Inline overlay panel: choose Steam launch command or repack modded files.

    Place this over the mod-list container with::

        panel.place(relx=0, rely=0, relwidth=1, relheight=1)

    ``on_choice(result)`` is called with ``"steam"``, ``"repack"``, or ``None``
    (cancel) and the caller is responsible for destroying the panel.
    """

    def __init__(self, parent, on_choice):
        super().__init__(parent, bg=BG_DEEP)
        self._on_choice = on_choice
        self._build()

    def _build(self):
        # Centred inner card
        card = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")

        # Title bar
        title_bar = tk.Frame(card, bg=BG_HEADER, height=scaled(42))
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        tk.Label(
            title_bar, text="Mewgenics — Deploy method",
            font=font_sized_px(_theme.FONT_FAMILY, 13, "bold"),
            fg=TEXT_MAIN, bg=BG_HEADER, anchor="w",
        ).pack(side="left", padx=scaled(14), pady=scaled(8))
        tk.Button(
            title_bar, text="✕",
            bg=BG_HEADER, fg=TEXT_DIM, activebackground=BG_HOVER,
            activeforeground=TEXT_MAIN, relief="flat", bd=0,
            highlightthickness=0, cursor="hand2",
            font=font_sized_px(_theme.FONT_FAMILY, 12),
            command=lambda: self._on_choice(None),
        ).pack(side="right", padx=scaled(8))

        body = tk.Frame(card, bg=BG_PANEL)
        body.pack(fill="both", padx=scaled(16), pady=scaled(12))

        _lbl_font  = font_sized_px(_theme.FONT_FAMILY, 12)
        _desc_font = font_sized_px(_theme.FONT_FAMILY, 10)

        # --- Steam launch command button ---
        ctk.CTkButton(
            body, text="Steam launch command  (Safer / Recommended)",
            font=_lbl_font, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, anchor="w",
            command=lambda: self._on_choice("steam"),
        ).pack(fill="x", pady=(0, scaled(2)))
        tk.Label(
            body,
            text="Generates a launch script for Steam. Set it once in Launch Options (no repack).",
            font=_desc_font, fg=TEXT_DIM, bg=BG_PANEL, anchor="w",
            wraplength=scaled(420),
        ).pack(fill="x", padx=scaled(4), pady=(0, scaled(10)))

        # --- Repack button ---
        ctk.CTkButton(
            body, text="Repack gpak  (No command needed / not recommended)",
            font=_lbl_font, fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, anchor="w",
            command=lambda: self._on_choice("repack"),
        ).pack(fill="x", pady=(0, scaled(2)))
        tk.Label(
            body,
            text="Unpack resources.gpak, merge mods, repack.",
            font=_desc_font, fg=TEXT_DIM, bg=BG_PANEL, anchor="w",
        ).pack(fill="x", padx=scaled(4), pady=(0, scaled(4)))


class MewgenicsLaunchCommandPanel(tk.Frame):
    """Inline overlay panel: shows the -modpaths launch string with copy button.

    Place over the mod-list container with::

        panel.place(relx=0, rely=0, relwidth=1, relheight=1)

    ``on_close()`` is called when the user clicks Close; caller destroys panel.
    """

    def __init__(self, parent, launch_string: str, modpaths_file=None, on_close=None):
        super().__init__(parent, bg=BG_DEEP)
        self._launch_string = launch_string
        self._modpaths_file = modpaths_file
        self._on_close = on_close or (lambda: None)
        self._build()

    def _build(self):
        card = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=1,
                        highlightbackground=BORDER, highlightcolor=BORDER)
        card.place(relx=0.5, rely=0.5, anchor="center")

        # Title bar
        title_bar = tk.Frame(card, bg=BG_HEADER, height=scaled(42))
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        tk.Label(
            title_bar, text="Mewgenics — Steam / Lutris launch command",
            font=font_sized_px(_theme.FONT_FAMILY, 13, "bold"),
            fg=TEXT_MAIN, bg=BG_HEADER, anchor="w",
        ).pack(side="left", padx=scaled(14), pady=scaled(8))
        tk.Button(
            title_bar, text="✕",
            bg=BG_HEADER, fg=TEXT_DIM, activebackground=BG_HOVER,
            activeforeground=TEXT_MAIN, relief="flat", bd=0,
            highlightthickness=0, cursor="hand2",
            font=font_sized_px(_theme.FONT_FAMILY, 12),
            command=self._on_close,
        ).pack(side="right", padx=scaled(8))

        body = tk.Frame(card, bg=BG_PANEL)
        body.pack(fill="both", padx=scaled(16), pady=(scaled(10), 0))

        _lbl_font  = font_sized_px(_theme.FONT_FAMILY, 11)
        _desc_font = font_sized_px(_theme.FONT_FAMILY, 10)
        _mono_font = font_sized_px("Consolas", 11)

        tk.Label(
            body,
            text="Paste this into Steam Launch Options (Properties → General):",
            font=_lbl_font, fg=TEXT_MAIN, bg=BG_PANEL, anchor="w",
            wraplength=scaled(500),
        ).pack(fill="x", pady=(0, scaled(6)))

        # Monospace textbox
        txt_frame = tk.Frame(body, bg=BG_ROW, bd=1, relief="flat",
                             highlightthickness=1, highlightbackground=BORDER)
        txt_frame.pack(fill="x", pady=(0, scaled(8)))
        txt = tk.Text(
            txt_frame, font=_mono_font, bg=BG_ROW, fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN, relief="flat", bd=0,
            wrap="word", height=4, width=scaled(52),
        )
        txt.pack(fill="both", padx=scaled(6), pady=scaled(6))
        txt.insert("1.0", self._launch_string)
        txt.configure(state="disabled")

        if self._modpaths_file is not None:
            tk.Label(
                body,
                text=(
                    f"Script written to:\n{self._modpaths_file}"
                    "\n\nUpdate this whenever you change your mod list."
                ),
                font=_desc_font, fg=TEXT_DIM, bg=BG_PANEL, anchor="w",
                justify="left", wraplength=scaled(500),
            ).pack(fill="x", pady=(0, scaled(8)))

        # Button bar
        sep = tk.Frame(card, bg=BORDER, height=1)
        sep.pack(fill="x")
        bar = tk.Frame(card, bg=BG_HEADER, height=scaled(48))
        bar.pack(fill="x")
        bar.pack_propagate(False)

        ctk.CTkButton(
            bar, text="Copy to clipboard",
            width=140, height=30,
            font=font_sized_px(_theme.FONT_FAMILY, 11),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._copy,
        ).pack(side="right", padx=(scaled(4), scaled(10)), pady=scaled(8))
        ctk.CTkButton(
            bar, text="Close",
            width=80, height=30,
            font=font_sized_px(_theme.FONT_FAMILY, 11),
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=scaled(4), pady=scaled(8))

    def _copy(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(self._launch_string)
            self.update_idletasks()
        except Exception:
            pass


class _OverwritesDialog(tk.Toplevel):
    """Thin modal wrapper around OverwritesPanel."""

    def __init__(self, parent, mod_name: str,
                 files_win: list[tuple[str, str]],
                 files_lose: list[tuple[str, str]]):
        super().__init__(parent)
        self.title(f"Conflicts: {mod_name}")
        self.geometry("860x580")
        self.minsize(600, 380)
        self.configure(bg=BG_DEEP)
        self.transient(parent)
        self.update_idletasks()
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        panel = OverwritesPanel(
            self,
            mod_name=mod_name,
            files_win=files_win,
            files_lose=files_lose,
            on_done=lambda p: self._on_close(),
        )
        panel.grid(row=0, column=0, sticky="nsew")

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
# OverwritesPanel — inline overlay version of _OverwritesDialog
# ---------------------------------------------------------------------------

# Matches BSA-style row paths produced by modlist_panel: "<archive>.<ext> : <inner>".
_BSA_ROW_RE = re.compile(r"^[^/\\:]+\.(?:bsa|ba2)\s+:\s", re.IGNORECASE)


class OverwritesPanel(ctk.CTkFrame):
    """Full-width overlay (spans mod list + plugin panel) showing conflict
    details for a single mod across three side-by-side panes."""

    def __init__(self, parent, mod_name: str,
                 files_win: list[tuple[str, str]],
                 files_lose: list[tuple[str, str]],
                 files_no_conflict: list[str] | None = None,
                 on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_done = on_done or (lambda p: None)
        files_no_conflict = files_no_conflict or []

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Conflicts: {mod_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Body — left column has win+lose stacked, right column has no-conflicts
        body = tk.Frame(self, bg=BG_DEEP)
        body.pack(fill="both", expand=True)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=2)
        body.grid_columnconfigure(1, weight=3)

        left = tk.Frame(body, bg=BG_DEEP)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_rowconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        self._build_two_col_pane(
            left, row=0, col=0,
            header=f"Files overriding others  ({len(files_win)})",
            header_color=TONE_GREEN,
            col0_title="File path",
            col1_title="Mod(s) beaten",
            rows=files_win,
            pady=(8, 4),
        )
        self._build_two_col_pane(
            left, row=1, col=0,
            header=f"Files overridden by others  ({len(files_lose)})",
            header_color=TONE_RED,
            col0_title="File path",
            col1_title="Winning mod",
            rows=files_lose,
            pady=(4, 8),
        )
        self._build_one_col_pane(
            body, row=0, col=1,
            header=f"Files with no conflicts  ({len(files_no_conflict)})",
            header_color=TONE_BLUE,
            col0_title="File path",
            rows=files_no_conflict,
        )

        footer = tk.Frame(self, bg=BG_PANEL, height=scaled(44))
        footer.pack(fill="x")
        footer.pack_propagate(False)
        tk.Frame(footer, bg=BORDER, height=1).pack(side="top", fill="x")
        ctk.CTkButton(
            footer, text="Close",
            fg_color=BTN_CANCEL, hover_color=BTN_CANCEL_HOV,
            text_color=TEXT_MAIN, font=FONT_BOLD,
            width=80, height=32,
            command=self._on_close,
        ).pack(side="right", padx=12, pady=6)

    def _build_two_col_pane(self, body, row, col, header, header_color,
                             col0_title, col1_title, rows, pady=8):
        outer = tk.Frame(body, bg=BG_PANEL)
        outer.grid(row=row, column=col, sticky="nsew", padx=8, pady=pady)
        outer.grid_rowconfigure(1, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        tk.Label(
            outer, text=header,
            bg=BG_PANEL, fg=header_color,
            font=font_sized_px(_theme.FONT_FAMILY, 10, "bold"), anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))

        tree_frame = tk.Frame(outer, bg=BG_DEEP)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        uid = f"OvPanel{row}{col}{id(self)}"
        style = ttk.Style()
        style.configure(f"{uid}.Treeview",
                        background=BG_DEEP, foreground=TEXT_MAIN,
                        fieldbackground=BG_DEEP, rowheight=scaled(20),
                        font=font_sized_px(_theme.FONT_FAMILY, 9))
        style.configure(f"{uid}.Treeview.Heading",
                        background=BG_HEADER, foreground=TEXT_SEP,
                        font=font_sized_px(_theme.FONT_FAMILY, 9, "bold"), relief="flat")
        style.map(f"{uid}.Treeview",
                  background=[("selected", BG_SELECT)],
                  foreground=[("selected", TEXT_MAIN)])

        tv = ttk.Treeview(
            tree_frame,
            columns=("col1",),
            displaycolumns=("col1",),
            show="headings tree",
            style=f"{uid}.Treeview",
            selectmode="browse",
        )
        tv.heading("#0",   text=col0_title, anchor="w")
        tv.heading("col1", text=col1_title, anchor="w")
        tv.column("#0",   minwidth=100, stretch=True)
        tv.column("col1", minwidth=100, stretch=True)
        tv.tag_configure("bsa", foreground=TAG_BSA_ALT)

        def _resize_cols(event, _tv=tv):
            half = event.width // 2
            _tv.column("#0",   width=half)
            _tv.column("col1", width=half)
        tv.bind("<Configure>", _resize_cols)

        vsb = tk.Scrollbar(tree_frame, orient="vertical", command=tv.yview,
                           bg=_theme.BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
                           highlightthickness=0, bd=0)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        if not LEGACY_WHEEL_REDUNDANT:
            tv.bind("<Button-4>", lambda e: tv.yview_scroll(-3, "units"))
            tv.bind("<Button-5>", lambda e: tv.yview_scroll( 3, "units"))

        for path, mod_str in rows:
            tags = ("bsa",) if _BSA_ROW_RE.match(path) else ()
            tv.insert("", "end", text=path, values=(mod_str,), tags=tags)
        if not rows:
            tv.insert("", "end", text="(none)", values=("",))

    def _build_one_col_pane(self, body, row, col, header, header_color,
                             col0_title, rows):
        outer = tk.Frame(body, bg=BG_PANEL)
        outer.grid(row=row, column=col, sticky="nsew", padx=(4, 8), pady=8)
        outer.grid_rowconfigure(1, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        tk.Label(
            outer, text=header,
            bg=BG_PANEL, fg=header_color,
            font=font_sized_px(_theme.FONT_FAMILY, 10, "bold"), anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=4, pady=(4, 2))

        tree_frame = tk.Frame(outer, bg=BG_DEEP)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        uid = f"NcPanel{col}{id(self)}"
        style = ttk.Style()
        style.configure(f"{uid}.Treeview",
                        background=BG_DEEP, foreground=TEXT_MAIN,
                        fieldbackground=BG_DEEP, rowheight=scaled(20),
                        font=font_sized_px(_theme.FONT_FAMILY, 9))
        style.configure(f"{uid}.Treeview.Heading",
                        background=BG_HEADER, foreground=TEXT_SEP,
                        font=font_sized_px(_theme.FONT_FAMILY, 9, "bold"), relief="flat")
        style.map(f"{uid}.Treeview",
                  background=[("selected", BG_SELECT)],
                  foreground=[("selected", TEXT_MAIN)])

        tv = ttk.Treeview(
            tree_frame,
            columns=(),
            show="tree",
            style=f"{uid}.Treeview",
            selectmode="browse",
        )
        tv.heading("#0", text=col0_title, anchor="w")
        tv.column("#0", minwidth=180, stretch=True)
        tv.tag_configure("bsa", foreground=TAG_BSA_ALT)

        vsb = tk.Scrollbar(tree_frame, orient="vertical", command=tv.yview,
                           bg=_theme.BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
                           highlightthickness=0, bd=0)
        tv.configure(yscrollcommand=vsb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        if not LEGACY_WHEEL_REDUNDANT:
            tv.bind("<Button-4>", lambda e: tv.yview_scroll(-3, "units"))
            tv.bind("<Button-5>", lambda e: tv.yview_scroll( 3, "units"))

        for path in rows:
            tags = ("bsa",) if _BSA_ROW_RE.match(path) else ()
            tv.insert("", "end", text=path, tags=tags)
        if not rows:
            tv.insert("", "end", text="(none)")

    def _on_close(self):
        self._on_done(self)


# VRAMr preset panel — overlay on plugin panel
# ---------------------------------------------------------------------------
class VRAMrPresetPanel(ctk.CTkFrame):
    """Inline panel overlaying the plugin panel. User picks a preset, clicks Run,
    then the panel hides and VRAMr runs in a background thread."""

    _PRESETS = [
        ("hq",          "High Quality",  "2K / 2K / 1K / 1K  — 4K modlist downscaled to 2K"),
        ("quality",     "Quality",       "2K / 1K / 1K / 1K  — Balance of quality & savings"),
        ("optimum",     "Optimum",       "2K / 1K / 512 / 512 — Good starting point"),
        ("performance", "Performance",   "2K / 512 / 512 / 512 — Big gains, lower close-up"),
        ("vanilla",     "Vanilla",       "512 / 512 / 512 / 512 — Just run the game"),
    ]

    def __init__(self, parent, *, bat_dir: Path, game_data_dir: Path,
                 output_dir: Path, log_fn, on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._bat_dir = bat_dir
        self._game_data_dir = game_data_dir
        self._output_dir = output_dir
        self._log = log_fn
        self._on_done = on_done or (lambda p: None)
        self._preset_var = tk.StringVar(value="optimum")
        self._build()

    def _build(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_propagate(False)
        ctk.CTkLabel(
            title_bar, text="VRAMr — Choose Preset",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4, pady=4)

        # Body
        body = ctk.CTkFrame(self, fg_color=BG_DEEP, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            body, text="VRAMr Texture Optimiser",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(anchor="w", padx=20, pady=(20, 4))
        ctk.CTkLabel(
            body, text="Select an optimisation preset, then click Run.",
            font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(anchor="w", padx=20, pady=(0, 12))

        frame = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
        frame.pack(fill="x", padx=20, pady=4)
        for key, label, desc in self._PRESETS:
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=3)
            ctk.CTkRadioButton(
                row, text=label, variable=self._preset_var, value=key,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=desc,
                font=FONT_SMALL, text_color=TEXT_DIM,
            ).pack(side="left", padx=(12, 0))

        ctk.CTkLabel(
            body, text=f"Output: {self._output_dir}",
            font=FONT_SMALL, text_color=TEXT_DIM, wraplength=480,
        ).pack(anchor="w", padx=20, pady=(12, 4))

        ctk.CTkButton(
            body, text="▶  Run VRAMr", width=160, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_run,
        ).pack(pady=(8, 20))

    def _on_close(self):
        self._on_done(self)

    def _on_run(self):
        preset = self._preset_var.get()
        self._log(f"VRAMr: starting with '{preset}' preset...")
        bat_dir = self._bat_dir
        game_data_dir = self._game_data_dir
        output_dir = self._output_dir
        log_fn = self._log
        app = self.winfo_toplevel()
        if hasattr(app, "_status"):
            app._status.show_log()
        self._on_done(self)

        def _log_safe(msg: str):
            try:
                if hasattr(app, "call_threadsafe"):
                    app.call_threadsafe(lambda m=msg: log_fn(m))
                else:
                    log_fn(msg)
            except Exception:
                pass

        def _worker():
            try:
                from wrappers.vramr import run_vramr
                run_vramr(
                    bat_dir=bat_dir,
                    game_data_dir=game_data_dir,
                    output_dir=output_dir,
                    preset=preset,
                    log_fn=_log_safe,
                )
            except Exception as exc:
                _log_safe(f"VRAMr error: {exc}")

        threading.Thread(target=_worker, daemon=True).start()


# BENDr run dialog
# ---------------------------------------------------------------------------
class _BENDrRunDialog(ctk.CTkToplevel):
    """Modal confirmation dialog that runs the BENDr pipeline in a background thread."""

    def __init__(self, parent, *, bat_dir: Path, game_data_dir: Path,
                 output_dir: Path, log_fn):
        super().__init__(parent, fg_color=BG_OVERLAY_DEEP)
        self.title("BENDr — Normal Map Processor")
        self.geometry("480x260")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self._bat_dir = bat_dir
        self._game_data_dir = game_data_dir
        self._output_dir = output_dir
        self._log = log_fn
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _build(self):
        ctk.CTkLabel(
            self, text="BENDr Normal Map Processor",
            font=(_theme.FONT_FAMILY, 16, "bold"), text_color=TEXT_MAIN,
        ).pack(pady=(16, 4))
        ctk.CTkLabel(
            self,
            text=(
                "Processes normal maps and parallax textures:\n"
                "BSA extract → filter → parallax prep → bend normals → BC7 compress"
            ),
            font=(_theme.FONT_FAMILY, 12), text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self, text=f"Output: {self._output_dir}",
            font=(_theme.FONT_FAMILY, 11), text_color=TEXT_DIM, wraplength=440,
        ).pack(pady=(4, 12))

        ctk.CTkButton(
            self, text="▶  Run BENDr", width=160, height=36,
            font=(_theme.FONT_FAMILY, 13, "bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_WHITE,
            command=self._on_run,
        ).pack(pady=(0, 16))

    def _on_run(self):
        self._log("BENDr: starting pipeline...")

        bat_dir = self._bat_dir
        game_data_dir = self._game_data_dir
        output_dir = self._output_dir
        log_fn = self._log
        app = self.winfo_toplevel().master
        if hasattr(app, "_status"):
            app._status.show_log()
        self._on_close()

        def _log_safe(msg: str):
            try:
                if hasattr(app, "call_threadsafe"):
                    app.call_threadsafe(lambda m=msg: log_fn(m))
                else:
                    log_fn(msg)
            except Exception:
                pass

        def _worker():
            try:
                from wrappers.bendr import run_bendr
                run_bendr(
                    bat_dir=bat_dir,
                    game_data_dir=game_data_dir,
                    output_dir=output_dir,
                    log_fn=_log_safe,
                )
            except Exception as exc:
                _log_safe(f"BENDr error: {exc}")

        threading.Thread(target=_worker, daemon=True).start()


# ParallaxR run dialog
# ---------------------------------------------------------------------------
class _ParallaxRRunDialog(ctk.CTkToplevel):
    """Modal confirmation dialog that runs the ParallaxR pipeline in a background thread."""

    def __init__(self, parent, *, bat_dir: Path, game_data_dir: Path,
                 output_dir: Path, log_fn):
        super().__init__(parent, fg_color=BG_OVERLAY_DEEP)
        self.title("ParallaxR — Parallax Texture Processor")
        self.geometry("480x260")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self._bat_dir = bat_dir
        self._game_data_dir = game_data_dir
        self._output_dir = output_dir
        self._log = log_fn
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _build(self):
        ctk.CTkLabel(
            self, text="ParallaxR Parallax Texture Processor",
            font=(_theme.FONT_FAMILY, 16, "bold"), text_color=TEXT_MAIN,
        ).pack(pady=(16, 4))
        ctk.CTkLabel(
            self,
            text=(
                "Processes normal maps and parallax textures:\n"
                "BSA extract → filter pairs → height maps → output QC"
            ),
            font=(_theme.FONT_FAMILY, 12), text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 12))

        ctk.CTkLabel(
            self, text=f"Output: {self._output_dir}",
            font=(_theme.FONT_FAMILY, 11), text_color=TEXT_DIM, wraplength=440,
        ).pack(pady=(4, 12))

        ctk.CTkButton(
            self, text="▶  Run ParallaxR", width=160, height=36,
            font=(_theme.FONT_FAMILY, 13, "bold"),
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_WHITE,
            command=self._on_run,
        ).pack(pady=(0, 16))

    def _on_run(self):
        self._log("ParallaxR: starting pipeline...")

        bat_dir = self._bat_dir
        game_data_dir = self._game_data_dir
        output_dir = self._output_dir
        log_fn = self._log
        app = self.winfo_toplevel().master
        if hasattr(app, "_status"):
            app._status.show_log()
        self._on_close()

        def _log_safe(msg: str):
            try:
                if hasattr(app, "call_threadsafe"):
                    app.call_threadsafe(lambda m=msg: log_fn(m))
                else:
                    log_fn(msg)
            except Exception:
                pass

        def _worker():
            try:
                from wrappers.parallaxr import run_parallaxr
                run_parallaxr(
                    bat_dir=bat_dir,
                    game_data_dir=game_data_dir,
                    output_dir=output_dir,
                    log_fn=_log_safe,
                )
            except Exception as exc:
                _log_safe(f"ParallaxR error: {exc}")

        threading.Thread(target=_worker, daemon=True).start()


_PGPATCHER_DEFAULT_PROTON = ""  # empty string = "Game default"


def _get_tool_prefix_env(
    exe_path: "Path", proton_name: str, prefix_dir: "Path | None" = None,
) -> "tuple[Path, Path, dict] | None":
    """Resolve (proton_script, prefix_dir, env) for a tool's isolated prefix.

    proton_name is the display name from the dropdown (e.g. "Proton 10.0").
    Returns None if the Proton version can't be found.
    The prefix directory is created if it doesn't exist.
    Runs wineboot to initialise the prefix if it's brand new.

    By default the prefix lives at ``prefix_<ProtonName>/`` next to the exe.
    Pass *prefix_dir* to place it elsewhere (e.g. a shared prefix under the
    app config) — the same initialisation (wineboot + ShowDotFiles) applies.
    """
    from Utils.steam_finder import find_any_installed_proton, find_steam_root_for_proton_script
    proton_script = find_any_installed_proton(proton_name)
    if proton_script is None:
        return None

    steam_root = find_steam_root_for_proton_script(proton_script)
    if steam_root is None:
        return None

    if prefix_dir is None:
        prefix_dir = exe_path.parent / f"prefix_{proton_script.parent.name}"
    is_new = not (prefix_dir / "pfx").is_dir()
    prefix_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"] = str(prefix_dir)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)

    if is_new:
        # Initialise the prefix synchronously before returning
        try:
            subprocess.run(
                ["python3", str(proton_script), "run", "wineboot", "--init"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception:
            pass
        # Enable "Show dotfiles" (winecfg) so tools can browse Unix dot-dirs
        # (e.g. ~/.config, ~/.local) under the Z: drive. Mirrors the winecfg
        # checkbox: HKCU\Software\Wine\ShowDotFiles = "Y".
        try:
            subprocess.run(
                ["python3", str(proton_script), "run", "reg", "add",
                 r"HKCU\Software\Wine", "/v", "ShowDotFiles",
                 "/t", "REG_SZ", "/d", "Y", "/f"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except Exception:
            pass

    return proton_script, prefix_dir, env


class _ExeConfigDialog(ctk.CTkToplevel):
    """Thin modal wrapper around ExeConfigPanel.

    Callers access: ``result``, ``launch_mode``, ``deploy_before_launch``,
    ``hide``, ``removed``, ``proton_override``, ``data_folder_exe``.
    """

    def __init__(self, parent, exe_path: "Path", game, saved_args: str = "",
                 custom_exes: "list | None" = None, launch_mode: "str | None" = None,
                 deploy_before_launch: "bool | None" = None,
                 is_hidden: bool = False, proton_override: "str | None" = None,
                 is_data_folder_exe: bool = False, is_apps_exe: bool = False,
                 saved_launch_options: str = "",
                 log_fn=None):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Configure: {exe_path.name}")
        self.geometry("480x180" if launch_mode is not None else "640x460")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Result attributes proxied from the panel
        self.result: "str | None" = None
        self.launch_mode: "str | None" = None
        self.deploy_before_launch: "bool | None" = None
        self.hide: "bool | None" = None
        self.removed: bool = False
        self.proton_override: "str | None" = None
        self.data_folder_exe: "bool | None" = None
        self.launch_options: "str | None" = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        def _on_done(panel):
            # Copy result attributes from panel before destroying
            self.result = panel.result
            self.launch_mode = panel.launch_mode
            self.deploy_before_launch = panel.deploy_before_launch
            self.hide = panel.hide
            self.removed = panel.removed
            self.proton_override = panel.proton_override
            self.data_folder_exe = panel.data_folder_exe
            self.launch_options = panel.launch_options
            self._on_close()

        self._panel = ExeConfigPanel(
            self,
            exe_path=exe_path, game=game, saved_args=saved_args,
            custom_exes=custom_exes, launch_mode=launch_mode,
            deploy_before_launch=deploy_before_launch, is_hidden=is_hidden,
            on_done=_on_done, proton_override=proton_override,
            is_data_folder_exe=is_data_folder_exe, is_apps_exe=is_apps_exe,
            saved_launch_options=saved_launch_options,
            log_fn=log_fn,
        )
        self._panel.grid(row=0, column=0, sticky="nsew")

        self.after(80, self._make_modal)

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
# ExeConfigPanel — inline overlay version of _ExeConfigDialog
# ---------------------------------------------------------------------------

class ExeConfigPanel(ctk.CTkFrame):
    """Inline panel version of _ExeConfigDialog. Overlays the plugin-panel container.
    Uses on_done(panel) callback; caller reads panel.result / .launch_mode / etc."""

    _EXE_ARGS_FILE = get_exe_args_path()

    def __init__(self, parent, exe_path: "Path", game, saved_args: str = "",
                 custom_exes: "list | None" = None, launch_mode: "str | None" = None,
                 deploy_before_launch: "bool | None" = None,
                 is_hidden: bool = False, on_done=None, proton_override: "str | None" = None,
                 is_data_folder_exe: bool = False, is_apps_exe: bool = False,
                 saved_launch_options: str = "",
                 log_fn=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._log = log_fn or print

        self._exe_path = exe_path
        self._game = game
        self._saved_args = saved_args
        self._custom_exes: "list" = list(custom_exes) if custom_exes else []
        self._initial_launch_mode: "str | None" = launch_mode
        self._launch_mode_var = tk.StringVar(value=launch_mode or "auto")
        self._deploy_before_launch_var = tk.BooleanVar(
            value=True if deploy_before_launch is None else deploy_before_launch
        )
        self._hide_var = tk.BooleanVar(value=is_hidden)
        self._data_folder_var = tk.BooleanVar(value=is_data_folder_exe)
        self._is_apps_exe = is_apps_exe
        self._launch_options_var = tk.StringVar(value=saved_launch_options)
        self._on_done = on_done or (lambda p: None)
        self.result: "str | None" = None
        self.launch_mode: "str | None" = None
        self.deploy_before_launch: "bool | None" = None
        self.hide: "bool | None" = None
        self.removed: bool = False
        self.proton_override: "str | None" = None
        self.data_folder_exe: "bool | None" = None
        self.launch_options: "str | None" = None

        # Proton version dropdown (non-launcher exes only)
        from Utils.steam_finder import list_installed_proton
        self._proton_versions: list[str] = (
            ["Game default"] + [p.parent.name for p in list_installed_proton()]
        )
        _default_override = _PGPATCHER_DEFAULT_PROTON if exe_path.name.lower() == "pgpatcher.exe" else ""
        _saved = proton_override if proton_override is not None else _default_override
        def _best_match(name: str) -> str:
            if not name:
                return "Game default"
            if name in self._proton_versions:
                return name
            name_lower = name.lower()
            for v in self._proton_versions:
                if v.lower().startswith(name_lower):
                    return v
            return "Game default"
        self._proton_var = tk.StringVar(value=_best_match(_saved))

        # Per-profile exe_args.json when the profile uses profile-specific mods
        self._EXE_ARGS_FILE = _resolve_exe_args_file(game)

        self._game_path: "Path | None" = (
            game.get_game_path() if hasattr(game, "get_game_path") else None
        )
        self._mods_path: "Path | None" = (
            game.get_effective_mod_staging_path() if hasattr(game, "get_effective_mod_staging_path") else None
        )
        self._overwrite_path: "Path | None" = (
            self._mods_path.parent / "overwrite" if self._mods_path else None
        )

        self._is_pgpatcher = exe_path.name.lower() == "pgpatcher.exe"
        self._mod_var = tk.StringVar(value="")
        self._mod_entries: list[tuple[str, "Path"]] = self._load_mod_entries()
        self._mod_popup: "tk.Toplevel | None" = None
        self._mod_popup_click_id: str = ""

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Configure: {exe_path.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Bottom action bar host — packed first (bottom) so it stays visible
        # when the scrollable section content is tall. Populated inside _build().
        self._bar_host = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self._bar_host.pack(side="bottom", fill="x")

        # Scrollable body frame — sections live here
        self._body = ctk.CTkScrollableFrame(
            self, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=BG_HEADER,
            scrollbar_button_hover_color=BG_HOVER,
        )
        self._body.pack(side="top", fill="both", expand=True)

        self._build()
        self._bind_wheel_to_body(self._body)

        if self._initial_launch_mode is None:
            self._load_saved()

    def _bind_wheel_to_body(self, root):
        """Forward mousewheel events from every child of the scrollable body
        to the body's internal canvas so scrolling works anywhere over it."""
        try:
            canvas = self._body._parent_canvas
        except Exception:
            return

        def _on_wheel(event):
            if event.num == 4:
                canvas.yview_scroll(-3, "units")
            elif event.num == 5:
                canvas.yview_scroll(3, "units")
            else:
                delta = -1 * int(event.delta / 40) if event.delta else 0
                if delta:
                    canvas.yview_scroll(delta, "units")
            return "break"

        def _walk(w):
            try:
                # On Tk >= 8.7 CTkScrollableFrame handles <MouseWheel> itself via
                # bind_all; only supplement Button-4/5 for Tk 8.6.
                if not LEGACY_WHEEL_REDUNDANT:
                    w.bind("<Button-4>", _on_wheel, add="+")
                    w.bind("<Button-5>", _on_wheel, add="+")
            except Exception:
                pass
            for child in w.winfo_children():
                _walk(child)

        _walk(root)

    def _load_mod_entries(self) -> "list[tuple[str, Path]]":
        entries: list[tuple[str, Path]] = []
        if self._overwrite_path and self._overwrite_path.is_dir():
            entries.append(("overwrite", self._overwrite_path))
        if self._mods_path and self._mods_path.is_dir():
            for e in sorted(self._mods_path.iterdir(), key=lambda p: p.name.casefold()):
                if e.is_dir() and "_separator" not in e.name:
                    entries.append((e.name, e))
        return entries

    def _build(self):
        body = self._body
        body.grid_columnconfigure(0, weight=1)

        is_game_exe = self._initial_launch_mode is not None
        body_total_row = count(start=0)

        if not is_game_exe:
            sec_args = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
            sec_args.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=(12, 4))
            sec_args.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                sec_args, text="Launch arguments", font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
            ctk.CTkLabel(
                sec_args,
                text="Arguments passed to the exe. Use Wine paths for file "
                     "arguments (e.g. Z:\\home\\...) \u2014 the buttons below insert them for you.",
                font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left", wraplength=560,
            ).grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))

            self._final_box = ctk.CTkTextbox(
                sec_args, height=90, font=FONT_NORMAL,
                fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
                border_width=1, wrap="word",
            )
            self._final_box.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 4))

            insert_row = ctk.CTkFrame(sec_args, fg_color="transparent")
            insert_row.grid(row=3, column=0, sticky="w", padx=10, pady=(0, 8))
            insert_btn = dict(height=26, font=FONT_SMALL,
                              fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN)
            ctk.CTkButton(
                insert_row, text="Insert game path", width=130,
                command=self._insert_game_path, **insert_btn,
            ).pack(side="left", padx=(0, 6))
            self._insert_mod_btn = ctk.CTkButton(
                insert_row, text="Insert mod path \u25bc", width=140,
                command=self._open_mod_popup, **insert_btn,
            )
            self._insert_mod_btn.pack(side="left")

            if self._is_pgpatcher:
                sec_mod = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
                sec_mod.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=4)
                sec_mod.grid_columnconfigure(1, weight=1)
                ctk.CTkLabel(
                    sec_mod, text="Output mod", font=FONT_BOLD,
                    text_color=TEXT_MAIN, anchor="w",
                ).grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(8, 2))
                ctk.CTkLabel(
                    sec_mod, text="Mod:", font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
                ).grid(row=1, column=0, sticky="w", padx=(10, 4), pady=(0, 8))
                mod_row = ctk.CTkFrame(sec_mod, fg_color="transparent")
                mod_row.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=(0, 8))
                mod_row.grid_columnconfigure(0, weight=1)
                self._mod_entry = ctk.CTkEntry(
                    mod_row, textvariable=self._mod_var, font=FONT_SMALL,
                    fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
                    placeholder_text="PGPatcher (default)",
                )
                self._mod_entry.grid(row=0, column=0, sticky="ew")
                self._mod_entry._entry.bind(
                    "<Control-a>",
                    lambda e: (self._mod_entry._entry.select_range(0, "end"),
                               self._mod_entry._entry.icursor("end"), "break")[2],
                )
                ctk.CTkButton(
                    mod_row, text="\u25bc", width=28, font=FONT_SMALL,
                    fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
                    command=self._open_mod_popup,
                ).grid(row=0, column=1, padx=(4, 0))
                self._mod_var.trace_add("write", self._on_mod_typed)

            # Proton version section
            sec_proton = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
            sec_proton.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=4)
            sec_proton.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                sec_proton, text="Proton version", font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))
            ctk.CTkOptionMenu(
                sec_proton, values=self._proton_versions,
                variable=self._proton_var,
                width=220, font=FONT_SMALL,
                fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
                dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
                command=lambda _: None,
            ).grid(row=0, column=1, sticky="w", padx=(0, 10), pady=(8, 4))
            ctk.CTkLabel(
                sec_proton,
                text="Use a specific Proton version with an isolated prefix next to\n"
                     "the exe, instead of the game's prefix. Useful for tools that\n"
                     "don't work with the game's Proton version.\n"
                     "For Bethesda games the game path (registry), plugins.txt and\n"
                     "My Games INIs are set up in the prefix automatically at launch.",
                font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
            ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 4))

            btn_row = ctk.CTkFrame(sec_proton, fg_color="transparent")
            btn_row.grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 8))
            small_btn = dict(height=28, font=FONT_SMALL,
                             fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN)
            ctk.CTkButton(
                btn_row, text="Run EXE in prefix …", width=160,
                command=self._run_exe_in_prefix, **small_btn,
            ).pack(side="left", padx=(0, 6))
            ctk.CTkButton(
                btn_row, text="Run protontricks", width=140,
                command=self._run_protontricks_in_prefix, **small_btn,
            ).pack(side="left", padx=(0, 6))
            ctk.CTkButton(
                btn_row, text="Open prefix folder", width=140,
                command=self._open_prefix_folder, **small_btn,
            ).pack(side="left")

        if is_game_exe:
            sec4 = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
            sec4.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=(12, 4))
            sec4.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                sec4, text="Launch via", font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=10, pady=(8, 4))
            ctk.CTkOptionMenu(
                sec4, values=["Auto", "Steam", "Heroic", "None"],
                variable=self._launch_mode_var,
                width=140, font=FONT_SMALL,
                fg_color=BG_HEADER, button_color=ACCENT, button_hover_color=ACCENT_HOV,
                dropdown_fg_color=BG_PANEL, text_color=TEXT_MAIN,
                command=lambda _: None,
            ).grid(row=0, column=1, sticky="w", padx=(0, 10), pady=(8, 4))
            ctk.CTkLabel(
                sec4,
                text="Auto detects Steam/Heroic ownership. Force a specific launcher or\n"
                     "None to always launch the exe directly via Proton.",
                font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
            ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 4))
            ctk.CTkCheckBox(
                sec4, text="Deploy mods before launching",
                variable=self._deploy_before_launch_var,
                font=FONT_SMALL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                checkmark_color=BG_DEEP,
            ).grid(row=2, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 8))

        if not is_game_exe:
            sec_launch = ctk.CTkFrame(body, fg_color=BG_PANEL, corner_radius=6)
            sec_launch.grid(row=next(body_total_row), column=0, sticky="ew", padx=12, pady=4)
            sec_launch.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                sec_launch, text="Launch Options", font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
            ctk.CTkLabel(
                sec_launch,
                text="Steam-style options: env vars (KEY=VALUE), wrappers (e.g. gamemoderun), "
                     "and %command% as placeholder for the full command. Without %command%, appended as suffix.",
                font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",  wraplength=460,
            ).grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
            _lo_entry = ctk.CTkEntry(
                sec_launch, textvariable=self._launch_options_var, font=FONT_SMALL,
                fg_color=BG_HEADER, text_color=TEXT_MAIN, border_color=BORDER,
                placeholder_text="e.g. PROTON_ENABLE_WAYLAND=0 gamemoderun %command%",
            )
            _lo_entry.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))
            _lo_entry._entry.bind(
                "<Control-a>",
                lambda e: (_lo_entry._entry.select_range(0, "end"),
                           _lo_entry._entry.icursor("end"), "break")[2],
            )

        bar = ctk.CTkFrame(self._bar_host, fg_color=BG_PANEL, corner_radius=0, height=48)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=90, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=9)
        ctk.CTkButton(
            bar, text="Save", width=90, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_save,
        ).pack(side="right", padx=4, pady=9)
        if self._exe_path in self._custom_exes:
            ctk.CTkButton(
                bar, text="Remove EXE", width=110, height=30, font=FONT_NORMAL,
                fg_color=BTN_DANGER_ALT, hover_color=BTN_DANGER_ALT_HOV, text_color=TEXT_WHITE,
                command=self._on_remove,
            ).pack(side="left", padx=(12, 4), pady=9)
        if not is_game_exe:
            ctk.CTkCheckBox(
                bar, text="Hide from dropdown",
                variable=self._hide_var,
                font=FONT_SMALL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                checkmark_color=BG_DEEP,
            ).pack(side="left", padx=(12, 4), pady=9)
        if not is_game_exe and not self._is_apps_exe:
            ctk.CTkCheckBox(
                bar, text="Run from Data folder",
                variable=self._data_folder_var,
                font=FONT_SMALL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                checkmark_color=BG_DEEP,
            ).pack(side="left", padx=(4, 4), pady=9)

    def _on_mod_typed(self, *_):
        if self._mod_popup and self._mod_popup.winfo_exists():
            self._populate_mod_popup()

    def _open_mod_popup(self):
        if self._mod_popup and self._mod_popup.winfo_exists():
            self._mod_popup.destroy()
            self._mod_popup = None
            return

        popup = tk.Toplevel(self.winfo_toplevel())
        popup.overrideredirect(True)
        popup.configure(bg=BG_PANEL)
        self._mod_popup = popup

        anchor = self._mod_entry if self._is_pgpatcher else self._insert_mod_btn
        anchor.update_idletasks()
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 2
        w = max(anchor.winfo_width() + 32, 320)
        popup.geometry(f"{w}x300+{x}+{y}")

        scroll = ctk.CTkScrollableFrame(popup, fg_color=BG_PANEL, corner_radius=0)
        scroll.pack(fill="both", expand=True)
        scroll.grid_columnconfigure(0, weight=1)
        self._mod_popup_scroll = scroll
        self._populate_mod_popup(show_all=True)

        popup.bind("<Escape>", lambda _: self._close_mod_popup())
        popup.lift()

        def _forward_scroll(event):
            canvas = scroll._parent_canvas
            if event.num == 4 or event.delta > 0:
                canvas.yview_scroll(-1, "units")
            else:
                canvas.yview_scroll(1, "units")
        # On Tk >= 8.7 CTkScrollableFrame handles <MouseWheel> itself; binding
        # another would double-scroll.
        if not LEGACY_WHEEL_REDUNDANT:
            popup.bind("<Button-4>", _forward_scroll)
            popup.bind("<Button-5>", _forward_scroll)

        def _bind_click_dismiss():
            if self._mod_popup and self._mod_popup.winfo_exists():
                self._mod_popup_click_id = self.bind(
                    "<Button-1>", self._on_root_click_while_popup, add="+"
                )
        self.after(100, _bind_click_dismiss)
        self._poll_mod_popup_focus()

    def _poll_mod_popup_focus(self):
        if not self._mod_popup or not self._mod_popup.winfo_exists():
            return
        try:
            mx, my = self.winfo_pointerx(), self.winfo_pointery()
            widget_under = self.winfo_containing(mx, my)
        except Exception:
            widget_under = None
        if widget_under is None and not self.focus_get():
            self._close_mod_popup()
            return
        self.after(300, self._poll_mod_popup_focus)

    def _close_mod_popup(self):
        if self._mod_popup and self._mod_popup.winfo_exists():
            self._mod_popup.destroy()
        self._mod_popup = None
        try:
            self.unbind("<Button-1>", self._mod_popup_click_id)
        except Exception:
            pass

    def _on_root_click_while_popup(self, event):
        if not self._mod_popup or not self._mod_popup.winfo_exists():
            self._close_mod_popup()
            return
        px, py, pw, ph = (
            self._mod_popup.winfo_rootx(), self._mod_popup.winfo_rooty(),
            self._mod_popup.winfo_width(), self._mod_popup.winfo_height(),
        )
        if px <= event.x_root <= px + pw and py <= event.y_root <= py + ph:
            return
        self._close_mod_popup()

    def _populate_mod_popup(self, show_all: bool = False):
        scroll = self._mod_popup_scroll
        for w in scroll.winfo_children():
            w.destroy()
        query = self._mod_var.get().casefold()
        names = [n for n, _ in self._mod_entries]
        filtered = names if (show_all or not query) else [n for n in names if query in n.casefold()]
        for name in filtered:
            btn = ctk.CTkButton(
                scroll, text=name, anchor="w", font=FONT_SMALL,
                fg_color="transparent", hover_color=BG_HOVER, text_color=TEXT_MAIN,
                height=26, corner_radius=4,
                command=lambda n=name: self._select_mod(n),
            )
            btn.pack(fill="x", padx=4, pady=1)

    def _select_mod(self, name: str):
        self._close_mod_popup()
        if self._is_pgpatcher:
            self._mod_var.set(name)
            return
        path = next((p for n, p in self._mod_entries if n == name), None)
        if path is not None:
            self._insert_arg_text(f'"{_to_wine_path(path)}"')

    def _insert_game_path(self):
        if self._game_path is None:
            self._log("Configure: game path not set.")
            return
        self._insert_arg_text(f'"{_to_wine_path(self._game_path)}"')

    def _insert_arg_text(self, text: str):
        existing = self._get_final_text()
        if existing and not existing.endswith(" "):
            text = " " + text
        self._final_box.insert("end", text)

    def _set_final_text(self, text: str):
        self._final_box.delete("1.0", "end")
        self._final_box.insert("1.0", text)

    def _get_final_text(self) -> str:
        return self._final_box.get("1.0", "end").strip()

    def _load_saved(self):
        if self._is_pgpatcher:
            # Output mod is stored separately; load it independently of saved_args.
            try:
                data = json.loads(self._EXE_ARGS_FILE.read_text(encoding="utf-8"))
                saved_mod = data.get("PGPatcher.exe:output_mod", "")
                if saved_mod:
                    self._mod_var.set(saved_mod)
            except (OSError, ValueError):
                pass
        if self._saved_args:
            self._set_final_text(self._saved_args)

    def _get_selected_tool_env(self):
        selected = self._proton_var.get()
        if selected == "Game default":
            self._log("Prefix tools: select a specific Proton version first.")
            return None
        result = _get_tool_prefix_env(self._exe_path, selected)
        if result is None:
            self._log(f"Prefix tools: could not find Proton '{selected}'.")
            return None
        proton_script, prefix_dir, env = result
        if getattr(self._game, "synthesis_registry_name", None):
            from Utils.bethesda_registry import maybe_register_for_game
            maybe_register_for_game(
                prefix_dir=prefix_dir,
                proton_script=proton_script,
                env=env,
                game=self._game,
                log_fn=self._log,
            )
        from wizards._proton_prefix import link_mygames, link_plugins_txt
        pfx = prefix_dir / "pfx"
        link_plugins_txt(self._game, pfx, lambda m: self._log(f"Prefix tools: {m}"))
        link_mygames(self._game, pfx, lambda m: self._log(f"Prefix tools: {m}"))
        return result

    def _open_prefix_folder(self):
        selected = self._proton_var.get()
        if selected == "Game default":
            self._log("Prefix tools: select a specific Proton version first.")
            return
        from Utils.steam_finder import find_any_installed_proton
        proton_script = find_any_installed_proton(selected)
        if proton_script is None:
            self._log(f"Prefix tools: could not find Proton '{selected}'.")
            return
        prefix_dir = self._exe_path.parent / f"prefix_{proton_script.parent.name}"
        if not prefix_dir.is_dir():
            self._log("Prefix tools: no prefix exists yet for this version — run the exe once first.")
            return
        xdg_open(str(prefix_dir))

    def _run_exe_in_prefix(self):
        result = self._get_selected_tool_env()
        if result is None:
            return
        proton_script, prefix_dir, env = result
        self._log(f"Prefix tools: initialised prefix at {prefix_dir}, opening file picker …")

        def _on_picked(exe):
            if exe is None:
                return
            if not exe.is_file():
                self._log(f"Prefix tools: file not found: {exe}")
                return
            self._log(f"Prefix tools: launching {exe.name} …")
            try:
                subprocess.Popen(
                    ["python3", str(proton_script), "run", str(exe)],
                    env=env, cwd=exe.parent,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                self._log(f"Prefix tools error: {e}")

        from Utils.portal_filechooser import pick_exe_file
        pick_exe_file("Select EXE to run in prefix", _on_picked)

    def _run_protontricks_in_prefix(self):
        result = self._get_selected_tool_env()
        if result is None:
            return
        proton_script, prefix_dir, env = result

        from Utils.steam_finder import game_steam_id
        steam_id = game_steam_id(self._game)

        if shutil.which("protontricks") is not None:
            cmd = ["protontricks"]
            if steam_id:
                cmd += [steam_id, "--gui"]
            else:
                cmd += ["--gui"]
        elif shutil.which("flatpak") is not None and subprocess.run(
            ["flatpak", "info", "com.github.Matoking.protontricks"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            cmd = ["flatpak", "run", "com.github.Matoking.protontricks"]
            if steam_id:
                cmd += [steam_id, "--gui"]
            else:
                cmd += ["--gui"]
        else:
            self._launch_winetricks_gui_in_prefix(prefix_dir / "pfx")
            return

        env["STEAM_COMPAT_DATA_PATH"] = str(prefix_dir)
        env["PROTON_VERSION"] = proton_script.parent.name
        self._log(f"Prefix tools: launching protontricks for prefix {prefix_dir.name} …")
        try:
            subprocess.Popen(
                cmd, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._log(f"Prefix tools error: {e}")

    def _launch_winetricks_gui_in_prefix(self, wineprefix: Path):
        from Utils.protontricks import (
            _bundled_winetricks,
            _get_proton_bin,
            cabextract_installed,
            install_cabextract,
            install_winetricks,
            winetricks_installed,
        )

        if not wineprefix.is_dir():
            self._log(
                "Prefix tools: protontricks not found and no Wine prefix is "
                "available — cannot launch winetricks."
            )
            return
        if not winetricks_installed():
            self._log("Prefix tools: winetricks not found — downloading …")
            if not install_winetricks(log_fn=lambda m: self._log(f"Prefix tools: {m}")):
                return
        if not cabextract_installed():
            self._log("Prefix tools: cabextract not found — downloading a portable copy …")
            if not install_cabextract(log_fn=lambda m: self._log(f"Prefix tools: {m}")):
                return

        wt = _bundled_winetricks()
        env = os.environ.copy()
        env["WINEPREFIX"] = str(wineprefix)
        path_prefix = str(wt.parent)
        proton_bin = _get_proton_bin()
        if proton_bin:
            path_prefix = proton_bin + os.pathsep + path_prefix
        env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")

        self._log(f"Prefix tools: launching winetricks GUI against {wineprefix.parent.name} …")
        try:
            subprocess.Popen(
                [str(wt), "--gui"], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._log(f"Prefix tools error: {e}")

    def _on_save(self):
        if self._initial_launch_mode is not None:
            self.launch_mode = self._launch_mode_var.get().lower()
            self.deploy_before_launch = self._deploy_before_launch_var.get()
        else:
            final = self._get_final_text()
            try:
                data = json.loads(self._EXE_ARGS_FILE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data[self._exe_path.name] = final
            if self._is_pgpatcher:
                data["PGPatcher.exe:output_mod"] = self._mod_var.get().strip()
            try:
                self._EXE_ARGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except OSError:
                pass
            self.result = final
            self.hide = self._hide_var.get()
            selected = self._proton_var.get()
            self.proton_override = "" if selected == "Game default" else selected
            if not self._is_apps_exe:
                self.data_folder_exe = self._data_folder_var.get()
        self.launch_options = self._launch_options_var.get().strip()
        self._on_done(self)

    def _on_remove(self):
        self.removed = True
        self.result = None
        self._on_done(self)

    def _on_cancel(self):
        self._on_done(self)


class _ReplaceModDialog(ctk.CTkToplevel):
    """Modal dialog shown when installing a mod whose name already exists.
    result: "all" | "selected" | "rename" | "cancel"
    selected_files: set[str] — always None here; populated by caller if "selected"
    new_name: str | None — set when result == "rename"
    """

    _WIDTH = 480
    _HEIGHT = 180

    def __init__(self, parent, mod_name: str,
                 suggestions: list[str] | None = None,
                 rename_conflict: str | None = None):
        self._parent_ref = parent
        super().__init__(master=parent)
        self.old_x = None
        self.old_y = None
        self.resizable(False, False)
        self.overrideredirect(True)
        if parent is not None:
            self.transient(parent)
        self.withdraw()

        self.result: str = "cancel"
        self.selected_files: set[str] | None = None
        self.new_name: str | None = None
        self._mod_name = mod_name
        # De-dup while preserving order; drop the current mod_name since it's
        # what the user is replacing.
        _seen: set[str] = set()
        self._suggestions: list[str] = []
        for s in (suggestions or []):
            s = (s or "").strip()
            if s and s != mod_name and s not in _seen:
                _seen.add(s)
                self._suggestions.append(s)

        self.transparent_color = self._apply_appearance_mode(self.cget("fg_color"))
        if sys.platform.startswith("win"):
            self.attributes("-transparentcolor", self.transparent_color)

        self.bg_color = self._apply_appearance_mode(
            ctk.ThemeManager.theme["CTkFrame"]["fg_color"])

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._frame = ctk.CTkFrame(
            self, corner_radius=5, width=self._WIDTH, border_width=1,
            bg_color=self.transparent_color, fg_color=self.bg_color,
        )
        self._frame.grid(sticky="nsew")
        self._frame.bind("<B1-Motion>", self._move_window)
        self._frame.bind("<ButtonPress-1>", self._old_xy_set)
        self._frame.grid_columnconfigure(0, weight=1)
        self._frame.grid_rowconfigure(1, weight=1)

        # Icons
        _warn = _PilImage.open(ICON_PATH["warning"])
        self._warn_icon = ctk.CTkImage(_warn, _warn, (30, 30))
        _cl = _PilImage.open(ICON_PATH["close"][0])
        _cl_d = _PilImage.open(ICON_PATH["close"][1])
        self._close_icon = ctk.CTkImage(_cl, _cl_d, (20, 20))

        # Title row
        title_lbl = ctk.CTkLabel(
            self._frame, text="  Mod Already Exists", font=("", 18),
            image=self._warn_icon, compound="left",
        )
        title_lbl.grid(row=0, column=0, sticky="w", padx=15, pady=(12, 4))
        title_lbl.bind("<B1-Motion>", self._move_window)
        title_lbl.bind("<ButtonPress-1>", self._old_xy_set)

        ctk.CTkButton(
            self._frame, text="", image=self._close_icon, width=20, height=20,
            hover=False, fg_color="transparent", command=self._on_cancel,
        ).grid(row=0, column=1, sticky="ne", padx=10, pady=10)

        # Body text
        if rename_conflict:
            body_text = (
                f"'{rename_conflict}' is also already installed.\n"
                f"Pick a different name, or choose another option."
            )
        else:
            body_text = (
                f"'{mod_name}' is already installed.\n"
                f"How would you like to handle the existing mod?"
            )
        ctk.CTkLabel(
            self._frame,
            text=body_text,
            justify="left", anchor="w",
            wraplength=self._WIDTH - 40,
        ).grid(row=1, column=0, padx=(20, 10), pady=(0, 6), sticky="new", columnspan=2)

        # Rename row (collapsed by default)
        self._rename_frame = ctk.CTkFrame(self._frame, fg_color="transparent")
        self._rename_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 4))
        self._rename_frame.grid_columnconfigure(0, weight=1)
        self._rename_frame.grid_remove()

        if rename_conflict:
            initial_name = rename_conflict
        elif self._suggestions:
            initial_name = self._suggestions[0]
        else:
            initial_name = mod_name
        self._rename_var = tk.StringVar(value=initial_name)
        rename_entry = ctk.CTkEntry(
            self._rename_frame, textvariable=self._rename_var,
            font=FONT_NORMAL, fg_color=BG_PANEL, text_color=TEXT_MAIN,
            border_color=BORDER,
        )
        rename_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        rename_entry.bind("<Return>", lambda _e: (self._on_rename_confirm(), "break")[1])
        self._rename_entry = rename_entry

        ctk.CTkButton(
            self._rename_frame, text="Confirm", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_rename_confirm,
        ).grid(row=0, column=1)

        if self._suggestions:
            ctk.CTkLabel(
                self._rename_frame, text="Or choose a suggestion:",
                font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 2))
            ctk.CTkOptionMenu(
                self._rename_frame, values=self._suggestions,
                font=FONT_SMALL, fg_color=BG_PANEL, text_color=TEXT_MAIN,
                button_color=BG_HEADER, button_hover_color=BG_HOVER,
                dropdown_fg_color=BG_PANEL, dropdown_text_color=TEXT_MAIN,
                command=lambda v: self._rename_var.set(v),
            ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 4))

        # Button row
        btn_frame = ctk.CTkFrame(self._frame, fg_color="transparent")
        btn_frame.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(2, 10))

        ctk.CTkButton(
            btn_frame, text="Replace All", width=110,
            text_color="white", command=self._on_all,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_frame, text="Replace Selected", width=130, fg_color="transparent", border_width=1,
            text_color=("black", "white"), command=self._on_selected,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_frame, text="Rename", width=90, fg_color="transparent", border_width=1,
            text_color=("black", "white"), command=self._on_rename,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_frame, text="Cancel", width=90, fg_color=BTN_CANCEL, hover_color=BTN_CANCEL_HOV,
            text_color="white", command=self._on_cancel,
        ).pack(side="left", padx=4)

        self.bind("<Escape>", lambda _e: self._on_cancel())
        self._center_and_show()
        if rename_conflict:
            self.after(60, self._on_rename)

    def _center_and_show(self):
        parent = self._parent_ref
        self.geometry(f"{self._WIDTH}")
        self.update_idletasks()
        # winfo_reqheight() returns scaled tk pixels; convert back to design
        # units so CTkToplevel.geometry() doesn't double-scale the value.
        scale = self._get_window_scaling() or 1
        req_h = self.winfo_reqheight() / scale
        final_h = int(max(req_h, self._HEIGHT))
        self.geometry(f"{self._WIDTH}x{final_h}")
        self.update_idletasks()
        if parent is not None:
            try:
                top = parent.winfo_toplevel()
                top.update_idletasks()
                px = top.winfo_rootx()
                py = top.winfo_rooty()
                pw = top.winfo_width()
                ph = top.winfo_height()
                aw = self.winfo_width()
                ah = self.winfo_height()
                if aw <= 1:
                    aw = scaled(self._WIDTH)
                if ah <= 1:
                    ah = scaled(final_h)
                self.geometry(f"+{px + (pw - aw) // 2}+{py + (ph - ah) // 2}")
            except Exception:
                pass
        self.deiconify()
        self.lift()
        self.focus_force()
        self.after(50, self._grab)

    def _grab(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _old_xy_set(self, event):
        self.old_x = event.x_root
        self.old_y = event.y_root

    def _move_window(self, event):
        if self.old_x is None or self.old_y is None:
            return
        dx = event.x_root - self.old_x
        dy = event.y_root - self.old_y
        self.geometry(f"+{self.winfo_x() + dx}+{self.winfo_y() + dy}")
        self.old_x = event.x_root
        self.old_y = event.y_root

    def _on_rename(self):
        if self._rename_frame is None:
            return
        self._rename_frame.grid()
        self.update_idletasks()
        scale = self._get_window_scaling() or 1
        req_h = self.winfo_reqheight() / scale
        new_h = int(max(req_h, self._HEIGHT))
        self.geometry(f"{self._WIDTH}x{new_h}")
        self.update_idletasks()
        parent = self._parent_ref
        if parent is not None:
            try:
                top = parent.winfo_toplevel()
                top.update_idletasks()
                px = top.winfo_rootx()
                py = top.winfo_rooty()
                pw = top.winfo_width()
                ph = top.winfo_height()
                aw = self.winfo_width() or scaled(self._WIDTH)
                ah = self.winfo_height() or scaled(new_h)
                self.geometry(f"+{px + (pw - aw) // 2}+{py + (ph - ah) // 2}")
            except Exception:
                pass
        self._rename_entry.focus_set()
        self._rename_entry.select_range(0, "end")

    def _on_rename_confirm(self):
        from gui.mod_name_utils import sanitize_mod_folder_name
        name = self._rename_var.get().strip() if self._rename_var else ""
        name = sanitize_mod_folder_name(name) if name else ""
        if not name or name == self._mod_name:
            return
        self.result = "rename"
        self.new_name = name
        self.grab_release()
        self.destroy()

    def _on_all(self):
        self.result = "all"
        self.grab_release()
        self.destroy()

    def _on_selected(self):
        self.result = "selected"
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.result = "cancel"
        self.grab_release()
        self.destroy()


class _SetPrefixDialog(ctk.CTkToplevel):
    """
    Modal dialog shown when a mod's top-level folders don't match any of the
    game's required folders.  result: ("prefix", path_str) | ("as_is", None) | None
    """

    _FONT_TITLE = (_theme.FONT_FAMILY, 14, "bold")
    _FONT_BODY  = (_theme.FONT_FAMILY, 13)
    _FONT_ENTRY = (_theme.FONT_FAMILY, 13)
    _FONT_TREE  = ("Courier New", 12)
    _FONT_BTN   = (_theme.FONT_FAMILY, 13)
    _FONT_BTN_B = (_theme.FONT_FAMILY, 13, "bold")

    def __init__(self, parent, required_folders: set[str],
                 file_list: list[tuple[str, str, bool]],
                 mod_name: str = ""):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Unexpected Mod Structure")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: tuple[str, str | None] | None = None
        self._required  = required_folders
        self._file_list = file_list
        self._mod_name  = (mod_name or "").strip()
        self._entry_var = tk.StringVar()
        self._entry_var.trace_add("write", self._on_entry_change)

        self._build()
        self._refresh_tree("")

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        row = 0
        if self._mod_name:
            ctk.CTkLabel(
                self,
                text=f"Mod: {self._mod_name}",
                font=self._FONT_TITLE,
                text_color=ACCENT,
                anchor="w",
            ).grid(row=row, column=0, sticky="ew", padx=16, pady=(16, 2))
            row += 1

        ctk.CTkLabel(
            self,
            text="This mod has no recognised top-level folders.",
            font=self._FONT_TITLE,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(2 if self._mod_name else (16, 2), 2))
        row += 1

        folders_str = ",  ".join(sorted(self._required))
        ctk.CTkLabel(
            self,
            text=f"Expected one of:  {folders_str}",
            font=self._FONT_BODY,
            text_color=TEXT_DIM,
            anchor="w",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 12))
        row += 1

        ctk.CTkLabel(
            self,
            text="Install all files under this path (e.g. archive/pc/mod):",
            font=self._FONT_BODY,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 4))
        row += 1

        self._entry = ctk.CTkEntry(
            self,
            textvariable=self._entry_var,
            font=self._FONT_ENTRY,
            fg_color=BG_PANEL,
            border_color=BORDER,
            text_color=TEXT_MAIN,
            height=36,
        )
        self._entry.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 8))
        self._entry.focus_set()
        row += 1

        tree_row = row
        self.grid_rowconfigure(tree_row, weight=1)
        tree_frame = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=6)
        tree_frame.grid(row=tree_row, column=0, sticky="nsew", padx=16, pady=(0, 10))
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        self._tree_text = tk.Text(
            tree_frame,
            font=self._FONT_TREE,
            bg=BG_PANEL,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            relief="flat",
            bd=0,
            highlightthickness=0,
            state="disabled",
            wrap="none",
            padx=8,
            pady=6,
        )
        tree_vsb = tk.Scrollbar(tree_frame, orient="vertical",
                                command=self._tree_text.yview)
        tree_hsb = tk.Scrollbar(tree_frame, orient="horizontal",
                                command=self._tree_text.xview)
        self._tree_text.configure(yscrollcommand=tree_vsb.set,
                                  xscrollcommand=tree_hsb.set)
        self._tree_text.grid(row=0, column=0, sticky="nsew")
        tree_vsb.grid(row=0, column=1, sticky="ns")
        tree_hsb.grid(row=1, column=0, sticky="ew")

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=56)
        bar.grid(row=tree_row + 1, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=100, height=32, font=self._FONT_BTN,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=12)
        ctk.CTkButton(
            bar, text="Install Anyway", width=140, height=32, font=self._FONT_BTN,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_as_is,
        ).pack(side="right", padx=4, pady=12)
        ctk.CTkButton(
            bar, text="Install with Prefix", width=160, height=32, font=self._FONT_BTN_B,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_prefix,
        ).pack(side="right", padx=4, pady=12)

        self.update_idletasks()
        w, h = 560, 540
        owner = self.master
        x = owner.winfo_rootx() + (owner.winfo_width()  - w) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _on_entry_change(self, *_):
        self._refresh_tree(self._entry_var.get())

    def _refresh_tree(self, prefix: str):
        prefix = prefix.strip().strip("/").replace("\\", "/")
        paths: list[str] = []
        for _, dst_rel, is_folder in self._file_list:
            if is_folder:
                continue
            dst = dst_rel.replace("\\", "/")
            if prefix:
                dst = f"{prefix}/{dst}"
            paths.append(dst)

        tree_str = _build_tree_str(paths)
        self._tree_text.configure(state="normal")
        self._tree_text.delete("1.0", "end")
        self._tree_text.insert("end", tree_str)
        self._tree_text.configure(state="disabled")

    def _on_prefix(self):
        self.result = ("prefix", self._entry_var.get())
        self.grab_release()
        self.destroy()

    def _on_as_is(self):
        self.result = ("as_is", None)
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.grab_release()
        self.destroy()


class _SelectFilesDialog(ctk.CTkToplevel):
    """
    Modal dialog that lists all files from the new archive and lets the user
    tick which ones to copy into the existing mod folder.
    result: set[str] of dst_rel paths to install, or None if cancelled.
    """

    def __init__(self, parent, file_list: list[tuple[str, str, bool]]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Select Files to Replace")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self.result: set[str] | None = None
        self._file_list = file_list
        self._vars: list[tuple[tk.BooleanVar, str]] = []

        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="Select files to copy into the existing mod folder:",
            font=FONT_NORMAL,
            text_color=TEXT_MAIN,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))

        scroll = ctk.CTkScrollableFrame(
            self, fg_color=BG_PANEL, corner_radius=6,
        )
        scroll.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        scroll.grid_columnconfigure(0, weight=1)

        row = 0
        for src_rel, dst_rel, is_folder in self._file_list:
            var = tk.BooleanVar(value=True)
            self._vars.append((var, dst_rel))
            ctk.CTkCheckBox(
                scroll,
                text=dst_rel or src_rel,
                variable=var,
                font=FONT_SMALL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                checkmark_color="white",
                border_color=BORDER,
            ).grid(row=row, column=0, sticky="w", padx=8, pady=2)
            row += 1

        helper = ctk.CTkFrame(self, fg_color="transparent")
        helper.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 4))
        ctk.CTkButton(
            helper, text="Select All", width=90, height=24, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=lambda: [v.set(True) for v, _ in self._vars],
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            helper, text="Select None", width=90, height=24, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=lambda: [v.set(False) for v, _ in self._vars],
        ).pack(side="left")

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=90, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=12)
        ctk.CTkButton(
            bar, text="Install Selected", width=120, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=12)

        self.after(50, self._position_window)

    def _position_window(self):
        self.update_idletasks()
        owner = self.master
        # Design units for W/H (CTk scales them); physical px for x/y.
        scale = self._get_window_scaling() or 1
        w = 520
        h = min(600, max(300, round(self.winfo_reqheight() / scale)))
        x = owner.winfo_rootx() + (owner.winfo_width() - round(w * scale)) // 2
        y = owner.winfo_rooty() + (owner.winfo_height() - round(h * scale)) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _on_ok(self):
        chosen = {dst for var, dst in self._vars if var.get()}
        if chosen:
            self.result = chosen
        self.grab_release()
        self.destroy()

    def _on_cancel(self):
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Per-mod plugin disabling dialog
# ---------------------------------------------------------------------------
class _DisablePluginsDialog(ctk.CTkToplevel):
    """Thin modal wrapper around DisablePluginsPanel.

    Callers access ``result`` (set[str] | None).
    """

    def __init__(self, parent, mod_name: str,
                 plugin_names: list[str], disabled: set[str]):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title(f"Disable Plugins — {mod_name}")
        self.resizable(False, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._make_modal)

        self.result: set[str] | None = None

        h = min(500, max(200, 80 + len(plugin_names) * 32 + 60 + 52))
        w = 400
        self.update_idletasks()
        try:
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            self.geometry(f"{w}x{h}")

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        def _on_done(panel):
            self.result = panel.result
            self._on_close()

        panel = DisablePluginsPanel(
            self,
            mod_name=mod_name,
            plugin_names=plugin_names,
            disabled=disabled,
            on_done=_on_done,
        )
        panel.grid(row=0, column=0, sticky="nsew")

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
# DisablePluginsPanel — inline overlay version of _DisablePluginsDialog
# ---------------------------------------------------------------------------

class DisablePluginsPanel(ctk.CTkFrame):
    """
    Inline panel version of _DisablePluginsDialog. Overlays _plugin_panel_container.
    result: set[str] of plugin names to disable, or None if cancelled.
    """

    def __init__(self, parent, mod_name: str,
                 plugin_names: list[str], disabled: set[str],
                 on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self.result: set[str] | None = None
        self._plugin_names = plugin_names
        self._disabled_lower = {n.lower() for n in disabled}
        self._on_done = on_done or (lambda p: None)
        self._vars: list[tuple[tk.BooleanVar, str]] = []

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Disable Plugins \u2014 {mod_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        self._build()

    def _build(self):
        ctk.CTkLabel(
            self,
            text="Checked plugins are enabled and will appear in plugins.txt.\n"
                 "Uncheck a plugin to exclude it.",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
            anchor="w",
            justify="left",
        ).pack(anchor="w", padx=16, pady=(12, 6))

        scroll = ctk.CTkScrollableFrame(self, fg_color=BG_PANEL, corner_radius=6)
        scroll.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        scroll.grid_columnconfigure(0, weight=1)

        for i, name in enumerate(self._plugin_names):
            var = tk.BooleanVar(value=name.lower() not in self._disabled_lower)
            self._vars.append((var, name))
            ctk.CTkCheckBox(
                scroll,
                text=name,
                variable=var,
                font=FONT_NORMAL,
                text_color=TEXT_MAIN,
                fg_color=ACCENT,
                hover_color=ACCENT_HOV,
                checkmark_color="white",
                border_color=BORDER,
            ).grid(row=i, column=0, sticky="w", padx=8, pady=3)

        helper = ctk.CTkFrame(self, fg_color="transparent")
        helper.pack(fill="x", padx=12, pady=(0, 4))
        ctk.CTkButton(
            helper, text="Enable All", width=90, height=24, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=lambda: [v.set(True) for v, _ in self._vars],
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            helper, text="Disable All", width=90, height=24, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=lambda: [v.set(False) for v, _ in self._vars],
        ).pack(side="left")

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=12)
        ctk.CTkButton(
            bar, text="Save", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=12)

    def _on_ok(self):
        self.result = {name for var, name in self._vars if not var.get()}
        self._on_done(self)

    def _on_cancel(self):
        self._on_done(self)


class BundleOptionsPanel(ctk.CTkFrame):
    """Inline panel to pick a RE/Fluffy bundle's active options.  Overlays
    _plugin_panel_container.  Select-one groups render as radios; independent
    ("Optional — any") groups as checkboxes with ▲/▼ reorder buttons.

    Selection + order are written straight into a deep-copied BundleSpec as the
    user interacts, so ``result`` is that spec on Save (None on Cancel).  Option
    order within a group is the override order: when two selected options write
    the same file, the one LOWER in the list wins (applied last).
    """

    def __init__(self, parent, mod_name: str, spec, on_done=None,
                 lib_dir=None, on_preview=None, on_preview_clear=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        import copy
        from pathlib import Path
        self.result = None
        self._spec = copy.deepcopy(spec)
        self._on_done = on_done or (lambda p: None)
        # Bundle library dir (<mod>/.mm_bundle) for resolving option screenshots,
        # and callbacks to show/clear the preview over the modlist panel.
        self._lib_dir = Path(lib_dir) if lib_dir else None
        self._on_preview = on_preview
        self._on_preview_clear = on_preview_clear

        # Per-option deployable file sets (lowercased mod-root rel paths), used to
        # flag options that write the same file as another selected option.  Built
        # once from the bundle library; empty when no lib_dir is available.
        self._opt_files: dict[str, set[str]] = {}
        if self._lib_dir is not None:
            from Utils.re_bundle import option_deployable_rels
            for g in self._spec.groups:
                for o in g.options:
                    if o.is_label:
                        continue
                    try:
                        self._opt_files[o.folder] = option_deployable_rels(
                            self._lib_dir, o.folder)
                    except Exception:
                        self._opt_files[o.folder] = set()
        # folder -> {"text": str, "marker": CTkLabel} for live conflict recolour.
        self._opt_widgets: dict[str, dict] = {}
        # Shared tooltip for option rows whose label was elided (long names).
        self._label_tooltip = TkTooltip(
            self, font=(_theme.FONT_FAMILY, _theme.FS10))

        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Bundle Options — {mod_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        ctk.CTkLabel(
            self,
            text="Choose which options are active. “Select one” groups allow a "
                 "single choice; optional add-ons can be combined.\n"
                 "When optional add-ons overlap, the one lower in the list wins "
                 "— use ▲/▼ to reorder.  Checking an add-on turns off any lower "
                 "one it fully replaces, so your choice wins.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(anchor="w", padx=16, pady=(12, 6))

        self._scroll = ctk.CTkScrollableFrame(self, fg_color=BG_PANEL, corner_radius=6)
        self._scroll.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self._scroll.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x")
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=12)
        ctk.CTkButton(
            bar, text="Save", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=12)

        self._build_rows()
        # Re-truncate option labels to the live panel width (like the modlist
        # column does on resize) so expanding the panel reveals more of each name.
        self._last_scroll_w = 0
        self._scroll.bind("<Configure>", self._on_scroll_resize, add="+")
        # Bind X11 wheel notches once; bind_scrollable_wheel re-walks descendants
        # on <Enter>, so rows rebuilt by a reorder keep scrolling without
        # re-binding (which would stack <Enter> handlers).
        bind_scrollable_wheel(self._scroll)

        # Show the first selected option's image as the initial preview.
        _first = next((o for g in self._spec.groups for o in g.options
                       if o.selected and not o.is_label), None)
        if _first is not None:
            self._preview_option(_first.folder)

    # Fixed row chrome reserved beside an option label, in pixels.  The label
    # column has a weighted spacer to its right, so the label only needs to clear
    # the checkbox/radio indicator (left) plus the widgets that sit *after* it:
    #   - checkbox/radio indicator + its text gap + the row's outer padding;
    #   - the ▲/▼ reorder buttons (checkbox rows only);
    #   - the '(overridden)' marker, but only on rows currently showing it (added
    #     per-row in :meth:`_label_max_px`, since it sits right after the label).
    _CHROME_INDICATOR_PX = 46    # checkbox/radio box + gap + row padx
    _CHROME_BUTTONS_PX = 78      # the two ▲/▼ buttons + their pads
    _MARKER_PX = 96              # width of the "(overridden)" marker text
    # Floor so a very narrow panel still shows a usable stub rather than "…".
    _LABEL_MIN_PX = 60

    def _scroll_width(self) -> int:
        """Current inner width of the option list (px)."""
        try:
            w = self._scroll.winfo_width()
        except Exception:
            w = 0
        if w <= 1:  # not yet realised — fall back to the panel's requested width
            try:
                w = self.winfo_reqwidth()
            except Exception:
                w = 420
        return w

    def _label_max_px(self, *, has_buttons: bool = True,
                      marker_shown: bool = False) -> int:
        """Available pixel width for an option label at the current panel width,
        reserving only the chrome that actually sits in this row."""
        reserve = self._CHROME_INDICATOR_PX
        if has_buttons:
            reserve += self._CHROME_BUTTONS_PX
        if marker_shown:
            reserve += self._MARKER_PX
        return max(self._LABEL_MIN_PX, self._scroll_width() - reserve)

    def _fit_label(self, text: str, *, has_buttons: bool = True,
                   marker_shown: bool = False) -> str:
        """Truncate *text* to the current available label width (pixel-aware)."""
        return _truncate_text(self._scroll, text, TK_FONT_NORMAL,
                              self._label_max_px(has_buttons=has_buttons,
                                                 marker_shown=marker_shown))

    def _option_tooltip_text(self, full_name: str, folder: str) -> str:
        """Tooltip body for an option row: the full name, plus the modinfo
        ``description=`` (when present) on following lines."""
        desc = ""
        if self._lib_dir is not None:
            try:
                from Utils.re_bundle import option_description
                desc = option_description(self._lib_dir, folder)
            except Exception:
                desc = ""
        return f"{full_name}\n\n{desc}" if desc else full_name

    def _maybe_tooltip(self, widget, full_text: str, folder: str = "") -> None:
        """Attach a tooltip showing the full name (the visible label may be
        elided) and, when *folder* resolves to a modinfo description, that text
        below it."""
        try:
            self._label_tooltip.attach(
                widget, self._option_tooltip_text(full_text, folder))
        except Exception:
            pass

    def _on_scroll_resize(self, _event=None) -> None:
        """Re-truncate every option label to the new panel width.  Skipped unless
        the panel width actually changed (the scroll frame fires <Configure> for
        height and child changes too)."""
        width = self._scroll_width()
        if width == getattr(self, "_last_scroll_w", 0):
            return
        self._last_scroll_w = width
        for w in self._opt_widgets.values():
            self._refit_row(w)

    def _refit_row(self, w: dict) -> None:
        """Re-truncate one row's label to the current width, reserving marker
        space only when that row is actually showing the '(overridden)' note."""
        lbl = w.get("label_widget")
        if lbl is None:
            return
        marker = w.get("marker")
        marker_shown = bool(marker is not None and str(marker.cget("text")))
        max_px = self._label_max_px(
            has_buttons=w.get("has_buttons", True), marker_shown=marker_shown)
        try:
            lbl.configure(text=_truncate_text(
                self._scroll, w["text"], TK_FONT_NORMAL, max_px))
        except Exception:
            pass

    def _build_rows(self):
        """(Re)build the option rows from the current spec.  Called on init and
        after any reorder so the displayed order matches ``group.options``."""
        for child in self._scroll.winfo_children():
            child.destroy()
        self._opt_widgets = {}

        row = 0
        for gi, group in enumerate(self._spec.groups):
            ctk.CTkLabel(
                self._scroll, text=group.name, font=FONT_BOLD, text_color=TEXT_MAIN,
                anchor="w",
            ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(12, 0)); row += 1
            ctk.CTkLabel(
                self._scroll, text=("Select one" if group.select_one else "Optional — any"),
                font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
            ).grid(row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 2)); row += 1

            if group.select_one:
                var = tk.IntVar(
                    value=next((i for i, o in enumerate(group.options) if o.selected), 0))
                def _pick(g=group, v=var):
                    for i, o in enumerate(g.options):
                        o.selected = (i == v.get()) and not o.is_label
                    self._recompute_conflicts()
                for oi, opt in enumerate(group.options):
                    if opt.is_label:
                        ctk.CTkLabel(
                            self._scroll, text=opt.label, font=FONT_SMALL,
                            text_color=TEXT_DIM, anchor="w",
                        ).grid(row=row, column=0, sticky="ew", padx=24, pady=(6, 0)); row += 1
                        continue
                    optrow = ctk.CTkFrame(self._scroll, fg_color="transparent")
                    optrow.grid(row=row, column=0, columnspan=2, sticky="ew",
                                padx=24, pady=2)
                    rb = ctk.CTkRadioButton(
                        optrow, text=self._fit_label(opt.label, has_buttons=False),
                        variable=var, value=oi,
                        command=_pick, font=FONT_NORMAL, text_color=TEXT_MAIN,
                        fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                    )
                    rb.grid(row=0, column=0, sticky="w")
                    self._maybe_tooltip(rb, opt.label, opt.folder)
                    marker = ctk.CTkLabel(
                        optrow, text="", font=FONT_SMALL, text_color=BG_RED_TEXT,
                        anchor="w",
                    )
                    marker.grid(row=0, column=1, sticky="w", padx=(8, 0))
                    self._opt_widgets[opt.folder] = {
                        "text": opt.label, "marker": marker, "label_widget": rb,
                        "has_buttons": False}
                    self._bind_hover_preview(optrow, opt.folder)
                    row += 1
            else:
                n = len(group.options)
                for oi, opt in enumerate(group.options):
                    if opt.is_label:
                        # Content-less divider / info entry — non-selectable label
                        # preserving the author's visual sectioning.
                        ctk.CTkLabel(
                            self._scroll, text=opt.label, font=FONT_SMALL,
                            text_color=TEXT_DIM, anchor="w",
                        ).grid(row=row, column=0, columnspan=2, sticky="ew",
                               padx=24, pady=(6, 0)); row += 1
                        continue
                    rowf = ctk.CTkFrame(self._scroll, fg_color="transparent")
                    rowf.grid(row=row, column=0, columnspan=2, sticky="ew", padx=18, pady=1)
                    # col 0 = checkbox, col 1 = conflict marker, col 2 = spacer
                    # (absorbs slack so the marker hugs the label), cols 3/4 = ▲/▼.
                    rowf.grid_columnconfigure(2, weight=1)
                    bvar = tk.BooleanVar(value=opt.selected)
                    def _toggle(o=opt, v=bvar):
                        o.selected = bool(v.get())
                        if o.selected:
                            # "Your click wins": if this option would be fully
                            # overridden by lower selected options, turn those
                            # overriders off so the one you just checked deploys.
                            self._promote_option(o.folder)
                        self._recompute_conflicts()
                    cb = ctk.CTkCheckBox(
                        rowf, text=self._fit_label(opt.label), variable=bvar,
                        command=_toggle, font=FONT_NORMAL, text_color=TEXT_MAIN,
                        fg_color=ACCENT, hover_color=ACCENT_HOV,
                        checkmark_color="white", border_color=BORDER,
                    )
                    cb.grid(row=0, column=0, sticky="w")
                    self._maybe_tooltip(cb, opt.label, opt.folder)
                    marker = ctk.CTkLabel(
                        rowf, text="", font=FONT_SMALL, text_color=BG_RED_TEXT,
                        anchor="w",
                    )
                    marker.grid(row=0, column=1, sticky="w", padx=(8, 0))
                    self._opt_widgets[opt.folder] = {
                        "text": opt.label, "marker": marker, "label_widget": cb,
                        "opt": opt, "var": bvar, "checkbox": cb, "has_buttons": True,
                    }
                    self._bind_hover_preview(cb, opt.folder)
                    self._bind_hover_preview(rowf, opt.folder)
                    ctk.CTkButton(
                        rowf, text="▲", width=26, height=24, font=FONT_SMALL,
                        fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
                        state=("normal" if oi > 0 else "disabled"),
                        command=lambda g=group, i=oi: self._move(g, i, -1),
                    ).grid(row=0, column=3, padx=(4, 0))
                    ctk.CTkButton(
                        rowf, text="▼", width=26, height=24, font=FONT_SMALL,
                        fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
                        state=("normal" if oi < n - 1 else "disabled"),
                        command=lambda g=group, i=oi: self._move(g, i, 1),
                    ).grid(row=0, column=4, padx=(4, 0))
                    row += 1

        self._recompute_conflicts()

    def _ordered_selected_folders(self) -> list[str]:
        """Selected option folders in deploy apply order, mirroring
        :func:`re_bundle._ordered_selected_folders`: select-one groups first (in
        declared order), independent groups last, and within a group the shown
        order (lower = applied later = wins)."""
        out: list[str] = []
        for g in self._spec.groups:
            if not g.select_one:
                continue
            out += [o.folder for o in g.options
                    if o.selected and not o.is_label]
        for g in self._spec.groups:
            if g.select_one:
                continue
            out += [o.folder for o in g.options
                    if o.selected and not o.is_label]
        return out

    def _promote_option(self, folder: str) -> None:
        """Make the just-checked *folder* win: turn off any other selected
        independent option that is a true *alternative* of it — i.e. one whose
        deployable file set is **identical** to *folder*'s (so the two write
        exactly the same files and only one can ever be visible).  This is the
        "your click wins" rule: checking one mesh/texture variant turns off the
        sibling variant writing the same files, regardless of list order.

        A subset/superset pair is NOT an alternative: the larger option ships
        files the smaller one doesn't, so enabling both still lets each
        contribute (the smaller wins the files it provides, the larger keeps its
        extras).  Those — and genuine partial-overlap add-ons (sharing only
        *some* files) — are left selected; apply order decides the overlap and
        :meth:`_recompute_conflicts` marks anything fully shadowed."""
        files = self._opt_files.get(folder, set())
        if not files:
            return  # no files → nothing to win or shadow

        for other, w in self._opt_widgets.items():
            if other == folder or "var" not in w:
                continue  # self, or a radio (can't empty its group)
            o = w["opt"]
            if not o.selected or o.is_label:
                continue
            ofiles = self._opt_files.get(other, set())
            if not ofiles:
                continue
            # Identical file sets → mutually-exclusive alternatives (e.g. two
            # recolours of the same texture).  Subset/superset pairs are kept:
            # the larger option still contributes the files the smaller lacks.
            if ofiles == files:
                o.selected = False
                try:
                    w["var"].set(False)
                except Exception:
                    pass

    def _recompute_conflicts(self):
        """Flag every selected option whose files are entirely overridden by
        another selected option, mirroring deploy order (see
        :meth:`_ordered_selected_folders`).  The LAST selected option to write a
        file wins it; an option none of whose files survive is marked
        '(overridden)' in red.  (Checking an option auto-promotes it past lower
        overriders via :meth:`_promote_option`, so a freshly-checked option won't
        be left overridden — this marks only the cases the user hasn't resolved,
        e.g. a select-one radio shadowed by an independent add-on.)
        """
        if not self._opt_widgets:
            return

        ordered = self._ordered_selected_folders()
        # winners[rel] = folder of the LAST selected option providing that file.
        winners: dict[str, str] = {}
        for folder in ordered:
            for rel in self._opt_files.get(folder, ()):
                winners[rel] = folder

        selected = set(ordered)
        for folder, w in self._opt_widgets.items():
            marker = w["marker"]
            files = self._opt_files.get(folder, set())
            was_shown = bool(str(marker.cget("text")))
            now_shown = bool(folder in selected and files and not any(
                winners.get(rel) == folder for rel in files))
            if now_shown:
                marker.configure(text="(overridden)", text_color=BG_RED_TEXT)
            else:
                marker.configure(text="")
            # Marker toggling changes the room left for the label, so re-fit it.
            if now_shown != was_shown:
                self._refit_row(w)

    def _move(self, group, idx: int, delta: int):
        """Swap option *idx* with its neighbour and rebuild the rows."""
        j = idx + delta
        if 0 <= j < len(group.options):
            group.options[idx], group.options[j] = group.options[j], group.options[idx]
            self._build_rows()

    def _bind_hover_preview(self, widget, folder: str) -> None:
        """Show the option's image while the pointer is over *widget*.  Bound on
        the widget and its internal children so hover is reliable across the CTk
        label/check parts.  The preview is not cleared on leave (the next hover
        replaces it) to avoid flicker when moving between rows."""
        if self._on_preview is None or self._lib_dir is None:
            return
        def _enter(_e=None, f=folder):
            self._preview_option(f)
        def _walk(w):
            try:
                w.bind("<Enter>", _enter, add="+")
            except Exception:
                pass
            for child in w.winfo_children():
                _walk(child)
        _walk(widget)

    def _preview_option(self, folder: str) -> None:
        """Show the option's screenshot in the modlist-panel preview, or clear
        the preview if it has no image."""
        if self._on_preview is None or self._lib_dir is None:
            return
        try:
            from Utils.re_bundle import option_image
            img = option_image(self._lib_dir, folder)
        except Exception:
            img = None
        if img is not None:
            self._on_preview(img)
        else:
            self._clear_preview()

    def _clear_preview(self) -> None:
        if self._on_preview_clear is not None:
            try:
                self._on_preview_clear()
            except Exception:
                pass

    def _on_ok(self):
        # Selection + order already live in self._spec (written on each interaction).
        self.result = self._spec
        self._on_done(self)

    def _on_cancel(self):
        self.result = None
        self._on_done(self)


# ---------------------------------------------------------------------------
# Download Custom Handler panel — overlay to download JSON handlers from GitHub
# ---------------------------------------------------------------------------

_CUSTOM_HANDLERS_SUBFOLDER_TEMPLATE = (
    "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/contents/"
    "Custom%20Handlers/{folder}?ref=Resources"
)


def _list_compatible_handlers(gh_fetch_text):
    """Return handler entries from the repo that the running app can use.

    Walks the root ``Custom Handlers/`` folder plus every ``X.Y/`` version
    subfolder whose major.minor is <= the running app's major.minor. Also
    drops any individual handler whose ``min_app_version`` field exceeds
    the running app's major.minor.

    Returns ``None`` on network failure, or a list of GitHub content entries
    (each having ``name``, ``download_url``, and a ``_min_app_version`` hint).
    """
    import json as _json
    import re as _re
    from version import __version__
    from gui.version_check import _meets_min_app_version, _major_minor

    try:
        listing = gh_fetch_text(_CUSTOM_HANDLERS_API_URL, timeout=15)
        if listing is None:
            return None
        root_data = _json.loads(listing)
    except Exception:
        return None

    have_mm = _major_minor(__version__)

    handlers: list[dict] = []
    seen: set[str] = set()

    def _add(entry: dict, default_min: str = "") -> None:
        name = entry.get("name", "")
        if not name.endswith(".json") or name in seen:
            return
        if default_min and "_min_app_version" not in entry:
            entry["_min_app_version"] = default_min
        seen.add(name)
        handlers.append(entry)

    # Collect compatible version subfolders, sorted highest first so a
    # newer-version copy of the same filename shadows the root copy.
    version_dirs: list[tuple[tuple[int, int], dict]] = []
    root_files: list[dict] = []
    for entry in root_data:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type", "")
        name = entry.get("name", "")
        if kind == "file" and name.endswith(".json"):
            root_files.append(entry)
        elif kind == "dir" and _re.fullmatch(r"\d+\.\d+", name):
            folder_mm = _major_minor(name)
            if folder_mm is None or have_mm is None or folder_mm > have_mm:
                continue
            version_dirs.append((folder_mm, entry))

    version_dirs.sort(key=lambda t: t[0], reverse=True)
    for _, dir_entry in version_dirs:
        folder_name = dir_entry.get("name", "")
        try:
            sub_listing = gh_fetch_text(
                _CUSTOM_HANDLERS_SUBFOLDER_TEMPLATE.format(folder=folder_name),
                timeout=15,
            )
            if sub_listing is None:
                continue
            sub_data = _json.loads(sub_listing)
        except Exception:
            continue
        for sub_entry in sub_data:
            if isinstance(sub_entry, dict) and sub_entry.get("type") == "file":
                _add(sub_entry, default_min=folder_name)

    for entry in root_files:
        _add(entry)

    # Per-file min_app_version gate (in case a file in the root folder is
    # gated, or to harden against a misplaced file in a version subfolder).
    compatible: list[dict] = []
    for h in handlers:
        download_url = h.get("download_url")
        if not download_url:
            continue
        try:
            raw = gh_fetch_text(
                download_url, accept="*/*", timeout=10, min_interval=1800,
            )
            if raw is None:
                # Couldn't fetch — fall back to the folder hint if present.
                if _meets_min_app_version(h.get("_min_app_version", ""), __version__):
                    compatible.append(h)
                continue
            try:
                parsed = _json.loads(raw)
            except Exception:
                continue
            min_ver = ""
            if isinstance(parsed, dict):
                min_ver = str(parsed.get("min_app_version", "")).strip()
                if isinstance(parsed.get("name"), str):
                    h["_display_name"] = parsed["name"]
            if not min_ver:
                min_ver = h.get("_min_app_version", "")
            h["_min_app_version"] = min_ver
            if _meets_min_app_version(min_ver, __version__):
                compatible.append(h)
        except Exception:
            continue
    return compatible


class DownloadCustomHandlerPanel(ctk.CTkFrame):
    """
    Overlay on the plugin panel listing custom game handlers from GitHub.
    User can download .json files into ~/.config/AmethystModManager/custom_games/
    """

    def __init__(self, parent, on_done=None, on_downloaded=None, log_fn=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_done = on_done or (lambda p: None)
        self._on_downloaded = on_downloaded or (lambda: None)
        self._log_fn = log_fn or (lambda msg: None)
        self._handlers: list[dict] = []
        self._status_var = tk.StringVar(value="Loading …")

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text="Download Custom Handler",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Content
        ctk.CTkLabel(
            self,
            text="Handlers from the Amethyst Mod Manager repository",
            font=FONT_SMALL,
            text_color=TEXT_DIM,
            anchor="w",
            justify="left",
        ).pack(anchor="w", padx=16, pady=(12, 4))

        self._status_lbl = ctk.CTkLabel(
            self, textvariable=self._status_var,
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
        )
        self._status_lbl.pack(anchor="w", padx=16, pady=(0, 8))

        self._scroll = ctk.CTkScrollableFrame(self, fg_color=BG_PANEL, corner_radius=6)
        self._scroll.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self._scroll.grid_columnconfigure(0, weight=1)

        # Bottom bar
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(side="top", fill="x")
        ctk.CTkButton(
            bar, text="Close", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=(4, 12), pady=12)

        # Fetch handlers in background
        threading.Thread(target=self._fetch_handlers, daemon=True).start()

    def _fetch_handlers(self):
        """Fetch the list of JSON files from GitHub API and extract game names."""
        from Utils.gh_cache import fetch_text as _gh_fetch_text
        try:
            handlers = _list_compatible_handlers(_gh_fetch_text)
            if handlers is None:
                self.after(0, lambda: self._on_fetch_error("Unable to reach GitHub"))
                return
            for h in handlers:
                if "_display_name" not in h:
                    h["_display_name"] = h.get("name", "").removesuffix(".json").replace("_", " ")
            self.after(0, lambda: self._on_handlers_loaded(handlers))
        except Exception as e:
            self.after(0, lambda: self._on_fetch_error(str(e)))

    def _on_handlers_loaded(self, handlers: list):
        self._handlers = handlers
        self._status_var.set(f"{len(handlers)} handler(s) available" if handlers else "No handlers found")
        for row, h in enumerate(handlers):
            display_name = h.get("_display_name", h.get("name", ""))
            filename = h.get("name", "")
            download_url = h.get("download_url")
            if not download_url:
                continue
            row_frame = ctk.CTkFrame(self._scroll, fg_color="transparent")
            row_frame.grid(row=row, column=0, sticky="ew", padx=8, pady=3)
            row_frame.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                row_frame, text=display_name, font=FONT_NORMAL, text_color=TEXT_MAIN,
                anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=(0, 8))
            ctk.CTkButton(
                row_frame, text="Download", width=90, height=24, font=FONT_SMALL,
                fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
                command=lambda u=download_url, n=filename: self._download_handler(u, n),
            ).grid(row=0, column=1, padx=4, pady=2)

    def _on_fetch_error(self, err: str):
        self._status_var.set(f"Error loading list: {err}")
        self._log_fn(f"Download Custom Handler: {err}")

    def _download_handler(self, download_url: str, filename: str):
        """Download a handler JSON and save to custom_games dir."""
        def _do():
            import json as _json
            import urllib.request as _urllib
            try:
                req = _urllib.Request(download_url, headers={"User-Agent": "Amethyst-Mod-Manager"})
                with _urllib.urlopen(req, timeout=15) as resp:
                    data = resp.read().decode("utf-8", errors="replace")
                # Validate JSON
                _json.loads(data)
                dest = get_custom_games_dir() / filename
                dest.write_text(data, encoding="utf-8")
                self.after(0, lambda: self._on_download_done(filename, None))
            except Exception as e:
                self.after(0, lambda: self._on_download_done(filename, str(e)))

        self._status_var.set(f"Downloading {filename} …")
        threading.Thread(target=_do, daemon=True).start()

    def _on_download_done(self, filename: str, err: str | None):
        if err:
            self._status_var.set(f"Error: {err}")
            self._log_fn(f"Download Custom Handler: failed to download {filename}: {err}")
        else:
            self._status_var.set(f"Saved to custom_games: {filename}")
            self._log_fn(f"Download Custom Handler: saved {filename} to custom_games folder")
            self._on_downloaded()  # Refresh game picker so new handler appears

    def _on_close(self):
        self._on_done(self)


# ---------------------------------------------------------------------------
# SepColorPanel — inline overlay for separator color picker
# ---------------------------------------------------------------------------
class SepColorPanel(ctk.CTkFrame):
    """
    Inline panel that overlays _plugin_panel_container for picking a separator colour.
    Shows a HSV colour wheel, a brightness slider, a live hex entry,
    and a live colour-preview swatch.

    on_result(hex_color: str | None, reset: bool) is called when the user
    confirms, resets, or cancels.  hex_color is "#rrggbb" or None (cancel/reset).
    on_done(panel) is called afterwards so the host can hide the overlay.
    """

    _WHEEL_SIZE = 200
    _SLIDER_H   = 20

    def __init__(self, parent, sep_name: str, initial_color: str | None = None,
                 on_result=None, on_done=None, title: str | None = None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._sep_name  = sep_name
        self._title     = title  # overrides the default "Separator Color — …" header when set
        self._on_result = on_result or (lambda hex_color, reset: None)
        self._on_done   = on_done   or (lambda p: None)

        self._hue: float = 0.0
        self._sat: float = 0.8
        self._val: float = 0.7

        if initial_color:
            try:
                r, g, b = (int(initial_color[i:i+2], 16) for i in (1, 3, 5))
                self._hue, self._sat, self._val = colorsys.rgb_to_hsv(r/255, g/255, b/255)
            except Exception:
                pass

        self._wheel_img: _PilTk.PhotoImage | None = None
        self._slider_img: _PilTk.PhotoImage | None = None
        self._suppress_hex_trace = False

        self._build()
        self._draw_wheel()
        self._draw_slider()
        self._update_all()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build(self):
        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        header_text = self._title if self._title else f"Separator Color \u2014 {self._sep_name}"
        ctk.CTkLabel(
            title_bar, text=header_text,
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="center",
        ).pack(expand=True, fill="both", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel_color,
        ).place(relx=1.0, rely=0.5, anchor="e", x=-4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Inner content — fixed-width centred column
        outer = ctk.CTkFrame(self, fg_color=BG_DEEP, corner_radius=0)
        outer.pack(fill="both", expand=True)

        PAD = 16
        ws  = self._WHEEL_SIZE
        col = ctk.CTkFrame(outer, fg_color=BG_DEEP, corner_radius=0)
        col.pack(anchor="center", pady=(PAD, PAD))

        # Colour wheel (centred in the fixed-width column)
        self._wheel_canvas = tk.Canvas(
            col, width=ws, height=ws,
            bg=BG_DEEP, highlightthickness=0, cursor="crosshair",
        )
        self._wheel_canvas.pack(pady=(0, 8))
        self._wheel_canvas.bind("<ButtonPress-1>", self._on_wheel_press)
        self._wheel_canvas.bind("<B1-Motion>",      self._on_wheel_drag)
        self._cross_h = self._wheel_canvas.create_line(0,0,0,0, fill="white", width=1)
        self._cross_v = self._wheel_canvas.create_line(0,0,0,0, fill="white", width=1)

        # Brightness slider
        self._slider_canvas = tk.Canvas(
            col, width=ws, height=self._SLIDER_H,
            bg=BG_DEEP, highlightthickness=0, cursor="sb_h_double_arrow",
        )
        self._slider_canvas.pack(fill="x", pady=(0, 10))
        self._slider_canvas.bind("<ButtonPress-1>", self._on_slider_press)
        self._slider_canvas.bind("<B1-Motion>",      self._on_slider_drag)
        self._slider_thumb = self._slider_canvas.create_rectangle(
            0, 0, 0, self._SLIDER_H, outline="white", width=2,
        )

        # Preview swatch
        self._swatch = tk.Frame(col, height=scaled(32), bg=BG_DEEP, relief="flat", bd=0,
                                highlightthickness=1, highlightbackground=BORDER)
        self._swatch.pack(fill="x", pady=(0, 10))

        # Hex entry row (centred)
        hex_row = ctk.CTkFrame(col, fg_color=BG_DEEP, corner_radius=0)
        hex_row.pack(pady=(0, 12))
        tk.Label(
            hex_row, text="#", bg=BG_DEEP, fg=TEXT_SEP,
            font=font_sized_px(_theme.FONT_FAMILY, 13),
        ).pack(side="left", padx=(0, 4))
        self._hex_var = tk.StringVar()
        self._hex_entry = tk.Entry(
            hex_row, textvariable=self._hex_var,
            bg=BG_PANEL, fg=TEXT_MAIN, insertbackground=TEXT_MAIN,
            relief="flat", font=font_sized_px(_theme.FONT_FAMILY, 13),
            bd=4, width=8, justify="center",
        )
        self._hex_entry.pack(side="left")
        self._hex_var.trace_add("write", self._on_hex_typed)

        # RGB sliders
        self._rgb_vars: dict[str, tk.IntVar] = {}
        self._rgb_labels: dict[str, tk.Label] = {}
        self._suppress_rgb_trace = False
        for channel in ("R", "G", "B"):
            srow = ctk.CTkFrame(col, fg_color=BG_DEEP, corner_radius=0)
            srow.pack(fill="x", pady=(0, 4))
            tk.Label(
                srow, text=channel, bg=BG_DEEP, fg=TEXT_SEP,
                font=font_sized_px(_theme.FONT_FAMILY, 12), width=2,
            ).pack(side="left", padx=(0, 6))
            var = tk.IntVar(value=0)
            self._rgb_vars[channel] = var
            sl = ctk.CTkSlider(
                srow, from_=0, to=255, number_of_steps=255,
                variable=var, height=16,
                command=lambda _v, ch=channel: self._on_rgb_slider(ch),
            )
            sl.pack(side="left", fill="x", expand=True)
            val_label = tk.Label(
                srow, text="0", bg=BG_DEEP, fg=TEXT_MAIN,
                font=font_sized_px(_theme.FONT_FAMILY, 12), width=4, anchor="e",
            )
            val_label.pack(side="left", padx=(8, 0))
            self._rgb_labels[channel] = val_label

        # Button bar
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        bar.pack(side="bottom", fill="x")
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")
        btn_inner = ctk.CTkFrame(bar, fg_color=BG_PANEL, corner_radius=0)
        btn_inner.pack(padx=12, pady=10)
        ctk.CTkButton(
            btn_inner, text="Reset to default", width=140, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_reset,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_inner, text="Cancel", width=90, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel_color,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            btn_inner, text="OK", width=90, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok,
        ).pack(side="left", padx=4)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    def _draw_wheel(self):
        import math
        ws  = self._WHEEL_SIZE
        img = _PilImage.new("RGB", (ws, ws), BG_DEEP)
        px  = img.load()
        cx  = cy = ws / 2
        r   = ws / 2 - 2
        for y in range(ws):
            for x in range(ws):
                dx, dy = x - cx, y - cy
                dist = (dx*dx + dy*dy) ** 0.5
                if dist <= r:
                    hue = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
                    sat = dist / r
                    rv, gv, bv = colorsys.hsv_to_rgb(hue, sat, self._val)
                    px[x, y] = (int(rv*255), int(gv*255), int(bv*255))
        self._wheel_img = _PilTk.PhotoImage(img)
        self._wheel_canvas.create_image(0, 0, anchor="nw", image=self._wheel_img)
        self._wheel_canvas.tag_raise(self._cross_h)
        self._wheel_canvas.tag_raise(self._cross_v)

    def _draw_slider(self):
        ws  = self._WHEEL_SIZE
        sh  = self._SLIDER_H
        img = _PilImage.new("RGB", (ws, sh))
        drw = _PilDraw.Draw(img)
        rv, gv, bv = colorsys.hsv_to_rgb(self._hue, self._sat, 1.0)
        for x in range(ws):
            t  = x / max(ws - 1, 1)
            drw.line([(x, 0), (x, sh)],
                     fill=(int(rv*t*255), int(gv*t*255), int(bv*t*255)))
        self._slider_img = _PilTk.PhotoImage(img)
        self._slider_canvas.create_image(0, 0, anchor="nw", image=self._slider_img)
        self._slider_canvas.tag_raise(self._slider_thumb)

    def _update_crosshair(self):
        import math
        ws  = self._WHEEL_SIZE
        cx  = cy = ws / 2
        r   = ws / 2 - 2
        angle = self._hue * 2 * math.pi
        px_ = cx + self._sat * r * math.cos(angle)
        py_ = cy - self._sat * r * math.sin(angle)
        arm = 6
        self._wheel_canvas.coords(self._cross_h, px_-arm, py_, px_+arm, py_)
        self._wheel_canvas.coords(self._cross_v, px_, py_-arm, px_, py_+arm)
        rv, gv, bv = colorsys.hsv_to_rgb(self._hue, self._sat, self._val)
        lum = 0.2126*rv + 0.7152*gv + 0.0722*bv
        col = TEXT_BLACK if lum > 0.5 else TEXT_WHITE
        self._wheel_canvas.itemconfigure(self._cross_h, fill=col)
        self._wheel_canvas.itemconfigure(self._cross_v, fill=col)

    def _update_slider_thumb(self):
        ws  = self._WHEEL_SIZE
        sh  = self._SLIDER_H
        tx  = int(self._val * (ws - 1))
        hw  = 5
        self._slider_canvas.coords(
            self._slider_thumb,
            max(0, tx-hw), 0, min(ws, tx+hw), sh,
        )

    def _current_hex(self) -> str:
        rv, gv, bv = colorsys.hsv_to_rgb(self._hue, self._sat, self._val)
        return "#{:02x}{:02x}{:02x}".format(int(rv*255), int(gv*255), int(bv*255))

    def _update_all(self, redraw_wheel=False, redraw_slider=False):
        if redraw_wheel:
            self._draw_wheel()
        if redraw_slider:
            self._draw_slider()
        self._update_crosshair()
        self._update_slider_thumb()
        cur = self._current_hex()
        self._swatch.configure(bg=cur)
        new_hex = cur[1:]
        self._suppress_hex_trace = True
        if self._hex_var.get().lower() != new_hex:
            self._hex_var.set(new_hex)
        self._suppress_hex_trace = False
        # Sync RGB sliders from current HSV (without retriggering _on_rgb_slider)
        if getattr(self, "_rgb_vars", None):
            rv, gv, bv = colorsys.hsv_to_rgb(self._hue, self._sat, self._val)
            rgb = {"R": int(round(rv * 255)), "G": int(round(gv * 255)), "B": int(round(bv * 255))}
            self._suppress_rgb_trace = True
            for ch, v in rgb.items():
                if self._rgb_vars[ch].get() != v:
                    self._rgb_vars[ch].set(v)
                self._rgb_labels[ch].configure(text=str(v))
            self._suppress_rgb_trace = False

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def _wheel_xy_to_hs(self, x, y):
        import math
        ws  = self._WHEEL_SIZE
        cx  = cy = ws / 2
        r   = ws / 2 - 2
        dx, dy = x - cx, y - cy
        dist   = min((dx*dx + dy*dy) ** 0.5, r)
        hue    = (math.atan2(-dy, dx) / (2 * math.pi)) % 1.0
        sat    = dist / r
        return hue, sat

    def _on_wheel_press(self, event):
        self._hue, self._sat = self._wheel_xy_to_hs(event.x, event.y)
        self._update_all(redraw_slider=True)

    def _on_wheel_drag(self, event):
        self._hue, self._sat = self._wheel_xy_to_hs(event.x, event.y)
        self._update_all(redraw_slider=True)

    def _on_slider_press(self, event):
        self._val = max(0.0, min(1.0, event.x / max(self._WHEEL_SIZE - 1, 1)))
        self._update_all(redraw_wheel=True)

    def _on_slider_drag(self, event):
        self._val = max(0.0, min(1.0, event.x / max(self._WHEEL_SIZE - 1, 1)))
        self._update_all(redraw_wheel=True)

    def _on_hex_typed(self, *_):
        if self._suppress_hex_trace:
            return
        raw = self._hex_var.get().strip().lstrip("#")
        if len(raw) == 6:
            try:
                r = int(raw[0:2], 16)
                g = int(raw[2:4], 16)
                b = int(raw[4:6], 16)
                h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
                self._hue, self._sat, self._val = h, s, v
                self._update_all(redraw_wheel=True, redraw_slider=True)
            except ValueError:
                pass

    def _on_rgb_slider(self, _channel: str):
        if self._suppress_rgb_trace:
            return
        r = max(0, min(255, int(self._rgb_vars["R"].get())))
        g = max(0, min(255, int(self._rgb_vars["G"].get())))
        b = max(0, min(255, int(self._rgb_vars["B"].get())))
        h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
        self._hue, self._sat, self._val = h, s, v
        self._update_all(redraw_wheel=True, redraw_slider=True)

    def _on_ok(self):
        self._on_result(self._current_hex(), False)
        self._on_done(self)

    def _on_reset(self):
        self._on_result(None, True)
        self._on_done(self)

    def _on_cancel_color(self):
        self._on_result(None, False)
        self._on_done(self)


# ---------------------------------------------------------------------------
# _ExeFilterDialog
# ---------------------------------------------------------------------------

class _ExeFilterDialog(ctk.CTkToplevel):
    """Thin modal wrapper around ExeFilterPanel."""

    def __init__(self, parent, load_fn, save_fn, refresh_fn, **_kwargs):
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("EXE Filter List")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        panel = ExeFilterPanel(
            self,
            load_fn=load_fn, save_fn=save_fn, refresh_fn=refresh_fn,
            on_done=lambda p: self._on_close(),
        )
        panel.grid(row=0, column=0, sticky="nsew")

        _center_dialog(self, parent, 440, 475)

        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _on_close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


# ---------------------------------------------------------------------------
# ExeFilterPanel — inline overlay version of _ExeFilterDialog
# ---------------------------------------------------------------------------

class ExeFilterPanel(ctk.CTkFrame):
    """Inline panel version of _ExeFilterDialog. Overlays the plugin-panel container."""

    def __init__(self, parent, load_fn, save_fn, refresh_fn, on_done=None, **_kwargs):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._load_fn = load_fn
        self._save_fn = save_fn
        self._refresh_fn = refresh_fn
        self._on_done = on_done or (lambda p: None)
        self._items: list[str] = list(load_fn())

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text="EXE Filter List",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        self._build()
        self.bind("<Return>", lambda _: (self._on_add(), "break")[1])

    def _build(self):
        ctk.CTkLabel(
            self,
            text=(
                "User-hidden executables are listed here.\n"
                "Use the \u2699 Configure button on any EXE to hide or unhide it."
            ),
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).pack(padx=14, pady=(10, 8), anchor="w")

        self._list_frame = ctk.CTkScrollableFrame(
            self, fg_color=BG_PANEL, corner_radius=6,
        )
        self._list_frame.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        self._list_frame.grid_columnconfigure(0, weight=1)
        self._refresh_list()

        add_row = ctk.CTkFrame(self, fg_color="transparent")
        add_row.pack(fill="x", padx=14, pady=(0, 10))
        add_row.grid_columnconfigure(0, weight=1)

        self._entry_var = tk.StringVar()
        self._entry_widget = ctk.CTkEntry(
            add_row, textvariable=self._entry_var, font=FONT_SMALL,
            fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER,
            placeholder_text="e.g.  my_helper.exe",
        )
        self._entry_widget.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(
            add_row, text="Add", width=72, height=28, font=FONT_SMALL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_add,
        ).grid(row=0, column=1)

        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x"
        )
        ctk.CTkButton(
            bar, text="Close", width=80, height=30, font=FONT_NORMAL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_close,
        ).pack(side="right", padx=12, pady=10)

    def _refresh_list(self):
        for w in self._list_frame.winfo_children():
            w.destroy()

        user_items = sorted(self._items)

        if not user_items:
            ctk.CTkLabel(
                self._list_frame,
                text="(no user-hidden executables)",
                font=FONT_SMALL, text_color=TEXT_DIM,
            ).grid(row=0, column=0, pady=10)
            return

        for row_idx, name in enumerate(user_items):
            row = ctk.CTkFrame(self._list_frame, fg_color="transparent")
            row.grid(row=row_idx, column=0, sticky="ew", pady=1)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                row, text=name, font=FONT_SMALL, text_color=TEXT_MAIN, anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=8)
            ctk.CTkButton(
                row, text="\u2715", width=28, height=24, font=FONT_SMALL,
                fg_color=BG_HEADER, hover_color=BTN_DANGER_ALT, text_color=TEXT_MAIN,
                command=lambda n=name: self._on_remove(n),
            ).grid(row=0, column=1, padx=(4, 4))

    def _on_add(self):
        raw = self._entry_var.get().strip()
        if not raw:
            return
        name = raw.lower()
        if name not in self._items:
            self._items.append(name)
            self._save_fn(self._items)
            self._refresh_fn()
        self._entry_var.set("")
        self._refresh_list()
        try:
            self._entry_widget.focus_set()
        except Exception:
            pass

    def _on_remove(self, name: str):
        if name in self._items:
            self._items.remove(name)
            self._save_fn(self._items)
            self._refresh_fn()
        self._refresh_list()

    def _on_close(self):
        self._on_done(self)

# ---------------------------------------------------------------------------
# IniFileEditorPanel — inline overlay for editing ini/json files
# ---------------------------------------------------------------------------

class IniFileEditorPanel(ctk.CTkFrame):
    """Inline panel (overlays _mod_panel_container) for editing ini/json files.
    Shows a text editor with Save and Cancel. Calls on_done(panel) on close."""

    def __init__(self, parent, file_path: str, rel_path: str, mod_name: str,
                 on_done=None, highlight: "str | None" = None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._file_path = Path(file_path)
        self._rel_path = rel_path
        self._mod_name = mod_name
        self._on_done = on_done or (lambda p: None)
        self._original_content: str | None = None
        self._highlight: "str | None" = highlight or None
        # Match navigation state
        self._match_starts: "list[str]" = []   # tk text indices ("line.col") of each match
        self._match_idx: int = -1
        self._active_needle: str = ""           # current highlighted term (find box or initial)
        self._SCROLL_W = 16
        self._scroll_first = 0.0
        self._scroll_last = 1.0
        self._thumb_drag_offset: "float | None" = None
        self._marker_after_id: "str | None" = None

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar,
            text=f"{rel_path} \u2014 {mod_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # In-editor search bar — live-highlights matches, drives the marker
        # strip + prev/next navigation (same machinery as a content-search hit).
        search_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0)
        search_bar.pack(fill="x")
        ctk.CTkLabel(
            search_bar, text="Find:", font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(side="left", padx=(12, 6), pady=6)
        self._find_var = tk.StringVar()
        self._find_entry = ctk.CTkEntry(
            search_bar, textvariable=self._find_var, height=26,
            font=FONT_SMALL, fg_color=BG_DEEP, text_color=TEXT_MAIN,
            border_color=BORDER, corner_radius=4,
        )
        self._find_entry.pack(side="left", padx=(0, 8), pady=6, fill="x", expand=True)
        self._find_var.trace_add("write", self._on_find_changed)
        self._find_entry.bind("<Return>",       lambda _e: self._goto_next_match())
        self._find_entry.bind("<Shift-Return>", lambda _e: self._goto_prev_match())
        self._find_entry.bind("<Escape>",       lambda _e: self._find_var.set(""))
        self._find_debounce_id: "str | None" = None

        # Text editor + combined scrollbar/marker strip (same pattern as the
        # Ini Files list and modlist panels: one canvas paints trough, thumb,
        # and a tick for every search match).
        editor_frame = tk.Frame(self, bg=BG_PANEL, highlightthickness=0)
        editor_frame.pack(fill="both", expand=True, padx=12, pady=12)
        editor_frame.rowconfigure(0, weight=1)
        editor_frame.columnconfigure(0, weight=1)

        self._textbox = ctk.CTkTextbox(
            editor_frame, font=FONT_MONO, fg_color=BG_DEEP, text_color=TEXT_MAIN,
            wrap="none", corner_radius=4, border_width=1, border_color=BORDER,
        )
        self._textbox.grid(row=0, column=0, sticky="nsew")
        # Underlying tk.Text — drives the marker strip via yscrollcommand.
        # CTkTextbox already owns the inner Text's yscrollcommand (its own
        # scrollbar) and re-asserts it via a periodic check loop, so we keep its
        # callback and chain our marker repaint after it instead of replacing it.
        self._text = self._textbox._textbox
        self._ctk_yset = self._textbox._y_scrollbar.set
        self._text.configure(yscrollcommand=self._scroll_set)
        # Suppress CTk's built-in vertical scrollbar — we provide the strip.
        # Neutralise its auto-show check loop so it stays hidden. A
        # `self.after(50, self._check_if_scrollbars_needed, ...)` call queued in
        # CTkTextbox.__init__ already captured the *original* bound method, so a
        # plain attribute override isn't enough — also stop it from ever
        # re-gridding the y scrollbar.
        self._textbox._hide_y_scrollbar = True
        self._textbox._check_if_scrollbars_needed = lambda *a, **k: None
        _orig_grid = self._textbox._create_grid_for_text_and_scrollbars
        def _grid_no_yscroll(*a, **k):
            k["re_grid_y_scrollbar"] = False
            _orig_grid(*a, **k)
            try:
                self._textbox._y_scrollbar.grid_forget()
            except Exception:
                pass
        self._textbox._create_grid_for_text_and_scrollbars = _grid_no_yscroll
        try:
            self._textbox._y_scrollbar.grid_forget()
        except Exception:
            pass

        self._marker_strip = tk.Canvas(
            editor_frame, bg=BG_DEEP, bd=0, highlightthickness=0,
            width=self._SCROLL_W, takefocus=0,
        )
        self._marker_strip.grid(row=0, column=1, sticky="ns", padx=(2, 0))
        self._marker_strip.bind("<Configure>",       self._on_marker_resize)
        self._marker_strip.bind("<ButtonPress-1>",   self._on_scrollbar_press)
        self._marker_strip.bind("<B1-Motion>",       self._on_scrollbar_drag)
        self._marker_strip.bind("<ButtonRelease-1>", self._on_scrollbar_release)
        self._marker_strip.bind("<Button-4>", lambda e: self._text.yview_scroll(-3, "units"))
        self._marker_strip.bind("<Button-5>", lambda e: self._text.yview_scroll(3, "units"))
        self._marker_strip.bind("<MouseWheel>", self._on_marker_mousewheel)

        # Button bar
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(
            btn_frame, text="Save", width=80, height=28,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_MAIN,
            font=FONT_SMALL, corner_radius=4,
            command=self._on_save,
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_frame, text="Cancel", width=80, height=28,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            font=FONT_SMALL, corner_radius=4,
            command=self._on_cancel,
        ).pack(side="right")

        # Match-navigation controls (left side) — only shown when highlighting.
        self._nav_frame = ctk.CTkFrame(btn_frame, fg_color="transparent")
        self._prev_btn = ctk.CTkButton(
            self._nav_frame, text="↑ Prev", width=72, height=28,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            font=FONT_SMALL, corner_radius=4, command=self._goto_prev_match,
        )
        self._prev_btn.pack(side="left", padx=(0, 6))
        self._next_btn = ctk.CTkButton(
            self._nav_frame, text="↓ Next", width=72, height=28,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            font=FONT_SMALL, corner_radius=4, command=self._goto_next_match,
        )
        self._next_btn.pack(side="left", padx=(0, 8))
        self._match_count_lbl = ctk.CTkLabel(
            self._nav_frame, text="", font=FONT_SMALL, text_color=TEXT_DIM,
        )
        self._match_count_lbl.pack(side="left")

        self._load_file()

    def _load_file(self):
        try:
            self._original_content = self._file_path.read_text(encoding="utf-8")
        except Exception:
            self._original_content = ""
        self._textbox.delete("0.0", "end")
        self._textbox.insert("0.0", self._original_content)
        if self._highlight:
            # Prefill the Find box; its trace runs _apply_highlight for us.
            self._find_var.set(self._highlight)
            self._run_find()
        else:
            self._update_nav_controls()
        self._schedule_marker_redraw()

    def _apply_highlight(self, needle: str):
        """Highlight every case-insensitive occurrence of *needle* in bright green,
        record each match position, and enable the match-navigation controls.
        Scans the *live* editor content so it tracks user edits."""
        self._active_needle = needle or ""
        self._match_starts = []
        self._match_idx = -1
        # Always clear any previous highlight tags first.
        try:
            self._textbox.tag_remove("search_highlight", "0.0", "end")
            self._textbox.tag_remove("search_current", "0.0", "end")
        except Exception:
            pass
        if not needle:
            self._update_nav_controls()
            self._schedule_marker_redraw()
            return
        try:
            self._textbox.tag_config(
                "search_highlight",
                background=STATUS_SUCCESS_SOLID,
                foreground=TEXT_BLACK,
            )
            # Brighter emphasis tag for the currently-focused match.
            self._textbox.tag_config(
                "search_current",
                background=ACCENT,
                foreground=TEXT_MAIN,
            )
        except Exception:
            return
        content = self._textbox.get("0.0", "end-1c")
        hay = content.casefold()
        needle_cf = needle.casefold()
        nlen = len(needle_cf)
        if nlen == 0:
            self._update_nav_controls()
            return
        start = 0
        while True:
            idx = hay.find(needle_cf, start)
            if idx == -1:
                break
            start_index = f"1.0+{idx}c"
            end_index = f"1.0+{idx + nlen}c"
            try:
                self._textbox.tag_add("search_highlight", start_index, end_index)
                # Resolve to a concrete "line.col" index so it survives edits/lookup.
                self._match_starts.append(self._text.index(start_index))
            except Exception:
                break
            start = idx + nlen
        if self._match_starts:
            self._goto_match(0)
        self._update_nav_controls()
        self._schedule_marker_redraw()

    def _on_find_changed(self, *_):
        """Debounced live re-highlight as the user types in the Find box."""
        if self._find_debounce_id is not None:
            try:
                self.after_cancel(self._find_debounce_id)
            except Exception:
                pass
        self._find_debounce_id = self.after(150, self._run_find)

    def _run_find(self):
        self._find_debounce_id = None
        self._apply_highlight(self._find_var.get())

    # -- Match navigation ---------------------------------------------------

    def _update_nav_controls(self):
        """Show the prev/next bar only when there is more than one match;
        keep a count label whenever there is at least one."""
        n = len(self._match_starts)
        if n == 0:
            try:
                self._nav_frame.pack_forget()
            except Exception:
                pass
            return
        self._nav_frame.pack(side="left")
        # Prev/Next only make sense with multiple matches.
        state = "normal" if n > 1 else "disabled"
        self._prev_btn.configure(state=state)
        self._next_btn.configure(state=state)
        cur = (self._match_idx + 1) if self._match_idx >= 0 else 0
        self._match_count_lbl.configure(text=f"{cur} / {n}")

    def _goto_match(self, idx: int):
        n = len(self._match_starts)
        if n == 0:
            return
        idx %= n
        # Clear previous current-match emphasis, re-flagging it as a plain hit.
        if 0 <= self._match_idx < n:
            prev_start = self._match_starts[self._match_idx]
            try:
                self._textbox.tag_remove(
                    "search_current", prev_start, f"{prev_start}+{self._match_len()}c")
            except Exception:
                pass
        self._match_idx = idx
        start = self._match_starts[idx]
        try:
            self._textbox.tag_add(
                "search_current", start, f"{start}+{self._match_len()}c")
            self._text.see(start)
        except Exception:
            pass
        self._update_nav_controls()
        self._schedule_marker_redraw()

    def _match_len(self) -> int:
        return len((self._active_needle or "").casefold())

    def _goto_next_match(self):
        if self._match_starts:
            self._goto_match(self._match_idx + 1)

    def _goto_prev_match(self):
        if self._match_starts:
            self._goto_match(self._match_idx - 1)

    # -- Combined scrollbar + marker strip ----------------------------------

    def _scroll_set(self, first, last):
        try:
            self._ctk_yset(first, last)
        except Exception:
            pass
        self._scroll_first = float(first)
        self._scroll_last = float(last)
        self._schedule_marker_redraw()

    def _schedule_marker_redraw(self):
        if self._marker_after_id is not None:
            try:
                self.after_cancel(self._marker_after_id)
            except Exception:
                pass
        self._marker_after_id = self.after(16, self._draw_marker_strip)

    def _on_marker_resize(self, _event=None):
        self._schedule_marker_redraw()

    def _draw_marker_strip(self):
        self._marker_after_id = None
        c = self._marker_strip
        try:
            c.delete("all")
        except Exception:
            return
        h = c.winfo_height()
        w = c.winfo_width()
        if h <= 1:
            return
        # Thumb
        first = max(0.0, min(1.0, self._scroll_first))
        last = max(first, min(1.0, self._scroll_last))
        y0 = int(first * h)
        y1 = max(y0 + 8, int(last * h))
        c.create_rectangle(2, y0, w - 2, y1, fill=BG_HOVER, outline="", tags="thumb")
        # One tick per match, positioned by its fractional line position.
        if not self._match_starts:
            return
        try:
            total_lines = int(float(self._text.index("end-1c").split(".")[0]))
        except Exception:
            total_lines = 0
        if total_lines <= 0:
            return
        for i, start in enumerate(self._match_starts):
            try:
                line = int(float(start.split(".")[0]))
            except Exception:
                continue
            frac = (line - 1) / max(1, total_lines - 1) if total_lines > 1 else 0.0
            y = int(frac * (h - 3))
            color = ACCENT if i == self._match_idx else STATUS_SUCCESS_SOLID
            c.create_rectangle(0, y, w, y + 3, fill=color, outline="", tags="marker")

    def _on_scrollbar_press(self, event):
        h = self._marker_strip.winfo_height()
        if h <= 1:
            return
        y0 = self._scroll_first * h
        y1 = self._scroll_last * h
        if y0 <= event.y <= y1:
            self._thumb_drag_offset = event.y - y0
        else:
            self._thumb_drag_offset = (y1 - y0) / 2.0
            self._scroll_to_pointer(event.y)

    def _on_scrollbar_drag(self, event):
        if self._thumb_drag_offset is not None:
            self._scroll_to_pointer(event.y - self._thumb_drag_offset)

    def _on_scrollbar_release(self, _event):
        self._thumb_drag_offset = None

    def _scroll_to_pointer(self, py):
        h = self._marker_strip.winfo_height()
        if h <= 1:
            return
        frac = max(0.0, min(1.0, py / h))
        try:
            self._text.yview_moveto(frac)
        except Exception:
            pass

    def _on_marker_mousewheel(self, event):
        delta = -1 if getattr(event, "delta", 0) > 0 else 1
        try:
            self._text.yview_scroll(delta * 3, "units")
        except Exception:
            pass

    def _on_save(self):
        try:
            content = self._textbox.get("0.0", "end-1c")
            self._file_path.write_text(content, encoding="utf-8")
            self._on_done(self)
        except OSError as e:
            tk.messagebox.showerror(
                "Save failed",
                f"Could not save {self._rel_path}:\n{e}",
                parent=self,
            )

    def _on_cancel(self):
        self._on_done(self)


# ---------------------------------------------------------------------------
# SepSettingsPanel — inline overlay for separator-level settings
# ---------------------------------------------------------------------------

class SepSettingsPanel(ctk.CTkFrame):
    """Inline panel that overlays _plugin_panel_container for per-separator settings."""

    def __init__(self, parent, sep_name: str, current_path: str,
                 current_raw: bool = False, current_mode: str = "",
                 current_merge: bool = False,
                 on_save=None, on_done=None):
        super().__init__(parent, fg_color=BG_PANEL, corner_radius=0)
        self._sep_name = sep_name
        self._on_save = on_save or (lambda path, raw, mode, merge: None)
        self._on_done = on_done or (lambda p: None)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Separator Settings \u2014 {sep_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4)
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Content
        content = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        content.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            content, text="Deployment Location",
            font=FONT_SMALL, text_color=TEXT_SEP, anchor="w",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self._path_var = tk.StringVar(value=current_path)
        self._entry = ctk.CTkEntry(
            content, textvariable=self._path_var,
            font=FONT_SMALL, fg_color=BG_DEEP, text_color=TEXT_MAIN,
            border_color=BORDER, corner_radius=4,
        )
        self._entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            content, text="Browse", width=80, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._browse,
        ).grid(row=1, column=1, padx=(0, 4))
        ctk.CTkButton(
            content, text="Clear", width=60, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=lambda: self._path_var.set(""),
        ).grid(row=1, column=2)
        content.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            content,
            text="Deploy this separator's mods here instead of the game directory.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Divider
        ctk.CTkFrame(content, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(16, 0))

        # Ignore deployment rules toggle
        self._raw_var = tk.BooleanVar(value=current_raw)
        ctk.CTkCheckBox(
            content, text="Ignore deployment rules",
            variable=self._raw_var,
            font=FONT_SMALL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            border_color=BORDER, checkmark_color=TEXT_MAIN,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ctk.CTkLabel(
            content,
            text="Skip routing rules; deploy files as-is.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # Divider
        ctk.CTkFrame(content, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=6, column=0, columnspan=3, sticky="ew", pady=(16, 0))

        ctk.CTkLabel(
            content, text="File Transfer Method",
            font=FONT_SMALL, text_color=TEXT_SEP, anchor="w",
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(12, 4))

        _mode_norm = (current_mode or "").strip().lower()
        if _mode_norm not in ("hardlink", "symlink"):
            _mode_norm = "default"
        self._mode_var = tk.StringVar(value=_mode_norm)
        for _r, (_val, _lbl) in enumerate([
            ("default",  "Default (use global setting)"),
            ("hardlink", "Hardlink"),
            ("symlink",  "Symlink"),
        ]):
            ctk.CTkRadioButton(
                content, text=_lbl, variable=self._mode_var, value=_val,
                font=FONT_SMALL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
                border_color=BORDER,
            ).grid(row=8 + _r, column=0, columnspan=3, sticky="w", pady=(2, 0))

        ctk.CTkLabel(
            content,
            text="Override the global deploy mode. Hardlink falls back to symlink if unsupported.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).grid(row=11, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # Divider
        ctk.CTkFrame(content, fg_color=BORDER, height=1, corner_radius=0).grid(
            row=12, column=0, columnspan=3, sticky="ew", pady=(16, 0))

        # Merge folders toggle
        self._merge_var = tk.BooleanVar(value=bool(current_merge))
        ctk.CTkCheckBox(
            content, text="Merge folders with target",
            variable=self._merge_var,
            font=FONT_SMALL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            border_color=BORDER, checkmark_color=TEXT_MAIN,
        ).grid(row=13, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ctk.CTkLabel(
            content,
            text="Merge mod folders into existing ones instead of replacing them.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w", justify="left",
        ).grid(row=14, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # Save / Cancel buttons
        btn_row = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        btn_row.pack(side="bottom", fill="x", padx=16, pady=12)

        ctk.CTkButton(
            btn_row, text="Save", width=90, font=FONT_SMALL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_MAIN,
            command=self._on_save_click,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            btn_row, text="Cancel", width=90, font=FONT_SMALL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right")

        self._entry.focus_set()

    def _browse(self):
        from Utils.portal_filechooser import pick_folder
        def _cb(chosen):
            if chosen is not None:
                self._path_var.set(str(chosen))
        pick_folder("Select deployment directory", _cb)

    def _on_save_click(self):
        _mode = self._mode_var.get()
        if _mode == "default":
            _mode = ""
        self._on_save(
            self._path_var.get().strip(),
            self._raw_var.get(),
            _mode,
            self._merge_var.get(),
        )
        self._on_done(self)

    def _on_cancel(self):
        self._on_done(self)


# ---------------------------------------------------------------------------
# MissingReqsPanel — inline overlay for missing Nexus requirements
# ---------------------------------------------------------------------------

class MissingReqsPanel(ctk.CTkFrame):
    """
    Inline panel version of the missing-requirements window.
    Overlays _plugin_panel_container (same as other overlay panels).
    """

    def __init__(self, parent, mod_name: str, domain: str, mod_id: int,
                 missing_ids: set, api,
                 install_from_browse,
                 ignored_set: set, save_ignored_fn,
                 on_done=None, mods=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._mod_name = mod_name
        self._domain = domain
        self._mod_id = mod_id
        self._missing_ids = missing_ids
        # Multi-mod mode: aggregate requirements across several mods.  Each
        # entry is {"mod_name", "mod_id", "domain", "missing_ids"}.  When None,
        # behave as the single-mod panel driven by the scalar fields above.
        self._mods = list(mods) if mods else None
        self._api = api
        self._install_from_browse = install_from_browse
        self._ignored_set = ignored_set
        self._save_ignored_fn = save_ignored_fn
        self._on_done = on_done or (lambda p: None)

        # Title bar
        title_bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=36)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Missing requirements \u2014 {mod_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=scaled(12))
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color=BG_PANEL, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._close,
        ).pack(side="right", padx=scaled(4))
        ctk.CTkFrame(self, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x")

        # Status label
        self._status_var = tk.StringVar(value="Loading\u2026")
        self._status_lbl = ctk.CTkLabel(
            self, textvariable=self._status_var,
            font=FONT_SMALL, text_color=TEXT_DIM,
        )
        self._status_lbl.pack(pady=20)

        # Scrollable list area
        list_frame = tk.Frame(self, bg=BG_DEEP)
        list_frame.pack(fill="both", expand=True, padx=4, pady=4)
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(
            list_frame, bg=BG_DEEP, bd=0, highlightthickness=0,
            takefocus=0,
        )
        vsb = tk.Scrollbar(list_frame, orient="vertical", command=self._canvas.yview,
                           bg=_theme.BG_SEP, troughcolor=BG_DEEP, activebackground=ACCENT,
                           highlightthickness=0, bd=0)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self._list_frame = list_frame

        def _on_wheel(e):
            self._canvas.yview_scroll(-3 if (getattr(e, "delta", 0) or 0) > 0 else 3, "units")
            return "break"
        self._canvas.bind("<MouseWheel>", _on_wheel)
        if not LEGACY_WHEEL_REDUNDANT:
            self._canvas.bind("<Button-4>", lambda e: (self._canvas.yview_scroll(-3, "units"), "break")[-1])
            self._canvas.bind("<Button-5>", lambda e: (self._canvas.yview_scroll( 3, "units"), "break")[-1])

        # Footer — let it size to its contents so the checkbox/button row is
        # never clipped at higher UI scales (a fixed height does not grow with
        # the scaled font + padding inside).
        footer = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0)
        footer.pack(fill="x", side="bottom")
        ctk.CTkFrame(footer, fg_color=BORDER, height=1, corner_radius=0).pack(side="top", fill="x")
        if self._mods is not None:
            ignore_init = all(m["mod_name"] in ignored_set for m in self._mods)
        else:
            ignore_init = mod_name in ignored_set
        self._ignore_var = tk.BooleanVar(value=ignore_init)
        ctk.CTkCheckBox(
            footer, text="Ignore requirements",
            variable=self._ignore_var,
            font=FONT_SMALL, text_color=TEXT_MAIN,
            checkbox_width=18, checkbox_height=18,
            command=self._on_ignore_toggle,
        ).pack(side="left", padx=scaled(12), pady=scaled(10))
        ctk.CTkButton(
            footer, text="Close", width=80, height=28,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            command=self._close,
        ).pack(side="right", padx=scaled(12), pady=scaled(8))

        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        err = None
        missing_list = []
        try:
            if self._mods is not None:
                # Aggregate every mod's missing requirements, deduping by the
                # requirement's own mod-id so a shared dependency shows once.
                seen: set = set()
                errors: list[str] = []
                for m in self._mods:
                    try:
                        all_reqs = self._api.get_mod_requirements(
                            m["domain"], m["mod_id"])
                    except Exception as e:
                        errors.append(f"{m['mod_name']}: {e}")
                        continue
                    for r in all_reqs:
                        if r.mod_id in m["missing_ids"] and r.mod_id not in seen:
                            seen.add(r.mod_id)
                            missing_list.append(r)
                if not missing_list and errors:
                    err = "Could not load requirements: " + "; ".join(errors)
            else:
                all_reqs = self._api.get_mod_requirements(self._domain, self._mod_id)
                for r in all_reqs:
                    if r.mod_id in self._missing_ids:
                        missing_list.append(r)
        except Exception as e:
            err = f"Could not load requirements: {e}"
        self.after(0, lambda: self._fetch_done(missing_list, err))

    def _fetch_done(self, missing_list, err):
        if not self.winfo_exists():
            return
        if err:
            self._status_var.set(err)
            return
        if not missing_list:
            self._status_var.set("No missing requirements (list is empty).")
            return
        required = []
        optional = []
        other = []
        required_kw = ("required", "hard requirement")
        optional_kw = ("optional", "not needed", "not required")
        for r in missing_list:
            note_l = (r.notes or "").lower()

            def _earliest(text, keywords):
                best = -1
                for k in keywords:
                    i = 0
                    while True:
                        j = text.find(k, i)
                        if j == -1:
                            break
                        if k == "required" and j >= 4 and text[j - 4:j] == "not ":
                            i = j + 1
                            continue
                        if best == -1 or j < best:
                            best = j
                        break
                return best

            req_pos = _earliest(note_l, required_kw)
            opt_pos = _earliest(note_l, optional_kw)
            if req_pos == -1 and opt_pos == -1:
                other.append(r)
            elif opt_pos == -1 or (req_pos != -1 and req_pos < opt_pos):
                required.append(r)
            else:
                optional.append(r)
        self._populate(
            required + other + optional,
            section_counts=[
                ("Required", len(required)),
                ("Other", len(other)),
                ("Optional", len(optional)),
            ],
        )

    def _populate(self, missing_list, section_counts=None):
        self._status_lbl.pack_forget()
        ROW_H = scaled(56)
        HDR_H = scaled(24)
        BTN_W = scaled(70)
        VIEW_W = scaled(56)
        BTN_H = scaled(28)
        NAME_PAD = scaled(10)
        canvas = self._canvas
        canvas_w = [600]
        if section_counts is None:
            section_counts = [("", len(missing_list))]
        nonempty_sections = [(n, c) for n, c in section_counts if c > 0]
        show_headers = len(nonempty_sections) >= 1
        header_at = {}
        if show_headers:
            idx = 0
            for name, count in section_counts:
                if count > 0:
                    header_at[idx] = name
                    idx += count

        def _on_resize(ev):
            canvas_w[0] = max(ev.width, 200)
            _repaint()

        self._list_frame.bind("<Configure>", _on_resize)
        row_bounds = []
        view_btns = []
        install_btns = []

        def _repaint():
            canvas.delete("all")
            row_bounds.clear()
            cw = canvas_w[0]
            btn_left = cw - 2 * BTN_W - scaled(16)
            name_max_px = max(btn_left - NAME_PAD - scaled(8), 20)
            y = 0

            def _draw_header(label, y_pos):
                canvas.create_rectangle(0, y_pos, cw, y_pos + HDR_H, fill=BG_PANEL, outline="")
                canvas.create_text(
                    NAME_PAD, y_pos + HDR_H // 2,
                    text=label, anchor="w",
                    font=TK_FONT_BOLD, fill=TEXT_MAIN,
                )
                return y_pos + HDR_H

            for i, req in enumerate(missing_list):
                if i in header_at:
                    y = _draw_header(header_at[i], y)
                y_top = y
                notes = (req.notes or "").strip() or "No notes"
                title = req.mod_name + (" (External)" if req.is_external else "")
                desc_h = min(16 * 2, 32)
                row_h = max(ROW_H, scaled(24 + desc_h + 12))
                y_bot = y_top + row_h
                row_bounds.append((y_top, y_bot))
                bg = BG_ROW_ALT if i % 2 else BG_ROW
                canvas.create_rectangle(0, y_top, cw, y_bot, fill=bg, outline="")
                canvas.create_text(
                    NAME_PAD, y_top + scaled(12),
                    text=title[:80] + ("\u2026" if len(title) > 80 else ""),
                    anchor="w", font=(_theme.FONT_FAMILY, _theme.FS11), fill=TEXT_MAIN,
                )
                canvas.create_text(
                    NAME_PAD, y_top + scaled(30),
                    text=notes[:120] + ("\u2026" if len(notes) > 120 else ""),
                    anchor="nw", width=name_max_px,
                    font=(_theme.FONT_FAMILY, _theme.FS10), fill=TEXT_DIM,
                )
                y = y_bot
            total_h = max(y, 1)
            canvas.configure(scrollregion=(0, 0, cw, total_h))

            while len(view_btns) < len(missing_list):
                idx = len(view_btns)
                req = missing_list[idx]
                url = req.url or f"https://www.nexusmods.com/{self._domain or req.game_domain}/mods/{req.mod_id}"
                vb = ctk.CTkButton(
                    self, text="View", width=VIEW_W, height=BTN_H,
                    fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_WHITE,
                    font=(_theme.FONT_FAMILY, _theme.CTK_FS10), cursor="hand2",
                    command=lambda u=url: open_url(u),
                )
                ib = ctk.CTkButton(
                    self, text="Install", width=BTN_W, height=BTN_H,
                    fg_color=BTN_SUCCESS, hover_color=BTN_SUCCESS_HOV, text_color=TEXT_WHITE,
                    font=(_theme.FONT_FAMILY, _theme.CTK_FS10), cursor="hand2",
                    command=lambda r=req: self._on_install(r),
                )
                view_btns.append(vb)
                install_btns.append(ib)
            for idx in range(len(missing_list)):
                y_top, y_bot = row_bounds[idx]
                cy = y_top + (y_bot - y_top) // 2
                vx = cw - BTN_W - scaled(4) - BTN_W - scaled(4)
                ix = cw - BTN_W - scaled(4)
                canvas.create_window(vx, cy, window=view_btns[idx], width=VIEW_W, height=BTN_H, tags="btns")
                canvas.create_window(ix, cy, window=install_btns[idx], width=BTN_W, height=BTN_H, tags="btns")

        _repaint()

    def _on_install(self, req):
        if self._install_from_browse is not None:
            entry = SimpleNamespace(
                mod_id=req.mod_id,
                domain_name=self._domain or req.game_domain or "",
                name=req.mod_name or f"Mod {req.mod_id}",
            )
            self._install_from_browse(entry)
        else:
            url = req.url or f"https://www.nexusmods.com/{self._domain or req.game_domain or ''}/mods/{req.mod_id}"
            open_url(url)

    def _on_ignore_toggle(self):
        names = ([m["mod_name"] for m in self._mods]
                 if self._mods is not None else [self._mod_name])
        if self._ignore_var.get():
            for n in names:
                self._ignored_set.add(n)
        else:
            for n in names:
                self._ignored_set.discard(n)
        self._save_ignored_fn()

    def _close(self):
        self._on_ignore_toggle()
        self._on_done(self)


class CollectionInstallModeDialog(tk.Frame):
    """Overlay panel that asks how to install a collection.

    Placed over the mod list panel with place(relx=0, rely=0, relwidth=1, relheight=1).
    Calls on_done(result) when finished, where result is one of:
      ("new", None, False, False)                              — create a new profile
      ("append", profile_name, overwrite_existing, skip_existing)  — append into existing profile
      None                                                     — cancelled
    """

    def __init__(self, parent, existing_profiles: list[str], on_done, force_new_profile: bool = False):
        super().__init__(parent, bg=BG_DEEP)
        self._on_done = on_done
        self._existing_profiles = existing_profiles
        self._force_new_profile = force_new_profile

        self._mode_var = tk.StringVar(value="new")
        self._overwrite_var = tk.BooleanVar(value=False)
        self._skip_existing_var = tk.BooleanVar(value=False)
        self._profile_var = tk.StringVar(
            value=existing_profiles[0] if existing_profiles else ""
        )

        self._build()

    def _build(self):
        # Full-size container so we can centre the card
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Centred card
        card = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=1,
                        highlightbackground=BORDER)
        card.grid(row=0, column=0)
        card.grid_columnconfigure(0, weight=1)

        row = 0

        # Header bar
        header = tk.Frame(card, bg=BG_HEADER, height=scaled(42))
        header.grid(row=row, column=0, sticky="ew")
        header.grid_propagate(False)
        tk.Label(
            header, text="Install Collection",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)
        row += 1

        # Separator
        tk.Frame(card, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")
        row += 1

        # Body
        body = tk.Frame(card, bg=BG_PANEL)
        body.grid(row=row, column=0, sticky="ew", padx=24, pady=(16, 8))
        body.grid_columnconfigure(0, weight=1)
        row += 1

        tk.Label(
            body, text="How would you like to install this collection?",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_PANEL, anchor="center", justify="center",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 14))

        ctk.CTkRadioButton(
            body, text="Create a new profile",
            variable=self._mode_var, value="new",
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
            command=self._on_mode_change,
        ).grid(row=1, column=0, sticky="w", pady=(0, 6))

        if self._force_new_profile:
            tk.Label(
                body,
                text="This collection requires a new profile and cannot be\nappended to an existing one.",
                font=TK_FONT_SMALL,
                fg=TEXT_DIM, bg=BG_PANEL, anchor="w", justify="left",
            ).grid(row=2, column=0, sticky="w", pady=(0, 8))
        else:
            ctk.CTkRadioButton(
                body, text="Append to existing profile",
                variable=self._mode_var, value="append",
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                command=self._on_mode_change,
            ).grid(row=2, column=0, sticky="w", pady=(0, 8))

        if not self._force_new_profile:
            self._profile_menu = ctk.CTkOptionMenu(
                body, values=self._existing_profiles or ["(no profiles)"],
                variable=self._profile_var,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=BG_DEEP, button_color=BG_HEADER, button_hover_color=BG_HOVER,
                dropdown_fg_color=BG_PANEL, dropdown_text_color=TEXT_MAIN,
                dropdown_hover_color=BG_HOVER,
                state="disabled", width=280,
            )
            self._profile_menu.grid(row=3, column=0, sticky="w", padx=(16, 0), pady=(0, 6))

            self._overwrite_cb = ctk.CTkCheckBox(
                body, text="Overwrite existing mods",
                variable=self._overwrite_var,
                font=FONT_NORMAL, text_color=TEXT_DIM,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                checkmark_color="white",
                state="disabled",
            )
            self._overwrite_cb.grid(row=4, column=0, sticky="w", padx=(16, 0), pady=(0, 4))

            self._skip_existing_cb = ctk.CTkCheckBox(
                body, text="Skip already installed mods",
                variable=self._skip_existing_var,
                font=FONT_NORMAL, text_color=TEXT_DIM,
                fg_color=ACCENT, hover_color=ACCENT_HOV, border_color=BORDER,
                checkmark_color="white",
                state="disabled",
            )
            self._skip_existing_cb.grid(row=5, column=0, sticky="w", padx=(16, 0), pady=(0, 4))

        # Separator before buttons
        tk.Frame(card, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")
        row += 1

        # Button bar
        bar = tk.Frame(card, bg=BG_HEADER, height=scaled(44))
        bar.grid(row=row, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Install", width=80, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=8)

    def _on_mode_change(self):
        is_append = self._mode_var.get() == "append"
        state = "normal" if is_append else "disabled"
        if hasattr(self, "_profile_menu"):
            self._profile_menu.configure(state=state)
        if hasattr(self, "_overwrite_cb"):
            self._overwrite_cb.configure(
                state=state,
                text_color=TEXT_MAIN if is_append else TEXT_DIM,
            )
        if hasattr(self, "_skip_existing_cb"):
            self._skip_existing_cb.configure(
                state=state,
                text_color=TEXT_MAIN if is_append else TEXT_DIM,
            )

    def _on_ok(self):
        mode = self._mode_var.get()
        if mode == "new":
            result = ("new", None, False, False)
        else:
            profile = self._profile_var.get()
            if not profile or profile == "(no profiles)":
                return
            result = ("append", profile, self._overwrite_var.get(), self._skip_existing_var.get())
        self._on_done(result)

    def _on_cancel(self):
        self._on_done(None)


class CollectionContinueInstallDialog(tk.Frame):
    """Overlay panel shown when a collection is already installed in a profile.

    Instead of offering new/append, shows a single 'Continue Install' action
    targeting the profile that already contains this collection's URL.
    """

    def __init__(self, parent, profile_name: str, on_done):
        super().__init__(parent, bg=BG_DEEP)
        self._on_done = on_done
        self._profile_name = profile_name
        self._build()

    def _build(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        card = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=1,
                        highlightbackground=BORDER)
        card.grid(row=0, column=0)
        card.grid_columnconfigure(0, weight=1)

        row = 0

        # Header bar
        header = tk.Frame(card, bg=BG_HEADER, height=scaled(42))
        header.grid(row=row, column=0, sticky="ew")
        header.grid_propagate(False)
        tk.Label(
            header, text="Continue Collection Install",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)
        row += 1

        # Separator
        tk.Frame(card, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")
        row += 1

        # Body
        body = tk.Frame(card, bg=BG_PANEL)
        body.grid(row=row, column=0, sticky="ew", padx=24, pady=(16, 8))
        body.grid_columnconfigure(0, weight=1)
        row += 1

        tk.Label(
            body, text=f"This collection is already installed in profile\n'{self._profile_name}'",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_PANEL, anchor="center", justify="center",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 14))

        # Separator before buttons
        tk.Frame(card, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")
        row += 1

        # Button bar
        bar = tk.Frame(card, bg=BG_HEADER, height=scaled(44))
        bar.grid(row=row, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Continue Install", width=120, height=28, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=8)

    def _on_ok(self):
        self._on_done(("continue", self._profile_name, False, False))

    def _on_cancel(self):
        self._on_done(None)


class CollectionUpdateDialog(tk.Frame):
    """Overlay shown when the user clicks **Update Collection**.

    Presents the reconciliation between the installed revision and the newly
    selected one as four expandable sections: mods to remove, mods to update,
    mods to add, and orphans (collection-origin mods without mod_id/file_id
    that the new manifest cannot verify). The user confirms with
    ``Apply Update`` or backs out with ``Cancel``.
    """

    def __init__(
        self,
        parent,
        *,
        profile_name: str,
        from_revision: "int | None",
        to_revision: "int | None",
        to_remove: list[str],
        to_update: list[str],         # "old mod name -> new file_id label"
        to_add: list[str],
        orphans: list[str],
        on_done,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._on_done = on_done
        self._profile_name = profile_name
        self._from_rev = from_revision
        self._to_rev = to_revision
        self._to_remove = list(to_remove)
        self._to_update = list(to_update)
        self._to_add = list(to_add)
        self._orphans = list(orphans)
        self._expanded: dict[str, bool] = {
            "remove": False, "update": False, "add": False, "orphan": False,
        }
        self._section_bodies: dict[str, tk.Frame] = {}
        self._build()

    # --------------------------------------------------------------
    def _build(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        card = tk.Frame(self, bg=BG_PANEL, bd=0,
                        highlightthickness=1, highlightbackground=BORDER)
        card.grid(row=0, column=0, padx=20, pady=20)
        card.grid_columnconfigure(0, weight=1)
        # Body row (scrollable region) is the one that should grow.
        # Rows 0-2 are fixed-height (header, summary); rows after that are the
        # body, separator, and button bar.
        card.grid_rowconfigure(3, weight=1)
        # Size is recomputed from the toplevel window once it's actually rendered,
        # since this overlay is place()'d to fill its parent and `self` has no
        # dimensions until after that completes.
        self._card = card
        card.configure(width=900, height=640)
        card.grid_propagate(False)
        self.after(10, self._resize_card_to_toplevel)

        row = 0

        header = tk.Frame(card, bg=BG_HEADER, height=scaled(42))
        header.grid(row=row, column=0, sticky="ew")
        header.grid_propagate(False)
        tk.Label(
            header, text="Update Collection",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)
        row += 1

        tk.Frame(card, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")
        row += 1

        summary = tk.Frame(card, bg=BG_PANEL)
        summary.grid(row=row, column=0, sticky="ew", padx=24, pady=(14, 6))
        summary.grid_columnconfigure(0, weight=1)
        row += 1

        from_label = f"Rev {self._from_rev}" if self._from_rev is not None else "Unknown"
        to_label = f"Rev {self._to_rev}" if self._to_rev is not None else "(latest)"
        tk.Label(
            summary,
            text=f"Profile '{self._profile_name}': {from_label} → {to_label}",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_PANEL,
            anchor="center", justify="center",
        ).grid(row=0, column=0, sticky="ew")

        counts = tk.Label(
            summary,
            text=(
                f"Remove {len(self._to_remove)}  ·  "
                f"Update {len(self._to_update)}  ·  "
                f"Add {len(self._to_add)}  ·  "
                f"Orphans {len(self._orphans)}"
            ),
            font=TK_FONT_NORMAL, fg=TEXT_DIM, bg=BG_PANEL,
            anchor="center", justify="center",
        )
        counts.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        warning_lbl = tk.Label(
            summary,
            text=(
                "Your existing load order and separators will be preserved. "
                "New mods will be inserted relative to their neighbors from "
                "the new collection manifest; mods with no defined position "
                "will be placed at the top of the list."
            ),
            font=TK_FONT_NORMAL, fg=TEXT_DIM, bg=BG_PANEL,
            anchor="center", justify="center",
        )
        warning_lbl.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        # Re-wrap the warning label whenever the dialog card resizes so the
        # text fits the current width.
        def _rewrap(_event=None):
            try:
                w = card.winfo_width() - 80
                if w > 120:
                    warning_lbl.configure(wraplength=w)
            except Exception:
                pass
        card.bind("<Configure>", _rewrap)

        # Scrollable body: four expandable sections, scrolled via canvas + scrollbar.
        scroll_wrap = tk.Frame(card, bg=BG_PANEL)
        scroll_wrap.grid(row=row, column=0, sticky="nsew", padx=(24, 8), pady=(10, 10))
        scroll_wrap.grid_rowconfigure(0, weight=1)
        scroll_wrap.grid_columnconfigure(0, weight=1)
        row += 1

        canvas = tk.Canvas(
            scroll_wrap, bg=BG_PANEL, bd=0, highlightthickness=0,
            takefocus=0,
        )
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb = ctk.CTkScrollbar(
            scroll_wrap, orientation="vertical", command=canvas.yview,
            width=14,
        )
        vsb.grid(row=0, column=1, sticky="ns", padx=(2, 0))
        canvas.configure(yscrollcommand=vsb.set)

        body = tk.Frame(canvas, bg=BG_PANEL)
        body.grid_columnconfigure(0, weight=1)
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")

        def _on_body_configure(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        body.bind("<Configure>", _on_body_configure)

        def _on_canvas_configure(event):
            # Keep the inner frame's width in sync with the canvas so labels
            # lay out to the full width instead of shrinking to content.
            canvas.itemconfigure(body_window, width=event.width)
        canvas.bind("<Configure>", _on_canvas_configure)

        # Mousewheel scrolling — bind to canvas and every descendant we add.
        # Linux uses Button-4/5; Windows/Mac use MouseWheel.
        def _on_mousewheel(event):
            y0, y1 = canvas.yview()
            if y0 <= 0.0 and y1 >= 1.0:
                return "break"
            if getattr(event, "num", None) == 4:
                canvas.yview_scroll(-1, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(1, "units")
            else:
                delta = int(-1 * (event.delta / 120)) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
                canvas.yview_scroll(delta, "units")
            return "break"
        self._wheel_handler = _on_mousewheel
        self._wheel_canvas = canvas
        # Bind wheel at the card level so scrolling works from anywhere in
        # the dialog (header, summary, body, buttons).
        for w in (card, scroll_wrap, canvas, body):
            w.bind("<MouseWheel>", _on_mousewheel)
            w.bind("<Button-4>", _on_mousewheel)
            w.bind("<Button-5>", _on_mousewheel)

        self._build_section(body, 0, "remove", "Remove", self._to_remove,
                            BTN_DANGER_ALT, BTN_DANGER_ALT_HOV)
        self._build_section(body, 1, "update", "Update", self._to_update,
                            BTN_INFO_DEEP, BTN_INFO_DEEP_HOV)
        self._build_section(body, 2, "add", "Add", self._to_add,
                            BTN_SUCCESS, BTN_SUCCESS_HOV)
        self._build_section(body, 3, "orphan", "Orphans", self._orphans,
                            BTN_WARN_ORANGE, BTN_WARN_ORANGE_HOV)

        if not (self._to_remove or self._to_update or self._to_add or self._orphans):
            tk.Label(
                body,
                text="No changes detected between these revisions.",
                font=TK_FONT_NORMAL, fg=TEXT_DIM, bg=BG_PANEL,
            ).grid(row=8, column=0, sticky="ew", pady=12)

        tk.Frame(card, bg=BORDER, height=1).grid(row=row, column=0, sticky="ew")
        row += 1

        bar = tk.Frame(card, bg=BG_HEADER, height=scaled(44))
        bar.grid(row=row, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_DEEP, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        apply_btn_enabled = bool(
            self._to_remove or self._to_update or self._to_add or self._orphans
        )
        apply_btn = ctk.CTkButton(
            bar, text="Apply Update", width=120, height=28, font=FONT_BOLD,
            fg_color=BTN_INFO_DEEP, hover_color=BTN_INFO_DEEP_HOV,
            text_color=TEXT_WHITE,
            command=self._on_ok,
        )
        if not apply_btn_enabled:
            apply_btn.configure(state="disabled")
        apply_btn.pack(side="right", padx=4, pady=8)

    # --------------------------------------------------------------
    def _bind_wheel(self, widget) -> None:
        """Forward mousewheel events from *widget* to the scrollable canvas."""
        handler = getattr(self, "_wheel_handler", None)
        if handler is None:
            return
        widget.bind("<MouseWheel>", handler)
        widget.bind("<Button-4>", handler)
        widget.bind("<Button-5>", handler)

    def _build_section(self, parent, grid_row: int, key: str,
                       label: str, items: list[str],
                       accent: str, accent_hov: str) -> None:
        count = len(items)
        header_row = tk.Frame(parent, bg=BG_PANEL)
        header_row.grid(row=grid_row * 2, column=0, sticky="ew", pady=(2, 0))
        header_row.grid_columnconfigure(1, weight=1)
        self._bind_wheel(header_row)

        toggle_text = f"▶ {label} ({count})" if count else f"  {label} (0)"
        toggle_btn = tk.Label(
            header_row,
            text=toggle_text,
            font=TK_FONT_BOLD, fg=accent, bg=BG_PANEL,
            anchor="w", cursor="hand2" if count else "",
        )
        toggle_btn.grid(row=0, column=0, sticky="w")
        self._bind_wheel(toggle_btn)
        if count:
            toggle_btn.bind("<Button-1>", lambda _e, k=key, b=toggle_btn, lbl=label, n=count: self._toggle(k, b, lbl, n))

        body = tk.Frame(parent, bg=BG_ROW, bd=0)
        body.grid(row=grid_row * 2 + 1, column=0, sticky="ew", padx=(18, 0), pady=(0, 2))
        body.grid_columnconfigure(0, weight=1)
        body.grid_remove()  # hidden by default
        self._section_bodies[key] = body
        self._bind_wheel(body)

        # Pre-populate the body with mod names.
        for i, name in enumerate(items):
            lbl = tk.Label(
                body, text=name, font=TK_FONT_NORMAL, fg=TEXT_MAIN, bg=BG_ROW,
                anchor="w", justify="left",
            )
            lbl.grid(row=i, column=0, sticky="ew", padx=8, pady=1)
            self._bind_wheel(lbl)

    def _toggle(self, key: str, label_widget: tk.Label, label: str, count: int) -> None:
        expanded = not self._expanded.get(key, False)
        self._expanded[key] = expanded
        body = self._section_bodies.get(key)
        if body is None:
            return
        if expanded:
            body.grid()
            label_widget.configure(text=f"▼ {label} ({count})")
        else:
            body.grid_remove()
            label_widget.configure(text=f"▶ {label} ({count})")
        # Recompute the scroll region once the layout settles.
        canvas = getattr(self, "_wheel_canvas", None)
        if canvas is not None:
            self.after_idle(
                lambda: canvas.configure(scrollregion=canvas.bbox("all"))
            )

    def _resize_card_to_toplevel(self) -> None:
        """Size the dialog card relative to the toplevel window, now that the
        overlay has been placed and has real dimensions."""
        card = getattr(self, "_card", None)
        if card is None or not card.winfo_exists():
            return
        try:
            top = self.winfo_toplevel()
            top.update_idletasks()
            w = top.winfo_width()
            h = top.winfo_height()
        except Exception:
            return
        # Generous defaults, capped to the window with a 60px margin.
        card_w = max(560, min(960, w - 60))
        card_h = max(360, min(720, h - 60))
        try:
            card.configure(width=card_w, height=card_h)
        except Exception:
            pass

    def _on_ok(self):
        self._on_done(True)

    def _on_cancel(self):
        self._on_done(False)


class _UserlistEntryDialog(ctk.CTkToplevel):
    """Dialog to configure a plugin's userlist.yaml entry (before/after/group)."""

    def __init__(self, parent, plugin_name: str, existing: dict):
        """
        existing: dict with optional keys 'before', 'after', 'group'
                  where before/after are lists of str and group is str.
        """
        super().__init__(parent, fg_color=BG_DEEP)
        self.title("Add to Userlist")
        self.geometry("460x300")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(100, self._make_modal)

        self._plugin_name = plugin_name
        self._existing = existing
        self.result: dict | None = None
        self._build()

    def _make_modal(self):
        try:
            self.grab_set()
            self.focus_set()
        except Exception:
            pass

    def _build(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text=f"Userlist entry: {self._plugin_name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 2))

        ctk.CTkLabel(
            self, text="Separate multiple plugin names with commas.",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

        def _row(parent, row, label, var):
            ctk.CTkLabel(
                parent, text=label, font=FONT_NORMAL,
                text_color=TEXT_DIM, anchor="w", width=60,
            ).grid(row=row, column=0, sticky="w", padx=(16, 4), pady=3)
            ctk.CTkEntry(
                parent, textvariable=var, font=FONT_NORMAL,
                fg_color=BG_PANEL, text_color=TEXT_MAIN, border_color=BORDER,
            ).grid(row=row, column=1, sticky="ew", padx=(0, 16), pady=3)

        fields = ctk.CTkFrame(self, fg_color="transparent")
        fields.grid(row=2, column=0, sticky="ew")
        fields.grid_columnconfigure(1, weight=1)

        before_list = self._existing.get("before", [])
        after_list  = self._existing.get("after",  [])
        group_val   = self._existing.get("group",  "")

        self._before_var = tk.StringVar(value=", ".join(before_list))
        self._after_var  = tk.StringVar(value=", ".join(after_list))
        self._group_var  = tk.StringVar(value=group_val)

        _row(fields, 0, "Before:", self._before_var)
        _row(fields, 1, "After:",  self._after_var)
        _row(fields, 2, "Group:",  self._group_var)

        # Button bar
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=44)
        bar.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(side="top", fill="x")
        ctk.CTkButton(
            bar, text="Cancel", width=80, height=28, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=(4, 12), pady=8)
        ctk.CTkButton(
            bar, text="Save", width=80, height=28, font=FONT_NORMAL,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_ok,
        ).pack(side="right", padx=4, pady=8)

    def _parse_list(self, var: tk.StringVar) -> list[str]:
        raw = var.get().strip()
        if not raw:
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def _on_ok(self):
        entry: dict = {}
        before = self._parse_list(self._before_var)
        after  = self._parse_list(self._after_var)
        group  = self._group_var.get().strip() or "default"
        if before:
            entry["before"] = before
        if after:
            entry["after"] = after
        entry["group"] = group
        self.result = entry
        self.destroy()

    def _on_cancel(self):
        self.destroy()

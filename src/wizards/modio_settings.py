"""
mod.io API key wizard (Baldur's Gate 3).

Lets the user paste their free read-only mod.io API key, test it, and save
it.  The key enables update-checking for BG3 mods installed manually from
mod.io (matched at install time via the pak's PublishHandle).

The mod.io logic lives in the BG3 game folder (Games/Baldur's Gate 3/
modio_*.py); that folder isn't importable by dotted path, so its modules are
loaded here by file path.  This wizard is just the UI shell.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL,
    TEXT_ON_ACCENT, TEXT_DIM, TEXT_MAIN,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)

_KEY_URL = "https://mod.io/me/access"


def _load_bg3_modio(stem: str):
    """Load a Games/Baldur's Gate 3/<stem>.py module by file path."""
    mod_name = f"{stem}_bg3"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    bg3_dir = (Path(__file__).resolve().parent.parent
               / "Games" / "Baldur's Gate 3")
    spec = importlib.util.spec_from_file_location(mod_name, str(bg3_dir / f"{stem}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class ModioSettingsWizard(ctk.CTkFrame):
    """Single-page wizard to enter and store the mod.io API key."""

    def __init__(self, parent, game: "BaseGame", log_fn=None, *,
                 on_close=None, **_kwargs):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game = game
        self._log = log_fn or (lambda msg: None)
        self._busy = False

        self._modio_key = _load_bg3_modio("modio_key")

        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text="mod.io API Key",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="✕", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4, pady=4)

        body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        body.pack(fill="both", expand=True, padx=20, pady=20)

        ctk.CTkLabel(
            body,
            text=(
                "Paste your mod.io read-only API key to enable update checks\n"
                "for Baldur's Gate 3 mods installed manually from mod.io.\n\n"
                "The key is read-only and stored securely (system keyring,\n"
                "or an encrypted file when no keyring is available)."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=480,
        ).pack(pady=(0, 12))

        ctk.CTkButton(
            body, text="Get my API key (mod.io)", width=200, height=30,
            font=FONT_SMALL, fg_color=BG_PANEL, hover_color=BG_HEADER,
            text_color=TEXT_MAIN, command=lambda: webbrowser.open(_KEY_URL),
        ).pack(pady=(0, 12))

        self._entry = ctk.CTkEntry(
            body, width=420, height=36, font=FONT_NORMAL,
            placeholder_text="mod.io API key",
        )
        self._entry.pack(pady=(0, 8))

        existing = ""
        try:
            existing = self._modio_key.load_modio_key()
        except Exception:
            pass
        if existing:
            self._entry.insert(0, existing)

        self._status = ctk.CTkLabel(
            body, text="", font=FONT_SMALL, text_color=TEXT_DIM,
            justify="center", wraplength=480,
        )
        self._status.pack(pady=(4, 12))

        btn_frame = ctk.CTkFrame(body, fg_color="transparent")
        btn_frame.pack(side="bottom", pady=(8, 0))

        self._save_btn = ctk.CTkButton(
            btn_frame, text="Test & Save", width=130, height=36, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._on_save,
        )
        self._save_btn.pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_frame, text="Clear key", width=110, height=36, font=FONT_NORMAL,
            fg_color=BG_PANEL, hover_color=BG_HEADER, text_color=TEXT_MAIN,
            command=self._on_clear,
        ).pack(side="right", padx=(8, 0))

    # ------------------------------------------------------------------

    def _set_status(self, text: str, *, ok: bool | None = None):
        color = TEXT_DIM
        if ok is True:
            color = "#5fb95f"
        elif ok is False:
            color = "#d65c5c"
        try:
            self._status.configure(text=text, text_color=color)
        except Exception:
            pass

    def _on_save(self):
        if self._busy:
            return
        key = self._entry.get().strip()
        if not key:
            self._set_status("Enter a key first.", ok=False)
            return
        self._busy = True
        self._save_btn.configure(state="disabled")
        self._set_status("Testing key…")

        def _work():
            ok = False
            err = ""
            try:
                modio_api = _load_bg3_modio("modio_api")
                ok = modio_api.ModioAPI(key).test_key()
            except Exception as e:
                err = str(e)
            self.after(0, lambda: self._save_done(key, ok, err))

        threading.Thread(target=_work, daemon=True).start()

    def _save_done(self, key: str, ok: bool, err: str):
        self._busy = False
        try:
            self._save_btn.configure(state="normal")
        except Exception:
            pass
        if not ok:
            msg = "Key rejected by mod.io." if not err else f"Key test failed: {err}"
            self._set_status(msg, ok=False)
            return
        try:
            self._modio_key.save_modio_key(key)
            self._set_status("Key saved. mod.io update checks are now enabled.", ok=True)
            self._log("mod.io: API key saved.")
        except Exception as e:
            self._set_status(f"Could not save key: {e}", ok=False)

    def _on_clear(self):
        try:
            self._modio_key.clear_modio_key()
            self._entry.delete(0, "end")
            self._set_status("Key cleared.", ok=None)
            self._log("mod.io: API key cleared.")
        except Exception as e:
            self._set_status(f"Could not clear key: {e}", ok=False)

    def _on_cancel(self):
        self._on_close_cb()

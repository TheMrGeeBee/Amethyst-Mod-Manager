"""
Changelog overlay — displays Changelog.txt over the modlist panel.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

import sys

import customtkinter as ctk

from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.theme import (
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_BOLD,
    TK_FONT_BOLD,
    TK_FONT_SMALL,
    scaled,
)

def _find_changelog() -> Path:
    """Locate Changelog.txt relative to the app entry point, working for both
    development (src/gui/changelog_overlay.py → ../../Changelog.txt) and
    packaged builds (AppImage/flatpak where __main__ lives in usr/app/)."""
    candidates = [
        getattr(sys.modules.get("__main__"), "__file__", None),
        __file__,
        sys.argv[0] if sys.argv else None,
    ]
    for origin in candidates:
        if not origin:
            continue
        base = Path(origin).resolve().parent
        for path in (
            base / "Changelog.txt",
            base.parent / "Changelog.txt",
            base.parent.parent / "Changelog.txt",
        ):
            if path.is_file():
                return path
    # Last resort: path relative to this file
    return Path(__file__).parent.parent.parent / "Changelog.txt"

_CHANGELOG_PATH = _find_changelog()


class ChangelogOverlay(tk.Frame):
    """
    Overlay that displays Changelog.txt.
    Placed over the ModListPanel area via place(relx=0, rely=0, relwidth=1, relheight=1).
    """

    def __init__(
        self,
        parent: tk.Widget,
        on_close: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._on_close = on_close
        self._build()

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(self, bg=BG_HEADER, height=scaled(42))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        tk.Label(
            toolbar, text="Changelog",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=85, height=30,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_close,
        ).pack(side="right", padx=(6, 12), pady=5)

        # Scrollable text area
        content = tk.Frame(self, bg=BG_DEEP)
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        text = tk.Text(
            content,
            font=TK_FONT_SMALL,
            bg=BG_PANEL,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            selectbackground="#3a5a8a",
            relief="flat",
            bd=0,
            wrap="word",
            state="normal",
            padx=14,
            pady=10,
        )
        vsb = tk.Scrollbar(content, orient="vertical", command=text.yview,
                           bg="#383838", troughcolor=BG_DEEP,
                           highlightthickness=0, bd=0)
        text.configure(yscrollcommand=vsb.set)
        text.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        # Bind scroll
        if not LEGACY_WHEEL_REDUNDANT:
            text.bind("<Button-4>", lambda e: text.yview_scroll(-3, "units"))
            text.bind("<Button-5>", lambda e: text.yview_scroll(3, "units"))

        # Load and display changelog
        try:
            raw = _CHANGELOG_PATH.read_text(encoding="utf-8")
        except OSError:
            raw = "(Changelog.txt not found)"

        # Configure tags for version headers
        text.tag_configure("version", font=TK_FONT_BOLD, foreground="#c8a050", spacing1=10, spacing3=2)
        text.tag_configure("bullet", foreground=TEXT_MAIN, lmargin1=8, lmargin2=16)
        text.tag_configure("dim", foreground=TEXT_DIM, lmargin1=8, lmargin2=16)

        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                text.insert("end", "\n")
                continue
            if (stripped.startswith("- v") or stripped.startswith("- V")) and len(stripped) > 3 and stripped[3].isdigit():
                # Version header line e.g. "- v1.0.5"
                version = stripped[2:]  # strip leading "- "
                text.insert("end", version + "\n", "version")
            elif stripped.startswith("- "):
                text.insert("end", "  • " + stripped[2:] + "\n", "bullet")
            else:
                text.insert("end", "  " + stripped + "\n", "dim")

        text.configure(state="disabled")

    def _do_close(self):
        if self._on_close:
            self._on_close()
        else:
            self.destroy()

"""
Mod Note overlay — edit / save / remove a free-form note attached to a mod.

Notes are stored in profile_state.json keyed by mod folder name, so they survive
uninstall + reinstall. This widget is a dumb editor; persistence is owned by
callbacks supplied by the caller (typically the modlist panel).
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_HOVER,
    BG_PANEL,
    BORDER,
    FONT_BOLD,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_ON_ACCENT,
    TK_FONT_BOLD,
    TK_FONT_NORMAL,
    TK_FONT_SMALL, scaled,
)


class ModNoteOverlay(tk.Frame):
    """Full-panel overlay for editing a single mod's note.

    Place over a parent panel via place(relx=0, rely=0, relwidth=1, relheight=1).

    Callbacks:
        on_save(text)  — called with the new text. Empty / whitespace-only text
                         is rerouted to on_remove().
        on_remove()    — called when the user clears the note.
        on_close()     — called to dismiss the overlay (caller destroys).
    """

    def __init__(
        self,
        parent: tk.Widget,
        *,
        mod_name: str,
        initial_text: str,
        on_save: Callable[[str], None],
        on_remove: Callable[[], None],
        on_close: Callable[[], None],
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._mod_name = mod_name
        self._initial_text = initial_text or ""
        self._on_save = on_save
        self._on_remove = on_remove
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
            toolbar, text=f"Note — {self._mod_name}",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=85, height=30,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_cancel,
        ).pack(side="right", padx=(6, 12), pady=5)

        # Content area — text editor with scrollbar
        content = tk.Frame(self, bg=BG_DEEP)
        content.grid(row=1, column=0, sticky="nsew", padx=12, pady=(12, 0))
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        self._text = tk.Text(
            content,
            wrap="word",
            undo=True,
            bg=BG_PANEL,
            fg=TEXT_MAIN,
            insertbackground=TEXT_MAIN,
            selectbackground=ACCENT,
            selectforeground=TEXT_ON_ACCENT,
            font=TK_FONT_NORMAL,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            padx=10,
            pady=8,
        )
        self._text.grid(row=0, column=0, sticky="nsew")

        vsb = tk.Scrollbar(
            content, orient="vertical", command=self._text.yview,
            bg="#383838", troughcolor=BG_DEEP,
            highlightthickness=0, bd=0,
        )
        self._text.configure(yscrollcommand=vsb.set)
        vsb.grid(row=0, column=1, sticky="ns")

        if self._initial_text:
            self._text.insert("1.0", self._initial_text)

        # Hint line
        tk.Label(
            self,
            text="Saved with the profile — survives uninstall and reinstall of this mod.",
            font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP, anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 0))

        # Button row
        btn_row = tk.Frame(self, bg=BG_DEEP)
        btn_row.grid(row=3, column=0, sticky="ew", padx=12, pady=12)

        ctk.CTkButton(
            btn_row, text="Save", width=90, height=32, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._do_save,
        ).pack(side="left")

        ctk.CTkButton(
            btn_row, text="Cancel", width=90, height=32, font=FONT_BOLD,
            fg_color=BG_HOVER, hover_color="#555", text_color=TEXT_MAIN,
            command=self._do_cancel,
        ).pack(side="left", padx=(8, 0))

        has_existing = bool(self._initial_text.strip())
        ctk.CTkButton(
            btn_row, text="Remove", width=90, height=32, font=FONT_BOLD,
            fg_color="#6b3333" if has_existing else "#3a3a3a",
            hover_color="#8c4444" if has_existing else "#3a3a3a",
            text_color="white" if has_existing else TEXT_DIM,
            state="normal" if has_existing else "disabled",
            command=self._do_remove if has_existing else (lambda: None),
        ).pack(side="right")

        # Bindings
        self.bind_all_recursive("<Control-s>", lambda e: (self._do_save(), "break"))
        self._text.bind("<Control-s>", lambda e: (self._do_save(), "break"))
        self._text.bind("<Escape>", lambda e: (self._do_cancel(), "break"))

        self._text.focus_set()
        self._text.mark_set("insert", "end-1c")
        self._text.see("end-1c")

    def bind_all_recursive(self, sequence, callback):
        """Bind a key on the overlay and its descendants without polluting the global app."""
        self.bind(sequence, callback)
        for child in self.winfo_children():
            try:
                child.bind(sequence, callback)
            except tk.TclError:
                pass

    def _do_save(self):
        text = self._text.get("1.0", "end-1c")
        if text.strip():
            self._on_save(text)
        else:
            self._on_remove()
        self._on_close()

    def _do_remove(self):
        self._on_remove()
        self._on_close()

    def _do_cancel(self):
        self._on_close()

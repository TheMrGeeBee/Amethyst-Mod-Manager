"""
mod_files_overlay.py
Overlay that lists a mod's files on Nexus so the user can pick which one to install.
Shown when the user clicks "Change Version" from the context menu.
"""

from __future__ import annotations

import threading
from typing import Callable

import tkinter as tk
import customtkinter as ctk

from Utils.xdg import open_url
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    BG_DEEP,
    BG_HEADER,
    BG_ROW,
    BG_ROW_ALT,
    TEXT_DIM,
    TEXT_MAIN,
    FONT_BOLD,
    FONT_SMALL,
    scaled,
    TK_FONT_BOLD, TK_FONT_SMALL,
)

_COL_VERSION  = scaled(90)
_COL_CATEGORY = scaled(100)
_COL_SIZE     = scaled(72)
_COL_BUTTONS  = scaled(160)
_INSTALLED_BG = "#1a3320"
_INSTALLED_FG = "#4caf50"
_MATCH_BG = "#3a2a14"
_MATCH_FG = "#ff9a3c"
_OLD_MATCH_BG = "#3a1818"
_OLD_MATCH_FG = "#e05a5a"


class ModFilesOverlay(tk.Frame):
    """
    Overlay that lists Nexus files for a mod.
    Placed over the ModListPanel via place(relx=0, rely=0, relwidth=1, relheight=1).

    on_install(file_id, file_name)  — called when user clicks Install for a file
    on_ignore(state: bool)          — called when the Ignore Update checkbox changes
    on_close()                      — called when user closes the overlay
    on_rehost(going_to_popout)      — called when the popout/dock toggle is clicked;
                                      the launcher rebuilds the overlay in the other
                                      host (Tk can't reparent widgets across toplevels)
    """

    def __init__(
        self,
        parent: tk.Widget,
        mod_name: str,
        game_domain: str,
        mod_id: int,
        installed_file_id: int,
        ignore_update: bool,
        on_install: Callable[[int, str], None],
        on_ignore: Callable[[bool], None],
        on_close: Callable[[], None],
        fetch_files_fn: Callable[[], list],
        on_rehost: Callable[[bool], None] | None = None,
        is_popped_out: bool = False,
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._mod_name = mod_name
        self._game_domain = game_domain
        self._mod_id = mod_id
        self._installed_file_id = installed_file_id
        self._on_install = on_install
        self._on_ignore = on_ignore
        self._on_close = on_close
        self._fetch_files_fn = fetch_files_fn
        self._on_rehost = on_rehost
        self._is_popped_out = is_popped_out
        self._files: list = []
        self._ignore_var = tk.BooleanVar(value=ignore_update)
        self._build()
        threading.Thread(target=self._load_files, daemon=True).start()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build(self):
        self.grid_rowconfigure(0, weight=0)  # toolbar
        self.grid_rowconfigure(1, weight=1)  # scroll area
        self.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(self, bg=BG_HEADER, height=scaled(42))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=85, height=30,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_close,
        ).pack(side="right", padx=(6, 12), pady=5)

        # Popout / dock toggle. Hidden when no re-host handler is wired up.
        if self._on_rehost is not None:
            popout_btn = ctk.CTkButton(
                toolbar, text=("⤡" if self._is_popped_out else "⤢"),
                width=32, height=30, font=FONT_BOLD,
                fg_color=BG_HEADER, hover_color="#555",
                text_color=TEXT_MAIN, corner_radius=4,
                command=self._do_popout_toggle,
            )
            popout_btn.pack(side="right", padx=(0, 6), pady=5)
            try:
                from gui.ctk_tooltip import CTkToolTip
                CTkToolTip(
                    popout_btn,
                    message="Dock back to main window" if self._is_popped_out
                    else "Open in a separate window",
                )
            except Exception:
                pass

        ctk.CTkCheckBox(
            toolbar,
            text="Ignore Update",
            variable=self._ignore_var,
            command=self._on_ignore_toggled,
            font=FONT_SMALL,
            text_color=TEXT_MAIN,
            fg_color=ACCENT,
            hover_color=ACCENT_HOV,
            checkmark_color="white",
            border_color="#666",
        ).pack(side="right", padx=(0, 12), pady=5)

        tk.Label(
            toolbar,
            text=f"Change Version — {self._mod_name}",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
            anchor="w",
        ).pack(side="left", padx=12, pady=8, fill="x", expand=True)

        # Scrollable content area
        scroll_frame = tk.Frame(self, bg=BG_DEEP)
        scroll_frame.grid(row=1, column=0, sticky="nsew")
        scroll_frame.grid_rowconfigure(0, weight=1)
        scroll_frame.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(scroll_frame, bg=BG_DEEP, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(scroll_frame, orient="vertical", command=self._canvas.yview,
                           bg="#383838", troughcolor=BG_DEEP, highlightthickness=0, bd=0)
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self._inner = tk.Frame(self._canvas, bg=BG_DEEP)
        self._inner_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")

        self._inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        self._resize_after_id: str | None = None
        self._destroyed = False

        # Bind scroll on the root window so it fires regardless of which child has focus
        _root = self.winfo_toplevel()
        if not LEGACY_WHEEL_REDUNDANT:
            self._scroll_bid4 = _root.bind("<Button-4>", self._on_scroll_up, add="+")
            self._scroll_bid5 = _root.bind("<Button-5>", self._on_scroll_down, add="+")
            self._scroll_bid_wheel = None
        else:
            self._scroll_bid4 = None
            self._scroll_bid5 = None
            self._scroll_bid_wheel = _root.bind("<MouseWheel>", self._on_mousewheel, add="+")

        # Shared grid columns on _inner (used by header row + all data rows)
        self._inner.grid_columnconfigure(0, weight=1)                         # file name
        self._inner.grid_columnconfigure(1, minsize=_COL_VERSION,  weight=0)  # version
        self._inner.grid_columnconfigure(2, minsize=_COL_CATEGORY, weight=0)  # category
        self._inner.grid_columnconfigure(3, minsize=_COL_SIZE,     weight=0)  # size
        self._inner.grid_columnconfigure(4, minsize=_COL_BUTTONS,  weight=0)  # buttons

        # Header row (row 0 of _inner)
        for col, text in enumerate(("File", "Version", "Category", "Size", "")):
            ipadx = 12 if col == 0 else 8
            tk.Label(
                self._inner, text=text,
                font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_HEADER, anchor="w",
            ).grid(row=0, column=col, sticky="nsew", ipadx=ipadx, ipady=4)

        # Loading placeholder
        self._loading_lbl = tk.Label(
            self._inner, text="Loading files…",
            font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP,
        )
        self._loading_lbl.grid(row=1, column=0, columnspan=5, padx=20, pady=20, sticky="w")

    # ------------------------------------------------------------------
    # Scroll / resize helpers
    # ------------------------------------------------------------------

    def _on_inner_configure(self, _evt=None):
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, evt):
        # Debounce: only apply after resizing settles (50 ms)
        if self._resize_after_id:
            self.after_cancel(self._resize_after_id)
        w = evt.width
        def _apply():
            if not self._destroyed:
                self._canvas.itemconfigure(self._inner_id, width=w)
        self._resize_after_id = self.after(50, _apply)

    def _on_scroll_up(self, _evt):
        self._canvas.yview_scroll(-3, "units")

    def _on_scroll_down(self, _evt):
        self._canvas.yview_scroll(3, "units")

    def _on_mousewheel(self, evt):
        self._canvas.yview_scroll(-3 if (getattr(evt, "delta", 0) or 0) > 0 else 3, "units")

    def cleanup(self):
        """Release scroll bindings and cancel pending timers. Idempotent."""
        if self._destroyed:
            return
        self._destroyed = True
        if self._resize_after_id:
            self.after_cancel(self._resize_after_id)
            self._resize_after_id = None
        try:
            _root = self.winfo_toplevel()
            if not LEGACY_WHEEL_REDUNDANT:
                _root.unbind("<Button-4>", self._scroll_bid4)
                _root.unbind("<Button-5>", self._scroll_bid5)
            elif self._scroll_bid_wheel:
                _root.unbind("<MouseWheel>", self._scroll_bid_wheel)
        except Exception:
            pass

    def _do_close(self):
        self.cleanup()
        self._on_close()

    def _do_popout_toggle(self):
        """Hand off to the launcher, which rebuilds the overlay in the other
        host (docked overlay vs. separate window). The file list reloads on
        show, so a fresh overlay is built in the new host."""
        if self._on_rehost is None:
            return
        going_to_popout = not self._is_popped_out
        self.cleanup()
        self._on_rehost(going_to_popout)

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def _load_files(self):
        try:
            files = self._fetch_files_fn()
        except Exception as exc:
            self.after(0, lambda e=exc: self._show_error(str(e)))
            return
        self.after(0, lambda: self._populate(files))

    def _show_error(self, msg: str):
        if not self.winfo_exists():
            return
        self._loading_lbl.configure(text=f"Error: {msg}", fg="#cc4444")

    def _populate(self, files: list):
        if not self.winfo_exists():
            return
        self._loading_lbl.destroy()

        if not files:
            tk.Label(
                self._inner, text="No files found.",
                font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP,
            ).grid(row=1, column=0, columnspan=5, padx=20, pady=20, sticky="w")
            return

        # Sort: MAIN first, then UPDATE, then others; newest first within category
        _ORDER = {"MAIN": 0, "UPDATE": 1, "OPTIONAL": 2, "MISCELLANEOUS": 3, "OLD_VERSION": 4}
        files = sorted(
            files,
            key=lambda f: (_ORDER.get((f.category_name or "").upper(), 9), -(f.uploaded_timestamp or 0)),
        )

        # Newest file whose display name matches the installed file's display
        # name (same Nexus "slot"/variant) — helps the user spot the right entry
        # when a page lists many same-mod-id variants (e.g. one Main + many
        # Optional patches).
        match_id, old_match_ids = resolve_latest_name_match(
            files, self._installed_file_id, self._mod_name)

        for row_idx, f in enumerate(files):
            # grid row 0 is the header, data starts at 1
            grid_row = row_idx + 1
            is_installed = (self._installed_file_id > 0 and f.file_id == self._installed_file_id)
            is_match = (not is_installed and match_id > 0 and f.file_id == match_id)
            is_old_match = (not is_installed and not is_match and f.file_id in old_match_ids)
            if is_installed:
                bg = _INSTALLED_BG
                name_fg = _INSTALLED_FG
            elif is_match:
                bg = _MATCH_BG
                name_fg = _MATCH_FG
            elif is_old_match:
                bg = _OLD_MATCH_BG
                name_fg = _OLD_MATCH_FG
            else:
                bg = BG_ROW if row_idx % 2 == 0 else BG_ROW_ALT
                name_fg = TEXT_MAIN
            name_text = (f.name or f.file_name) + ("  ✓" if is_installed else "")

            # File name
            tk.Label(
                self._inner, text=name_text,
                font=TK_FONT_SMALL, fg=name_fg, bg=bg, anchor="w",
            ).grid(row=grid_row, column=0, sticky="nsew", ipadx=12, ipady=6)

            # Version
            tk.Label(
                self._inner, text=f.version or "",
                font=TK_FONT_SMALL, fg=TEXT_DIM, bg=bg, anchor="w",
            ).grid(row=grid_row, column=1, sticky="nsew", ipadx=8, ipady=6)

            # Category
            tk.Label(
                self._inner, text=(f.category_name or "").capitalize(),
                font=TK_FONT_SMALL, fg=TEXT_DIM, bg=bg, anchor="w",
            ).grid(row=grid_row, column=2, sticky="nsew", ipadx=8, ipady=6)

            # Size
            size_str = _fmt_size(f.size_in_bytes or (f.size_kb * 1024 if f.size_kb else 0))
            tk.Label(
                self._inner, text=size_str,
                font=TK_FONT_SMALL, fg=TEXT_DIM, bg=bg, anchor="w",
            ).grid(row=grid_row, column=3, sticky="nsew", ipadx=8, ipady=6)

            # Buttons
            btn_frame = tk.Frame(self._inner, bg=bg)
            btn_frame.grid(row=grid_row, column=4, sticky="nsew")

            nexus_url = (
                f"https://www.nexusmods.com/{self._game_domain}/mods/{self._mod_id}"
                f"?tab=files&file_id={f.file_id}"
            )
            ctk.CTkButton(
                btn_frame, text="View", width=60, height=28,
                fg_color="#444", hover_color="#555",
                text_color=TEXT_MAIN, font=FONT_SMALL,
                command=lambda url=nexus_url: open_url(url),
            ).pack(side="left", padx=(8, 6), pady=6)

            fid, fname = f.file_id, f.file_name
            ctk.CTkButton(
                btn_frame, text="Install", width=70, height=28,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
                text_color=TEXT_ON_ACCENT, font=FONT_SMALL,
                command=lambda i=fid, n=fname: self._do_install(i, n),
            ).pack(side="left", pady=6)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_ignore_toggled(self):
        self._on_ignore(self._ignore_var.get())

    def _do_install(self, file_id: int, file_name: str):
        self._do_close()
        self._on_install(file_id, file_name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_latest_name_match(files, installed_file_id: int,
                              fallback_name: str) -> tuple[int, set[int]]:
    """Resolve the file the Change Version window highlights orange.

    Returns ``(match_id, old_match_ids)`` where ``match_id`` is the newest file
    whose display name matches the installed file's name (or *fallback_name* if
    the installed file isn't in the list), or ``-1`` when there is no name match.
    ``old_match_ids`` are the other same-name files (the red "old match" rows)."""
    installed_file = next(
        (f for f in files
         if installed_file_id > 0 and f.file_id == installed_file_id),
        None,
    )
    # Prefer the installed file's name over the local folder name because the
    # user may have renamed the mod on install.
    target = _normalize_match(
        (installed_file.name or installed_file.file_name)
        if installed_file else fallback_name
    )
    match_id = -1
    old_match_ids: set[int] = set()
    if target:
        name_matches = [f for f in files
                        if _normalize_match(f.name or "") == target]
        if name_matches:
            newest = max(name_matches, key=lambda f: f.uploaded_timestamp or 0)
            match_id = newest.file_id
            old_match_ids = {f.file_id for f in name_matches
                             if f.file_id != match_id}
    return match_id, old_match_ids


def _normalize_match(s: str) -> str:
    """Casefold and collapse whitespace/punctuation so that names like
    'Cargo Reconsidered - Watchtower' and 'Cargo Reconsidered  -  Watchtower'
    compare equal. Returns '' for empty input."""
    if not s:
        return ""
    out = []
    prev_sep = True
    for ch in s.casefold():
        if ch.isalnum():
            out.append(ch)
            prev_sep = False
        else:
            if not prev_sep:
                out.append(" ")
                prev_sep = True
    return "".join(out).strip()


def _fmt_size(n_bytes: int) -> str:
    if not n_bytes:
        return ""
    if n_bytes >= 1_073_741_824:
        return f"{n_bytes / 1_073_741_824:.1f} GB"
    if n_bytes >= 1_048_576:
        return f"{n_bytes / 1_048_576:.1f} MB"
    if n_bytes >= 1_024:
        return f"{n_bytes / 1_024:.0f} KB"
    return f"{n_bytes} B"

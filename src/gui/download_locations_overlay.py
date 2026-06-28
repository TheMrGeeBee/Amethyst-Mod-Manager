"""
Download locations overlay — manage extra folders to scan for archives.

Shown when the user clicks the Locations button in the Downloads tab.
Lets users add/remove paths; saved to ~/.config/AmethystModManager/download_locations.json.

The default Downloads folder (XDG_DOWNLOAD_DIR or ~/Downloads) is always
listed first. Users may disable it so it's skipped during scans; this is
persisted via the `default_disabled` flag in the config file.
"""

from __future__ import annotations

import json
import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

from Utils.config_paths import get_download_locations_path
from Utils.portal_filechooser import pick_folder
from Utils.xdg import xdg_download_dir

from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.theme import (
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    ACCENT,
    TEXT_MAIN,
    TEXT_DIM,
    FONT_BOLD,
    FONT_SMALL,
    TK_FONT_BOLD,
    TK_FONT_SMALL, scaled,
)


def _read_config() -> tuple[list[str], bool, bool]:
    """Load (extras, default_disabled, cache_disabled) from config.

    Supports the legacy format (a bare JSON list of paths) as well as the
    newer object form
    ``{"extras": [...], "default_disabled": bool, "cache_disabled": bool}``.
    """
    path = get_download_locations_path()
    if not path.is_file():
        return [], False, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], False, False
    if isinstance(data, list):
        return [str(p).strip() for p in data if p and str(p).strip()], False, False
    if isinstance(data, dict):
        raw = data.get("extras", [])
        extras = (
            [str(p).strip() for p in raw if p and str(p).strip()]
            if isinstance(raw, list) else []
        )
        return (
            extras,
            bool(data.get("default_disabled", False)),
            bool(data.get("cache_disabled", False)),
        )
    return [], False, False


def _write_config(extras: list[str], default_disabled: bool,
                  cache_disabled: bool) -> None:
    path = get_download_locations_path()
    path.write_text(
        json.dumps(
            {
                "extras": extras,
                "default_disabled": default_disabled,
                "cache_disabled": cache_disabled,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _load_locations() -> list[str]:
    """Load extra download scan paths from config."""
    return _read_config()[0]


def _save_locations(locations: list[str]) -> None:
    """Save extra download scan paths (preserving the toggle flags)."""
    _, disabled, cache_disabled = _read_config()
    _write_config(locations, disabled, cache_disabled)


def get_default_downloads_dir() -> Path:
    """Return the system default Downloads folder (per xdg-user-dirs)."""
    return xdg_download_dir()


def is_default_downloads_disabled() -> bool:
    """True if the user has opted out of scanning the default Downloads folder."""
    return _read_config()[1]


def set_default_downloads_disabled(disabled: bool) -> None:
    extras, _, cache_disabled = _read_config()
    _write_config(extras, bool(disabled), cache_disabled)


def is_cache_default_disabled() -> bool:
    """True if the user has opted out of scanning the active game's
    download_cache folder."""
    return _read_config()[2]


def set_cache_default_disabled(disabled: bool) -> None:
    extras, default_disabled, _ = _read_config()
    _write_config(extras, default_disabled, bool(disabled))


def load_extra_download_locations() -> list[str]:
    """Return extra scan paths only (excludes the default Downloads folder)."""
    return _load_locations()


def get_effective_download_locations() -> list[Path]:
    """Return all folders that should be scanned for archives.

    Includes the default Downloads folder (unless the user disabled it) plus
    any user-added extras. De-duplicated by resolved path.
    """
    dirs: list[Path] = []
    seen: set[Path] = set()
    if not is_default_downloads_disabled():
        default = get_default_downloads_dir()
        try:
            key = default.resolve()
        except OSError:
            key = default
        dirs.append(default)
        seen.add(key)
    for p in _load_locations():
        path = Path(p).expanduser()
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        dirs.append(path)
        seen.add(key)
    return dirs


class DownloadLocationsOverlay(tk.Frame):
    """
    Overlay for managing extra download scan locations.
    Placed over the plugin panel when the user clicks Locations.
    """

    def __init__(
        self,
        parent: tk.Widget,
        on_close: Optional[Callable[[], None]] = None,
        on_saved: Optional[Callable[[], None]] = None,
        active_game_name: str = "",
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._on_close = on_close
        self._on_saved = on_saved
        self._active_game_name = active_game_name or ""
        extras, disabled, cache_disabled = _read_config()
        self._locations: list[str] = extras
        self._default_disabled: bool = disabled
        self._cache_disabled: bool = cache_disabled

        self._build()

    def _build(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(self, bg=BG_HEADER, height=scaled(42))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        tk.Label(
            toolbar, text="Download Locations",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=85, height=30,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_close,
        ).pack(side="right", padx=(6, 12), pady=5)

        # Content
        content = tk.Frame(self, bg=BG_DEEP)
        content.grid(row=1, column=0, sticky="nsew", padx=12, pady=12)
        content.grid_rowconfigure(1, weight=1)
        content.grid_columnconfigure(0, weight=1)

        tk.Label(
            content,
            text=(
                "Folders to scan for mod archives. The default Downloads folder "
                "is included automatically — remove it if you don't want it scanned."
            ),
            font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP, wraplength=400,
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        # Scrollable list of paths
        list_frame = tk.Frame(content, bg=BG_PANEL, bd=0, highlightthickness=0)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        self._list_canvas = tk.Canvas(
            list_frame, bg=BG_PANEL, bd=0,
            highlightthickness=0, yscrollincrement=1,
        )
        self._list_canvas.bind("<MouseWheel>", self._on_list_scroll)
        if not LEGACY_WHEEL_REDUNDANT:
            self._list_canvas.bind("<Button-4>", lambda e: self._list_canvas.yview_scroll(-3, "units"))
            self._list_canvas.bind("<Button-5>", lambda e: self._list_canvas.yview_scroll(3, "units"))
        self._list_vsb = tk.Scrollbar(
            list_frame, orient="vertical", command=self._list_canvas.yview,
            bg="#383838", troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        self._list_canvas.configure(yscrollcommand=self._list_vsb.set)
        self._list_canvas.grid(row=0, column=0, sticky="nsew")
        self._list_vsb.grid(row=0, column=1, sticky="ns")

        self._list_inner = tk.Frame(self._list_canvas, bg=BG_PANEL)
        self._list_inner_id = self._list_canvas.create_window(
            (0, 0), window=self._list_inner, anchor="nw",
        )
        self._list_inner.bind("<Configure>", self._on_list_configure)
        # Match the inner frame's width to the canvas width so long path
        # labels wrap instead of pushing the Remove button off-screen.
        self._list_canvas.bind("<Configure>", self._on_list_canvas_resize)

        # Add button row
        btn_row = tk.Frame(content, bg=BG_DEEP)
        btn_row.grid(row=2, column=0, sticky="w", pady=(8, 0))

        ctk.CTkButton(
            btn_row, text="+ Add Folder", width=120, height=28,
            fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            font=FONT_BOLD, command=self._on_add,
        ).pack(side="left", padx=(0, 8))

        self._repaint_list()

    def _on_list_configure(self, event):
        self._list_canvas.configure(scrollregion=self._list_canvas.bbox("all"))

    def _on_list_canvas_resize(self, event):
        """Match the inner frame's width to the canvas viewport width and
        re-wrap path labels so a long path can't push the Remove button
        off-screen."""
        new_w = max(event.width, 1)
        self._list_canvas.itemconfigure(self._list_inner_id, width=new_w)
        # Allow ~100px for the Remove button + padding before the label
        # has to wrap.
        wrap = max(new_w - 110, 80)
        for child in self._list_inner.winfo_children():
            if not isinstance(child, tk.Frame):
                continue
            for sub in child.winfo_children():
                if isinstance(sub, tk.Label):
                    try:
                        sub.configure(wraplength=wrap)
                    except Exception:
                        pass

    def _on_list_scroll(self, event):
        self._list_canvas.yview_scroll(-3 if (event.delta or 0) > 0 else 3, "units")

    def _repaint_list(self):
        """Rebuild the list of path rows."""
        for w in self._list_inner.winfo_children():
            w.destroy()

        self._list_inner.grid_columnconfigure(0, weight=1)
        row_idx = 0
        # Compute an initial wraplength from the canvas's current width so
        # long paths wrap immediately rather than after the next Configure.
        cw = self._list_canvas.winfo_width()
        wrap = max(cw - 110, 80) if cw > 1 else 360

        # Default Downloads row — always shown, either as an active entry with
        # a Remove button, or greyed-out with a Restore button when disabled.
        default_dir = get_default_downloads_dir()
        row = tk.Frame(self._list_inner, bg=BG_PANEL)
        row.grid(row=row_idx, column=0, sticky="ew", pady=2)
        row.grid_columnconfigure(0, weight=1)

        if self._default_disabled:
            lbl_text = f"{default_dir}  (default — disabled)"
            fg_col = TEXT_DIM
            btn = ctk.CTkButton(
                row, text="Restore", width=80, height=24,
                fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
                font=FONT_SMALL, command=self._on_restore_default,
            )
        else:
            lbl_text = f"{default_dir}  (default)"
            fg_col = TEXT_MAIN
            btn = ctk.CTkButton(
                row, text="Remove", width=80, height=24,
                fg_color="#a83232", hover_color="#c43c3c", text_color="white",
                font=FONT_SMALL, command=self._on_remove_default,
            )
        tk.Label(
            row, text=lbl_text, anchor="w", justify="left",
            font=TK_FONT_SMALL, fg=fg_col, bg=BG_PANEL,
            wraplength=wrap,
        ).grid(row=0, column=0, sticky="ew", padx=8, pady=4)
        btn.grid(row=0, column=1, padx=4, pady=4, sticky="ne")
        row_idx += 1

        # Per-game Mod Manager Cache row — only shown when a game is
        # active. Its enabled / disabled state is independent of the
        # Downloads default.
        cache_dir = self._get_active_cache_dir()
        if cache_dir is not None:
            cache_row = tk.Frame(self._list_inner, bg=BG_PANEL)
            cache_row.grid(row=row_idx, column=0, sticky="ew", pady=2)
            cache_row.grid_columnconfigure(0, weight=1)

            if self._cache_disabled:
                cache_lbl_text = f"{cache_dir}  (cache — disabled)"
                cache_fg = TEXT_DIM
                cache_btn = ctk.CTkButton(
                    cache_row, text="Restore", width=80, height=24,
                    fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
                    font=FONT_SMALL, command=self._on_restore_cache,
                )
            else:
                cache_lbl_text = f"{cache_dir}  (cache for {self._active_game_name})"
                cache_fg = TEXT_MAIN
                cache_btn = ctk.CTkButton(
                    cache_row, text="Remove", width=80, height=24,
                    fg_color="#a83232", hover_color="#c43c3c", text_color="white",
                    font=FONT_SMALL, command=self._on_remove_cache,
                )
            tk.Label(
                cache_row, text=cache_lbl_text, anchor="w", justify="left",
                font=TK_FONT_SMALL, fg=cache_fg, bg=BG_PANEL,
                wraplength=wrap,
            ).grid(row=0, column=0, sticky="ew", padx=8, pady=4)
            cache_btn.grid(row=0, column=1, padx=4, pady=4, sticky="ne")
            row_idx += 1

        for extras_idx, path_str in enumerate(self._locations):
            row = tk.Frame(self._list_inner, bg=BG_PANEL)
            row.grid(row=row_idx, column=0, sticky="ew", pady=2)
            row.grid_columnconfigure(0, weight=1)

            path = Path(path_str).expanduser()
            display = str(path) if path.is_dir() else str(path_str)
            tk.Label(
                row, text=display, anchor="w", justify="left",
                font=TK_FONT_SMALL, fg=TEXT_MAIN, bg=BG_PANEL,
                wraplength=wrap,
            ).grid(row=0, column=0, sticky="ew", padx=8, pady=4)

            ctk.CTkButton(
                row, text="Remove", width=80, height=24,
                fg_color="#a83232", hover_color="#c43c3c", text_color="white",
                font=FONT_SMALL, command=lambda idx=extras_idx: self._on_remove(idx),
            ).grid(row=0, column=1, padx=4, pady=4, sticky="ne")
            row_idx += 1

        if not self._locations and not self._default_disabled:
            tk.Label(
                self._list_inner,
                text="Click 'Add Folder' to scan additional locations.",
                font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL,
            ).grid(row=row_idx, column=0, sticky="w", padx=8, pady=8)

    def _on_add(self):
        root = self.winfo_toplevel()

        def _on_picked(chosen: Path | None) -> None:
            root.after(0, lambda: self._add_picked(chosen))

        pick_folder("Select folder to scan for archives", _on_picked)

    def _add_picked(self, chosen: Path | None) -> None:
        if chosen is None:
            return
        path_str = str(chosen.resolve())
        if path_str not in self._locations:
            self._locations.append(path_str)
            _save_locations(self._locations)
            self._repaint_list()
            if self._on_saved:
                self._on_saved()

    def _get_active_cache_dir(self) -> Path | None:
        """Return the active game's download_cache folder, or None if no
        game is currently selected."""
        if not self._active_game_name:
            return None
        try:
            from Utils.config_paths import get_download_cache_dir_for_game
            return get_download_cache_dir_for_game(self._active_game_name)
        except Exception:
            return None

    def _on_remove(self, idx: int) -> None:
        if 0 <= idx < len(self._locations):
            self._locations.pop(idx)
            _save_locations(self._locations)
            self._repaint_list()
            if self._on_saved:
                self._on_saved()

    def _on_remove_default(self) -> None:
        self._default_disabled = True
        _write_config(self._locations, True, self._cache_disabled)
        self._repaint_list()
        if self._on_saved:
            self._on_saved()

    def _on_restore_default(self) -> None:
        self._default_disabled = False
        _write_config(self._locations, False, self._cache_disabled)
        self._repaint_list()
        if self._on_saved:
            self._on_saved()

    def _on_remove_cache(self) -> None:
        self._cache_disabled = True
        _write_config(self._locations, self._default_disabled, True)
        self._repaint_list()
        if self._on_saved:
            self._on_saved()

    def _on_restore_cache(self) -> None:
        self._cache_disabled = False
        _write_config(self._locations, self._default_disabled, False)
        self._repaint_list()
        if self._on_saved:
            self._on_saved()

    def _do_close(self):
        if self._on_close:
            self._on_close()
        else:
            self.place_forget()

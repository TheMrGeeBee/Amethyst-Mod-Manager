"""Cache manager overlay — browse per-game download caches and clear selectively."""

from __future__ import annotations

import shutil
import threading
import tkinter as tk
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

from Utils.config_paths import get_download_cache_dir
from gui.ctk_components import CTkAlert
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.theme import (
    ACCENT,
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    FONT_BOLD,
    FONT_NORMAL,
    FONT_SMALL,
    TEXT_DIM,
    TEXT_ERR,
    TEXT_MAIN,
    TEXT_OK,
    scaled,
)

_CLEAR_ALL_PRESERVE = {"md5_cache.json"}


def _fmt_size(n_bytes: int) -> str:
    if n_bytes <= 0:
        return "—"
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n_bytes >= threshold:
            return f"{n_bytes / threshold:.1f} {unit}"
    return f"{n_bytes} B"


def _get_dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def _enumerate_game_caches(cache_dir: Path) -> list[Path]:
    """Subdirectories at the cache root, sorted by name."""
    if not cache_dir.is_dir():
        return []
    try:
        return sorted(
            (p for p in cache_dir.iterdir()
             if p.is_dir() and p.name not in _CLEAR_ALL_PRESERVE),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []


class CacheManagerOverlay(ctk.CTkFrame):
    """Per-game cache browser. Place over the plugin panel container."""

    def __init__(
        self,
        parent: tk.Widget,
        on_close: Optional[Callable[[], None]] = None,
        active_game_name: str = "",
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close = on_close
        self._active_game_name = (active_game_name or "").strip()
        self._cache_dir = get_download_cache_dir()
        self._check_vars: dict[str, tk.BooleanVar] = {}
        self._size_labels: dict[str, ctk.CTkLabel] = {}
        self._row_frames: dict[str, tk.Frame] = {}
        self._total_size: int = 0
        self._build()
        self.after(50, self._refresh_sizes)

    # ---- layout ------------------------------------------------------------

    def _build(self) -> None:
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(self, bg=BG_HEADER, height=scaled(42))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        tk.Label(
            toolbar, text="Manage Download Caches",
            font=FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            toolbar, text="✕ Close",
            width=scaled(85), height=scaled(30),
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_close,
        ).pack(side="right", padx=(6, 12), pady=5)

        # Header / summary
        header = tk.Frame(self, bg=BG_DEEP)
        header.grid(row=1, column=0, sticky="ew", padx=12, pady=(12, 4))
        header.grid_columnconfigure(0, weight=1)

        self._desc_lbl = tk.Label(
            header,
            text=f"Location: {self._cache_dir}",
            font=FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP,
            justify="left", anchor="w", wraplength=scaled(400),
        )
        self._desc_lbl.grid(row=0, column=0, sticky="ew")
        header.bind(
            "<Configure>",
            lambda e: self._desc_lbl.configure(wraplength=max(e.width - 8, 80)),
        )

        self._total_lbl = tk.Label(
            header, text="Total: calculating…",
            font=FONT_NORMAL, fg=TEXT_MAIN, bg=BG_DEEP, anchor="w",
        )
        self._total_lbl.grid(row=1, column=0, sticky="w", pady=(6, 0))

        # Scrollable list
        list_frame = tk.Frame(self, bg=BG_PANEL, bd=0, highlightthickness=0)
        list_frame.grid(row=2, column=0, sticky="nsew", padx=12, pady=(4, 8))
        list_frame.grid_rowconfigure(0, weight=1)
        list_frame.grid_columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            list_frame, bg=BG_PANEL, bd=0,
            highlightthickness=0, yscrollincrement=1,
        )
        self._canvas.bind("<MouseWheel>", self._on_scroll)
        if not LEGACY_WHEEL_REDUNDANT:
            self._canvas.bind("<Button-4>", lambda e: self._canvas.yview_scroll(-3, "units"))
            self._canvas.bind("<Button-5>", lambda e: self._canvas.yview_scroll(3, "units"))
        self._vsb = tk.Scrollbar(
            list_frame, orient="vertical", command=self._canvas.yview,
            bg="#383838", troughcolor=BG_DEEP, activebackground=ACCENT,
            highlightthickness=0, bd=0,
        )
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._vsb.grid(row=0, column=1, sticky="ns")

        self._inner = tk.Frame(self._canvas, bg=BG_PANEL)
        self._inner_id = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw",
        )
        self._inner.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
        )
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfigure(self._inner_id, width=max(e.width, 1)),
        )

        # Status + action bar
        action = tk.Frame(self, bg=BG_DEEP)
        action.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        action.grid_columnconfigure(0, weight=1)

        self._status_lbl = tk.Label(
            action, text="", font=FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP, anchor="w",
        )
        self._status_lbl.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        btn_row = tk.Frame(action, bg=BG_DEEP)
        btn_row.grid(row=1, column=0, sticky="ew")
        for col in range(4):
            btn_row.grid_columnconfigure(col, weight=1, uniform="cache_btns")

        ctk.CTkButton(
            btn_row, text="All",
            height=scaled(30),
            fg_color="#3a4a5a", hover_color="#4a6a7a", text_color="white",
            font=FONT_NORMAL, command=self._select_all,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))

        ctk.CTkButton(
            btn_row, text="None",
            height=scaled(30),
            fg_color="#3a4a5a", hover_color="#4a6a7a", text_color="white",
            font=FONT_NORMAL, command=self._select_none,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 4))

        self._clear_sel_btn = ctk.CTkButton(
            btn_row, text="Clear Selected",
            height=scaled(30),
            fg_color="#5a3a00", hover_color="#7a5200", text_color="white",
            font=FONT_BOLD, command=self._on_clear_selected,
        )
        self._clear_sel_btn.grid(row=0, column=2, sticky="ew", padx=(0, 4))

        self._clear_all_btn = ctk.CTkButton(
            btn_row, text="Clear All",
            height=scaled(30),
            fg_color="#a83232", hover_color="#c43c3c", text_color="white",
            font=FONT_BOLD, command=self._on_clear_all,
        )
        self._clear_all_btn.grid(row=0, column=3, sticky="ew")

        self._repaint()

    # ---- list painting -----------------------------------------------------

    def _repaint(self) -> None:
        for w in self._inner.winfo_children():
            w.destroy()
        self._check_vars.clear()
        self._size_labels.clear()
        self._row_frames.clear()
        self._inner.grid_columnconfigure(1, weight=1)

        games = _enumerate_game_caches(self._cache_dir)
        if not games:
            tk.Label(
                self._inner,
                text="No per-game caches found.",
                font=FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL,
            ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=12)
            return

        for idx, game_dir in enumerate(games):
            name = game_dir.name
            is_active = (name == self._active_game_name)
            row = tk.Frame(self._inner, bg=BG_PANEL)
            row.grid(row=idx, column=0, columnspan=3, sticky="ew", pady=1)
            row.grid_columnconfigure(1, weight=1)
            self._row_frames[name] = row

            var = tk.BooleanVar(value=False)
            self._check_vars[name] = var
            ctk.CTkCheckBox(
                row, text="", variable=var, width=scaled(24),
            ).grid(row=0, column=0, padx=(8, 6), pady=4)

            label_text = f"{name}  (active)" if is_active else name
            tk.Label(
                row, text=label_text, anchor="w",
                font=FONT_NORMAL,
                fg=(TEXT_OK if is_active else TEXT_MAIN), bg=BG_PANEL,
            ).grid(row=0, column=1, sticky="ew", padx=(2, 8), pady=4)

            size_lbl = ctk.CTkLabel(
                row, text="—", font=FONT_SMALL, text_color=TEXT_DIM, anchor="e",
                width=scaled(80),
            )
            size_lbl.grid(row=0, column=2, sticky="e", padx=(0, 12), pady=4)
            self._size_labels[name] = size_lbl

    # ---- size refresh ------------------------------------------------------

    def _refresh_sizes(self) -> None:
        games = list(self._size_labels.keys())
        cache_dir = self._cache_dir

        def _worker():
            sizes: dict[str, int] = {}
            for name in games:
                sizes[name] = _get_dir_size(cache_dir / name)
            try:
                self.after(0, lambda: self._apply_sizes(sizes))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_sizes(self, sizes: dict[str, int]) -> None:
        total = 0
        for name, sz in sizes.items():
            total += sz
            lbl = self._size_labels.get(name)
            if lbl is not None:
                try:
                    lbl.configure(text=_fmt_size(sz))
                except Exception:
                    pass
        self._total_size = total
        try:
            self._total_lbl.configure(text=f"Total: {_fmt_size(total)}")
        except Exception:
            pass

    # ---- selection helpers -------------------------------------------------

    def _select_all(self) -> None:
        for var in self._check_vars.values():
            var.set(True)

    def _select_none(self) -> None:
        for var in self._check_vars.values():
            var.set(False)

    def _selected_names(self) -> list[str]:
        return [name for name, var in self._check_vars.items() if var.get()]

    # ---- clear actions -----------------------------------------------------

    def _on_clear_selected(self) -> None:
        names = self._selected_names()
        if not names:
            self._status_lbl.configure(text="Nothing selected.", fg=TEXT_DIM)
            return

        total = 0
        for name in names:
            lbl = self._size_labels.get(name)
            if lbl is not None:
                # parse back is overkill — just recompute
                pass
            total += _get_dir_size(self._cache_dir / name)

        listing = "\n".join(f"  • {n}" for n in names[:10])
        if len(names) > 10:
            listing += f"\n  • …and {len(names) - 10} more"

        alert = CTkAlert(
            state="warning",
            title=f"Clear {len(names)} Cache{'s' if len(names) != 1 else ''}",
            body_text=(
                f"Clear {_fmt_size(total)} across {len(names)} game(s)?\n\n"
                f"{listing}\n\n"
                "Archives will be re-downloaded as needed."
            ),
            btn1="Clear",
            btn2="Cancel",
            parent=self.winfo_toplevel(),
            height=320,
        )
        if alert.get() != "Clear":
            return

        self._run_clear(names)

    def _on_clear_all(self) -> None:
        names = list(self._check_vars.keys())
        if not names:
            self._status_lbl.configure(text="Cache is empty.", fg=TEXT_DIM)
            return

        total = sum(_get_dir_size(self._cache_dir / n) for n in names)
        alert = CTkAlert(
            state="warning",
            title="Clear All Download Caches",
            body_text=(
                f"Clear {_fmt_size(total)} of cached downloads across every game?\n\n"
                f"Location: {self._cache_dir}\n\n"
                "The md5 cache is preserved. "
                "Archives will be re-downloaded as needed."
            ),
            btn1="Clear",
            btn2="Cancel",
            parent=self.winfo_toplevel(),
            height=280,
        )
        if alert.get() != "Clear":
            return

        self._run_clear(names)

    def _run_clear(self, names: list[str]) -> None:
        self._clear_sel_btn.configure(state="disabled")
        self._clear_all_btn.configure(state="disabled")
        self._status_lbl.configure(text="Clearing…", fg=TEXT_DIM)

        cache_dir = self._cache_dir

        def _worker():
            cleared = 0
            errors: list[str] = []
            for name in names:
                target = cache_dir / name
                try:
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                        cleared += 1
                except OSError as exc:
                    errors.append(f"{name}: {exc}")
            try:
                self.after(0, lambda: self._on_clear_done(cleared, errors))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_clear_done(self, cleared: int, errors: list[str]) -> None:
        try:
            self._clear_sel_btn.configure(state="normal")
            self._clear_all_btn.configure(state="normal")
        except Exception:
            pass
        if errors:
            self._status_lbl.configure(
                text=f"Cleared {cleared}; {len(errors)} failed.", fg=TEXT_ERR)
        else:
            self._status_lbl.configure(
                text=f"Cleared {cleared} cache{'s' if cleared != 1 else ''}.",
                fg=TEXT_OK)
        self._repaint()
        self._refresh_sizes()

    # ---- scroll / close ----------------------------------------------------

    def _on_scroll(self, event) -> None:
        self._canvas.yview_scroll(-3 if (event.delta or 0) > 0 else 3, "units")

    def _do_close(self) -> None:
        if callable(self._on_close):
            try:
                self._on_close()
                return
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass

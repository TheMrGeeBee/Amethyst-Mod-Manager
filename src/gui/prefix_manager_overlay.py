"""Prefix manager overlay — browse every isolated tool Wine/Proton prefix and
delete them selectively.

Wizard tools (Pandora, BodySlide, xEdit, PGPatcher, ESLifier, Wrye Bash, …) each
run in their own ``prefix_<ProtonName>`` directory created next to the tool exe
(see ``wizards/_proton_prefix.py`` / ``gui.dialogs._get_tool_prefix_env``).  Those
exes live either under a game's mod-staging folders (Pandora ships as a mod) or in
the per-game ``Applications/`` folder.  VRAMr/Bendr/ParallaxR instead use shared
``wine_prefixes/<tool>`` directories.

This overlay scans every game, every profile and both staging/Applications roots
(plus the shared wine_prefixes dir) so the user can reclaim disk space without
hunting through each tool's folder by hand.
"""

from __future__ import annotations

import os
import shutil
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import customtkinter as ctk

from Utils.config_paths import get_profiles_dir, get_wine_prefixes_dir
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
    TK_FONT_BOLD, TK_FONT_NORMAL, TK_FONT_SMALL,
)

# Shared wine_prefixes/<tool> dirs are plain WINEPREFIX folders (not prefix_*).
_WINE_PREFIX_TOOLS = {
    "vramr": "VRAMr",
    "bendr": "Bendr",
    "parallaxr": "ParallaxR",
}


def _fmt_size(n_bytes: int) -> str:
    if n_bytes <= 0:
        return "—"
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n_bytes >= threshold:
            return f"{n_bytes / threshold:.1f} {unit}"
    return f"{n_bytes} B"


def _get_dir_size(path: Path) -> int:
    # os.walk(followlinks=False) so the Wine prefix's dosdevices symlinks
    # (e.g. z: -> /) never send us crawling the whole host filesystem.
    if not path.is_dir():
        return 0
    total = 0
    try:
        for dirpath, _dirnames, files in os.walk(path):
            for f in files:
                fp = os.path.join(dirpath, f)
                try:
                    st = os.lstat(fp)
                    if not os.path.islink(fp):
                        total += st.st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


@dataclass
class PrefixEntry:
    """One discovered tool prefix."""

    key: str          # unique id (the absolute path string)
    path: Path        # the prefix_* / wine_prefixes/<tool> directory itself
    tool: str         # tool/application name (folder owning the prefix)
    game: str         # owning game (or "Shared" for wine_prefixes tools)
    location: str     # short context: "Applications", "Staging", profile name, …
    proton: str       # Proton/Wine version label ("" when unknown)


def _classify_location(rel_parts: tuple[str, ...]) -> str:
    """Human label for where a prefix lives, given path parts below the game dir.

    rel_parts excludes the leading game-name segment and the trailing
    ``prefix_*`` + tool-folder segments.
    """
    if not rel_parts:
        return "Staging"
    head = rel_parts[0]
    if head == "Applications":
        return "Applications"
    if head == "mods":
        return "Staging (mods)"
    if head == "overwrite":
        return "Overwrite"
    if head == "profiles" and len(rel_parts) >= 2:
        return f"Profile: {rel_parts[1]}"
    return head


def _scan_root_for_prefixes(root: Path, game: str) -> list[PrefixEntry]:
    """Find every ``prefix_*`` directory under *root* (a single game's tree)."""
    out: list[PrefixEntry] = []
    if not root.is_dir():
        return out
    # os.walk so we can prune: never descend into a discovered prefix dir
    # (they hold a full Wine prefix tree — searching inside is pointless).
    for dirpath, dirnames, _files in os.walk(root):
        found = [d for d in dirnames if d.startswith("prefix_")]
        for d in found:
            p = Path(dirpath) / d
            tool_dir = p.parent
            proton = d[len("prefix_"):]
            try:
                rel = tool_dir.relative_to(root).parts  # e.g. ("Applications", "SSEEdit")
            except ValueError:
                rel = (tool_dir.name,)
            tool = rel[-1] if rel else tool_dir.name
            location = _classify_location(rel[:-1])
            out.append(PrefixEntry(
                key=str(p),
                path=p,
                tool=tool,
                game=game,
                location=location,
                proton=proton,
            ))
        # Prune found prefix dirs so os.walk doesn't recurse into them.
        if found:
            dirnames[:] = [d for d in dirnames if not d.startswith("prefix_")]
    return out


def _enumerate_prefixes() -> list[PrefixEntry]:
    """Discover every tool prefix across all games, profiles and shared dirs."""
    seen: set[str] = set()
    out: list[PrefixEntry] = []

    def _add(entry: PrefixEntry) -> None:
        if entry.key in seen:
            return
        seen.add(entry.key)
        out.append(entry)

    # 1) Every game folder under Profiles/ (covers staging, Applications and
    #    per-profile mods for all games and profiles, default layout).
    profiles_root = get_profiles_dir()
    if profiles_root.is_dir():
        try:
            game_dirs = [d for d in profiles_root.iterdir() if d.is_dir()]
        except OSError:
            game_dirs = []
        for game_dir in game_dirs:
            for entry in _scan_root_for_prefixes(game_dir, game_dir.name):
                _add(entry)

    # 2) Custom staging paths (live outside Profiles/). Pull from configured games.
    try:
        from gui.game_helpers import _GAMES  # type: ignore
        for name, game in list(_GAMES.items()):
            try:
                root = game.get_profile_root()
            except Exception:
                continue
            if root is None or not root.is_dir():
                continue
            # Skip if already covered by the Profiles scan above.
            try:
                if profiles_root in root.parents or root == profiles_root:
                    continue
            except Exception:
                pass
            for entry in _scan_root_for_prefixes(root, name):
                _add(entry)
    except Exception:
        pass

    # 3) Shared wine_prefixes/<tool> dirs (VRAMr / Bendr / ParallaxR) and the
    #    shared_<Proton> wizard-tool prefixes (one per Proton version, reused by
    #    every wizard tool that opts into "Use shared prefix").
    wine_root = get_wine_prefixes_dir()
    if wine_root.is_dir():
        for sub, label in _WINE_PREFIX_TOOLS.items():
            d = wine_root / sub
            if d.is_dir():
                _add(PrefixEntry(
                    key=str(d),
                    path=d,
                    tool=label,
                    game="Shared",
                    location="wine_prefixes",
                    proton="",
                ))
        try:
            shared_dirs = [
                d for d in wine_root.iterdir()
                if d.is_dir() and d.name.startswith("shared_")
            ]
        except OSError:
            shared_dirs = []
        for d in shared_dirs:
            _add(PrefixEntry(
                key=str(d),
                path=d,
                tool="Wizard Tools (shared)",
                game="Shared",
                location="wine_prefixes",
                proton=d.name[len("shared_"):],
            ))

    out.sort(key=lambda e: (e.game.lower(), e.tool.lower(), e.proton.lower()))
    return out


class PrefixManagerOverlay(ctk.CTkFrame):
    """Tool-prefix browser. Place over the plugin panel container."""

    def __init__(
        self,
        parent: tk.Widget,
        on_close: Optional[Callable[[], None]] = None,
        active_game_name: str = "",
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close = on_close
        self._active_game_name = (active_game_name or "").strip()
        self._entries: list[PrefixEntry] = []
        self._check_vars: dict[str, tk.BooleanVar] = {}
        self._size_labels: dict[str, ctk.CTkLabel] = {}
        self._row_frames: dict[str, tk.Frame] = {}
        self._build()
        self.after(50, self._reload)

    # ---- layout ------------------------------------------------------------

    def _build(self) -> None:
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Toolbar
        toolbar = tk.Frame(self, bg=BG_HEADER, height=scaled(42))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)

        tk.Label(
            toolbar, text="Manage Tool Prefixes",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            toolbar, text="✕ Close",
            width=85, height=30,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._do_close,
        ).pack(side="right", padx=(6, 12), pady=5)

        # Header / summary
        header = tk.Frame(self, bg=BG_DEEP)
        header.grid(row=1, column=0, sticky="ew", padx=12, pady=(12, 4))
        header.grid_columnconfigure(0, weight=1)

        self._desc_lbl = tk.Label(
            header,
            text=(
                "Wizard tool prefixes across all games and profiles. "
                "Deleting one frees disk space; it is recreated on next run."
            ),
            font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP,
            justify="left", anchor="w", wraplength=scaled(400),
        )
        self._desc_lbl.grid(row=0, column=0, sticky="ew")
        header.bind(
            "<Configure>",
            lambda e: self._desc_lbl.configure(wraplength=max(e.width - 8, 80)),
        )

        self._total_lbl = tk.Label(
            header, text="Total: calculating…",
            font=TK_FONT_NORMAL, fg=TEXT_MAIN, bg=BG_DEEP, anchor="w",
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
            action, text="", font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP, anchor="w",
        )
        self._status_lbl.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        btn_row = tk.Frame(action, bg=BG_DEEP)
        btn_row.grid(row=1, column=0, sticky="ew")
        for col in range(4):
            btn_row.grid_columnconfigure(col, weight=1, uniform="prefix_btns")

        ctk.CTkButton(
            btn_row, text="All",
            height=30,
            fg_color="#3a4a5a", hover_color="#4a6a7a", text_color="white",
            font=FONT_NORMAL, command=self._select_all,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))

        ctk.CTkButton(
            btn_row, text="None",
            height=30,
            fg_color="#3a4a5a", hover_color="#4a6a7a", text_color="white",
            font=FONT_NORMAL, command=self._select_none,
        ).grid(row=0, column=1, sticky="ew", padx=(0, 4))

        self._del_sel_btn = ctk.CTkButton(
            btn_row, text="Delete Selected",
            height=30,
            fg_color="#5a3a00", hover_color="#7a5200", text_color="white",
            font=FONT_BOLD, command=self._on_delete_selected,
        )
        self._del_sel_btn.grid(row=0, column=2, sticky="ew", padx=(0, 4))

        self._del_all_btn = ctk.CTkButton(
            btn_row, text="Delete All",
            height=30,
            fg_color="#a83232", hover_color="#c43c3c", text_color="white",
            font=FONT_BOLD, command=self._on_delete_all,
        )
        self._del_all_btn.grid(row=0, column=3, sticky="ew")

    # ---- enumeration / list painting --------------------------------------

    def _reload(self) -> None:
        """Re-scan disk for prefixes (off-thread) then repaint + size."""
        self._status_lbl.configure(text="Scanning…", fg=TEXT_DIM)

        def _worker():
            try:
                entries = _enumerate_prefixes()
            except Exception:
                entries = []
            try:
                self.after(0, lambda: self._on_scan_done(entries))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_scan_done(self, entries: list[PrefixEntry]) -> None:
        self._entries = entries
        self._repaint()
        self._status_lbl.configure(text="", fg=TEXT_DIM)
        self._refresh_sizes()

    def _repaint(self) -> None:
        for w in self._inner.winfo_children():
            w.destroy()
        self._check_vars.clear()
        self._size_labels.clear()
        self._row_frames.clear()
        self._inner.grid_columnconfigure(1, weight=1)

        if not self._entries:
            tk.Label(
                self._inner,
                text="No tool prefixes found.",
                font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL,
            ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=12)
            return

        for idx, entry in enumerate(self._entries):
            is_active = (entry.game == self._active_game_name)
            row = tk.Frame(self._inner, bg=BG_PANEL)
            row.grid(row=idx, column=0, columnspan=3, sticky="ew", pady=1)
            row.grid_columnconfigure(1, weight=1)
            self._row_frames[entry.key] = row

            var = tk.BooleanVar(value=False)
            self._check_vars[entry.key] = var
            ctk.CTkCheckBox(
                row, text="", variable=var, width=24,
            ).grid(row=0, column=0, rowspan=2, padx=(8, 6), pady=4)

            # Line 1: tool — game (active)
            game_label = f"{entry.game}  (active)" if is_active else entry.game
            tk.Label(
                row, text=f"{entry.tool}  —  {game_label}", anchor="w",
                font=TK_FONT_NORMAL,
                fg=(TEXT_OK if is_active else TEXT_MAIN), bg=BG_PANEL,
            ).grid(row=0, column=1, sticky="ew", padx=(2, 8), pady=(4, 0))

            # Line 2: location · proton · path
            bits = [entry.location]
            if entry.proton:
                bits.append(entry.proton)
            try:
                bits.append(str(entry.path.parent))
            except Exception:
                pass
            tk.Label(
                row, text="  ·  ".join(bits), anchor="w",
                font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL,
                justify="left",
            ).grid(row=1, column=1, sticky="ew", padx=(2, 8), pady=(0, 4))

            size_lbl = ctk.CTkLabel(
                row, text="—", font=FONT_SMALL, text_color=TEXT_DIM, anchor="e",
                width=80,
            )
            size_lbl.grid(row=0, column=2, rowspan=2, sticky="e", padx=(0, 12), pady=4)
            self._size_labels[entry.key] = size_lbl

    # ---- size refresh ------------------------------------------------------

    def _refresh_sizes(self) -> None:
        entries = list(self._entries)

        def _worker():
            sizes: dict[str, int] = {}
            for e in entries:
                sizes[e.key] = _get_dir_size(e.path)
            try:
                self.after(0, lambda: self._apply_sizes(sizes))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_sizes(self, sizes: dict[str, int]) -> None:
        total = 0
        for key, sz in sizes.items():
            total += sz
            lbl = self._size_labels.get(key)
            if lbl is not None:
                try:
                    lbl.configure(text=_fmt_size(sz))
                except Exception:
                    pass
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

    def _selected_entries(self) -> list[PrefixEntry]:
        out = []
        for e in self._entries:
            var = self._check_vars.get(e.key)
            if var is not None and var.get():
                out.append(e)
        return out

    # ---- delete actions ----------------------------------------------------

    def _on_delete_selected(self) -> None:
        entries = self._selected_entries()
        if not entries:
            self._status_lbl.configure(text="Nothing selected.", fg=TEXT_DIM)
            return
        total = sum(_get_dir_size(e.path) for e in entries)

        listing = "\n".join(f"  • {e.tool} ({e.game})" for e in entries[:10])
        if len(entries) > 10:
            listing += f"\n  • …and {len(entries) - 10} more"

        alert = CTkAlert(
            state="warning",
            title=f"Delete {len(entries)} Prefix{'es' if len(entries) != 1 else ''}",
            body_text=(
                f"Delete {_fmt_size(total)} across {len(entries)} prefix(es)?\n\n"
                f"{listing}\n\n"
                "Each prefix is recreated automatically the next time its tool runs."
            ),
            btn1="Delete",
            btn2="Cancel",
            parent=self.winfo_toplevel(),
            height=320,
        )
        if alert.get() != "Delete":
            return
        self._run_delete(entries)

    def _on_delete_all(self) -> None:
        entries = list(self._entries)
        if not entries:
            self._status_lbl.configure(text="No prefixes to delete.", fg=TEXT_DIM)
            return
        total = sum(_get_dir_size(e.path) for e in entries)
        alert = CTkAlert(
            state="warning",
            title="Delete All Tool Prefixes",
            body_text=(
                f"Delete {_fmt_size(total)} across every tool prefix "
                f"({len(entries)} total)?\n\n"
                "Each prefix is recreated automatically the next time its tool runs."
            ),
            btn1="Delete",
            btn2="Cancel",
            parent=self.winfo_toplevel(),
            height=280,
        )
        if alert.get() != "Delete":
            return
        self._run_delete(entries)

    def _run_delete(self, entries: list[PrefixEntry]) -> None:
        self._del_sel_btn.configure(state="disabled")
        self._del_all_btn.configure(state="disabled")
        self._status_lbl.configure(text="Deleting…", fg=TEXT_DIM)

        targets = [e.path for e in entries]

        def _worker():
            deleted = 0
            errors: list[str] = []
            for target in targets:
                try:
                    # Guard: only remove prefix_* dirs or the known shared
                    # wine_prefixes/<tool> dirs — never anything else.
                    if not _is_deletable_prefix(target):
                        errors.append(f"{target.name}: refused (not a prefix dir)")
                        continue
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                        deleted += 1
                except OSError as exc:
                    errors.append(f"{target.name}: {exc}")
            try:
                self.after(0, lambda: self._on_delete_done(deleted, errors))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_delete_done(self, deleted: int, errors: list[str]) -> None:
        try:
            self._del_sel_btn.configure(state="normal")
            self._del_all_btn.configure(state="normal")
        except Exception:
            pass
        if errors:
            self._status_lbl.configure(
                text=f"Deleted {deleted}; {len(errors)} failed.", fg=TEXT_ERR)
        else:
            self._status_lbl.configure(
                text=f"Deleted {deleted} prefix{'es' if deleted != 1 else ''}.",
                fg=TEXT_OK)
        self._reload()

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


def _is_deletable_prefix(path: Path) -> bool:
    """True for prefix_* dirs, the shared_<Proton> wizard prefixes, or a known
    shared wine_prefixes/<tool> dir."""
    if path.name.startswith("prefix_"):
        return True
    try:
        return (
            path.parent == get_wine_prefixes_dir()
            and (path.name in _WINE_PREFIX_TOOLS or path.name.startswith("shared_"))
        )
    except Exception:
        return False

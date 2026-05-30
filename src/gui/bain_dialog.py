"""
bain_dialog.py
Simple BAIN sub-package picker as a full-cover CustomTkinter overlay. Unlike
the FOMOD wizard there are no steps/groups/conditions — just a checklist of
sub-packages to merge.
"""

from __future__ import annotations

import os
import tkinter as tk
import customtkinter as ctk
from typing import Optional

from Utils.bain_installer import BainSubPackage

from gui.theme import (
    BG_DEEP,
    BG_PANEL,
    BG_HEADER,
    BG_HOVER,
    BG_CARD,
    BG_GREEN_ROW,
    BG_RED_DEEP,
    BG_GREEN_TEXT,
    BG_RED_TEXT,
    TONE_GREEN,
    TONE_RED,
    ACCENT,
    ACCENT_HOV,
    TEXT_ON_ACCENT,
    TEXT_MAIN,
    TEXT_DIM,
    BORDER,
    FONT_NORMAL,
    FONT_BOLD,
    FONT_SMALL,
)
from gui.wheel_compat import LEGACY_WHEEL_REDUNDANT
from gui.tk_tooltip import TkTooltip


class BainDialog(ctk.CTkFrame):
    """Inline BAIN sub-package picker, placed as a full-cover overlay.

    on_done(result): result is None if cancelled, or {"selected": [name, ...]}.
    """

    def __init__(self, parent, subpackages: list[BainSubPackage],
                 mod_root: str,
                 readme_text: Optional[str] = None,
                 saved_selections: Optional[dict] = None,
                 selections_path=None,
                 on_done=None):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_done = on_done or (lambda r: None)
        self._subpackages = subpackages
        self._mod_root = mod_root
        self._readme_text = readme_text
        self._selections_path = selections_path
        self.result: Optional[dict] = None

        # Initial checkbox state: restore saved selection if present, else the
        # sub-package's own default (all selected for a fresh BAIN install).
        saved = None
        if saved_selections and isinstance(saved_selections.get("selected"), list):
            saved = set(saved_selections["selected"])
        self._vars: dict[str, tk.BooleanVar] = {}
        for pkg in subpackages:
            checked = (pkg.name in saved) if saved is not None else pkg.default_selected
            self._vars[pkg.name] = tk.BooleanVar(value=checked)

        # Per-package set of relative file keys (case-insensitive, matching how
        # conflicts resolve on a case-insensitive game install) so we can show
        # which selected packages win/lose shared files live.
        self._pkg_files: dict[str, set[str]] = {
            pkg.name: self._scan_pkg_files(pkg.path) for pkg in subpackages
        }
        # Card widgets to recolour on conflict recompute, keyed by package name.
        self._cards: dict[str, dict] = {}
        # Shared hover tooltip for the per-card promote buttons.
        self._tooltip = TkTooltip(self, fg=TEXT_MAIN)

        self._build_ui()
        self._recompute_conflicts()

    @staticmethod
    def _scan_pkg_files(root: str) -> set[str]:
        """Return the set of file paths under *root*, relative and lower-cased."""
        out: set[str] = set()
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                rel = os.path.relpath(os.path.join(dirpath, fn), root)
                out.add(rel.replace("\\", "/").lower())
        return out

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self.grid_columnconfigure(0, weight=1)

        self._options_scroll: Optional[ctk.CTkScrollableFrame] = None
        self._scroll_canvas = None
        self._build_title_bar()
        self._build_content_area()
        self._build_button_bar()
        self._setup_scroll_binding()

    def _build_title_bar(self):
        bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            bar, text="BAIN package — choose sub-packages to install",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w"
        ).grid(row=0, column=0, sticky="w", padx=12, pady=8)

    def _build_content_area(self):
        content = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_rowconfigure(0, weight=1)

        has_readme = bool(self._readme_text and self._readme_text.strip())
        if has_readme:
            # Left: readme text; right: sub-package checklist.
            content.grid_columnconfigure(0, weight=1, minsize=320)
            content.grid_columnconfigure(1, weight=0, minsize=1)
            content.grid_columnconfigure(2, weight=1)

            left = ctk.CTkFrame(content, fg_color=BG_PANEL, corner_radius=0)
            left.grid(row=0, column=0, sticky="nsew")
            left.grid_rowconfigure(1, weight=1)
            left.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(left, text="Package readme", font=FONT_SMALL,
                         text_color=TEXT_DIM, anchor="w").grid(
                row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
            box = ctk.CTkTextbox(left, fg_color=BG_DEEP, text_color=TEXT_MAIN,
                                 font=FONT_NORMAL, wrap="word", corner_radius=0)
            box.grid(row=1, column=0, sticky="nsew", padx=4, pady=4)
            box.insert("1.0", self._readme_text)
            box.configure(state="disabled")

            ctk.CTkFrame(content, fg_color=BORDER, width=1, corner_radius=0).grid(
                row=0, column=1, sticky="ns")
            list_col = 2
        else:
            content.grid_columnconfigure(0, weight=1)
            list_col = 0

        self._build_checklist(content, list_col)

    def _build_checklist(self, parent, column: int):
        scroll = ctk.CTkScrollableFrame(
            parent, fg_color=BG_DEEP, corner_radius=0,
            scrollbar_button_color=BG_PANEL,
            scrollbar_button_hover_color=ACCENT)
        scroll.grid(row=0, column=column, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        self._options_scroll = scroll
        self._scroll_canvas = scroll._parent_canvas

        # Header so it's obvious these rows are selectable sub-packages, and the
        # green/red conflict colouring isn't cryptic.
        hdr = ctk.CTkLabel(
            scroll,
            text=f"Sub-packages ({len(self._subpackages)}) — tick to install · "
                 "green = files used · red = fully overridden by a later package",
            font=FONT_SMALL, text_color=TEXT_DIM, anchor="w")
        hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))

        for i, pkg in enumerate(self._subpackages):
            # Each sub-package is rendered as a distinct bordered card with an
            # accent left-edge bar so it reads as a discrete package, not a flat
            # checklist line.
            # CTkFrame defaults to height=200 and ignores grid propagation
            # inside a scrollable frame, so pin an explicit content-sized height
            # with propagation off for a deterministic compact card.
            card = ctk.CTkFrame(
                scroll, fg_color=BG_CARD, corner_radius=6, height=52,
                border_width=1, border_color=BORDER)
            card.grid(row=i + 1, column=0, sticky="ew", padx=8, pady=3)
            card.grid_columnconfigure(2, weight=1)
            card.grid_rowconfigure((0, 1), weight=1)
            card.grid_propagate(False)

            # Accent bar (left edge) — visually anchors each card. Spans both
            # text rows.
            bar = ctk.CTkFrame(card, fg_color=ACCENT, width=4, corner_radius=0)
            bar.grid(row=0, column=0, rowspan=2, sticky="ns")

            cb = ctk.CTkCheckBox(
                card, text="", width=24, variable=self._vars[pkg.name],
                fg_color=ACCENT, hover_color=ACCENT_HOV,
                checkmark_color=TEXT_ON_ACCENT, border_color=BORDER,
                command=self._recompute_conflicts)
            cb.grid(row=0, column=1, rowspan=2, sticky="w", padx=10)

            # Name + raw folder name gridded directly into the card (no nested
            # frame, which would carry its own height=200 default).
            label = ctk.CTkLabel(
                card, text=pkg.display_name, font=FONT_BOLD,
                text_color=TEXT_MAIN, anchor="w", cursor="hand2")
            label.grid(row=0, column=2, sticky="sw", padx=(0, 8))

            sub = ctk.CTkLabel(
                card, text=pkg.name, font=FONT_SMALL, text_color=TEXT_DIM,
                anchor="w", cursor="hand2")
            sub.grid(row=1, column=2, sticky="nw", padx=(0, 8))

            for _w in (label, sub):
                _w.bind("<Button-1>",
                        lambda _e, v=self._vars[pkg.name]: (
                            v.set(not v.get()), self._recompute_conflicts()))

            # "Promote to winner" button on the right edge. Only shown on red
            # (fully-overridden) cards; unchecks the later packages overriding
            # this one so it wins its files. Hidden via grid_remove() by default.
            promote = ctk.CTkButton(
                card, text="⬆", width=30, height=30, font=FONT_BOLD,
                fg_color=TONE_GREEN, hover_color=BG_GREEN_ROW,
                text_color=TEXT_ON_ACCENT,
                command=lambda n=pkg.name: self._promote_package(n))
            promote.grid(row=0, column=3, rowspan=2, sticky="e", padx=(4, 8))
            promote.grid_remove()
            self._tooltip.attach(
                promote,
                "Use this package — turn off the later packages overriding "
                "its files")

            self._cards[pkg.name] = {
                "card": card, "bar": bar, "label": label, "sub": sub,
                "promote": promote}

        # Spacer row absorbs any slack so cards keep their natural height
        # instead of stretching when there are only a few sub-packages.
        spacer_row = len(self._subpackages) + 1
        scroll.grid_rowconfigure(spacer_row, weight=1)
        ctk.CTkFrame(scroll, fg_color="transparent", height=1).grid(
            row=spacer_row, column=0, sticky="nsew")

    def _build_button_bar(self):
        bar = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=50)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        ctk.CTkFrame(bar, fg_color=BORDER, height=1, corner_radius=0).pack(
            side="top", fill="x")

        ctk.CTkButton(
            bar, text="Cancel", width=100, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER,
            text_color=TEXT_MAIN, command=self._on_cancel
        ).pack(side="right", padx=(4, 12), pady=10)

        ctk.CTkButton(
            bar, text="Install", width=100, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color=TEXT_ON_ACCENT, command=self._on_install
        ).pack(side="right", padx=4, pady=10)

        ctk.CTkButton(
            bar, text="Select All", width=100, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_DIM,
            command=lambda: self._set_all(True)
        ).pack(side="left", padx=(12, 4), pady=10)

        ctk.CTkButton(
            bar, text="Select None", width=100, height=30, font=FONT_NORMAL,
            fg_color=BG_HEADER, hover_color=BG_HOVER, text_color=TEXT_DIM,
            command=lambda: self._set_all(False)
        ).pack(side="left", padx=4, pady=10)

    def _setup_scroll_binding(self):
        """Bind scroll globally so the checklist scrolls regardless of which
        widget the pointer is over. Matches the FOMOD wizard's sensitivity."""
        canvas = self._scroll_canvas
        if canvas is None:
            return

        def _on_scroll(event):
            try:
                if self._options_scroll is None or not self._options_scroll.winfo_exists():
                    return
                sx = self._options_scroll.winfo_rootx()
                sy = self._options_scroll.winfo_rooty()
                sw = self._options_scroll.winfo_width()
                sh = self._options_scroll.winfo_height()
            except Exception:
                return
            if sx <= event.x_root < sx + sw and sy <= event.y_root < sy + sh:
                num = getattr(event, "num", None)
                delta = getattr(event, "delta", 0) or 0
                if num == 4 or delta > 0:
                    direction = -3
                elif num == 5 or delta < 0:
                    direction = 3
                else:
                    return
                canvas.yview("scroll", direction, "units")

        # On Tk >= 8.7 CTkScrollableFrame already handles <MouseWheel> via its
        # own bind_all — we only need to supplement Button-4/5 for Tk 8.6.
        root = self.winfo_toplevel()
        if not LEGACY_WHEEL_REDUNDANT:
            root.bind_all("<Button-4>", _on_scroll, add="+")
            root.bind_all("<Button-5>", _on_scroll, add="+")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _set_all(self, value: bool):
        for v in self._vars.values():
            v.set(value)
        self._recompute_conflicts()

    # ------------------------------------------------------------------
    # Conflict highlighting
    # ------------------------------------------------------------------

    def _recompute_conflicts(self):
        """Recolour each card based on the live selection.

        Among the checked packages (in install order — alphabetical by folder
        name, matching ``resolve_bain_files``), a later package overrides an
        earlier one for any shared file. A checked package is:
          • green  — it wins (contributes) at least one file, or has no files;
          • red    — every one of its files is also provided by a later checked
                     package, so it is fully overridden and installs nothing.
        Unchecked packages stay neutral.
        """
        # Install order = order of self._subpackages (already name-sorted).
        selected_order = [p.name for p in self._subpackages
                          if self._vars[p.name].get()]

        # winners[key] = name of the LAST selected package providing that file.
        winners: dict[str, str] = {}
        for name in selected_order:
            for key in self._pkg_files.get(name, ()):
                winners[key] = name

        for pkg in self._subpackages:
            name = pkg.name
            widgets = self._cards.get(name)
            if not widgets:
                continue
            checked = self._vars[name].get()
            if not checked:
                state = "neutral"
            else:
                files = self._pkg_files.get(name, set())
                if not files:
                    state = "win"  # nothing to override; treat as contributing
                elif any(winners.get(k) == name for k in files):
                    state = "win"
                else:
                    state = "lose"
            self._apply_card_state(widgets, state)
            # Show the promote button only on fully-overridden cards.
            if state == "lose":
                widgets["promote"].grid()
            else:
                widgets["promote"].grid_remove()

    def _overriders_of(self, name: str) -> list[str]:
        """Return the checked packages installed AFTER *name* that provide any
        of *name*'s files (i.e. the ones overriding it)."""
        files = self._pkg_files.get(name, set())
        if not files:
            return []
        names = [p.name for p in self._subpackages]
        try:
            idx = names.index(name)
        except ValueError:
            return []
        out = []
        for later in names[idx + 1:]:
            if self._vars[later].get() and (self._pkg_files.get(later, set()) & files):
                out.append(later)
        return out

    def _promote_package(self, name: str):
        """Uncheck the later packages overriding *name* so it wins its files."""
        for overrider in self._overriders_of(name):
            self._vars[overrider].set(False)
        self._recompute_conflicts()

    @staticmethod
    def _apply_card_state(widgets: dict, state: str):
        if state == "win":
            card_bg, bar_col, name_col = BG_GREEN_ROW, TONE_GREEN, BG_GREEN_TEXT
            sub_col = BG_GREEN_TEXT
        elif state == "lose":
            card_bg, bar_col, name_col = BG_RED_DEEP, TONE_RED, BG_RED_TEXT
            sub_col = BG_RED_TEXT
        else:  # neutral
            card_bg, bar_col, name_col, sub_col = BG_CARD, ACCENT, TEXT_MAIN, TEXT_DIM
        widgets["card"].configure(fg_color=card_bg)
        widgets["bar"].configure(fg_color=bar_col)
        widgets["label"].configure(text_color=name_col)
        widgets["sub"].configure(text_color=sub_col)

    def _on_install(self):
        selected = [name for name, v in self._vars.items() if v.get()]
        self.result = {"selected": selected}
        self._finish(self.result)

    def _on_cancel(self):
        self.result = None
        self._finish(None)

    def _finish(self, result):
        cb = self._on_done
        try:
            self.destroy()
        finally:
            cb(result)

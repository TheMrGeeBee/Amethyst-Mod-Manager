"""
Archive Unpack overlay — lists each plugin in the selected mod folder
together with every sibling archive (BSA / BA2 / ` - Main` / ` - Textures`)
that auto-loads with it.  Selecting a plugin row unpacks all of its
sibling archives in one go.

A mod typically has at most one plugin, but some ship two (e.g. a master
plus a patch).  Archives without a matching plugin stem are surfaced in a
separate "(no matching plugin)" group so they're still reachable.

Place over the plugin panel via place(relx=0, rely=0, relwidth=1, relheight=1).

The overlay is a dumb UI; the caller passes ``on_unpack(paths)`` and
``on_close()`` callbacks.  The caller is responsible for closing the
overlay.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from typing import Callable

import customtkinter as ctk

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    BORDER,
    FONT_BOLD,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_ON_ACCENT,
    TK_FONT_BOLD,
    TK_FONT_NORMAL,
    TK_FONT_SMALL,
)
from Utils.bsa_reader import read_bsa_file_list


class BsaUnpackOverlay(tk.Frame):
    """Single-mod plugin / archive picker.

    Shows one row per plugin in the mod folder, listing every sibling
    archive (``Plugin.bsa``, ``Plugin - Main.ba2``, ``Plugin - Textures.ba2``)
    that auto-loads with it.  Clicking Unpack invokes
    ``on_unpack(list_of_archive_paths)`` and dismisses the overlay; the
    caller does the actual work.

    Archives whose stem doesn't match any plugin get their own
    "(no matching plugin)" group so they're still reachable.
    """

    def __init__(
        self,
        parent: tk.Widget,
        *,
        mod_name: str,
        mod_dir: Path,
        on_unpack: Callable[[list[Path]], None],
        on_close: Callable[[], None],
    ):
        super().__init__(parent, bg=BG_DEEP)
        self._mod_name = mod_name
        self._mod_dir = Path(mod_dir)
        self._on_unpack = on_unpack
        self._on_close = on_close
        # Filled in by _add_row; consumed by _on_resize to keep label
        # wraplengths in sync with the overlay width.
        self._row_labels: list[tuple[tk.Label, tk.Label]] = []
        self._title_label: tk.Label | None = None
        self._hint_label: tk.Label | None = None
        self._build()

    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # Toolbar — grid layout so the close button always wins horizontal
        # space.  No fixed height: when the title wraps to multiple lines
        # at narrow widths, the toolbar grows vertically to fit instead
        # of clipping.  minsize keeps it from collapsing on a single
        # short title.
        toolbar = tk.Frame(self, bg=BG_HEADER)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_columnconfigure(0, weight=1)
        toolbar.grid_columnconfigure(1, weight=0)
        toolbar.grid_rowconfigure(0, minsize=42)

        # Pick a title kind based on what's in the mod folder.  If the
        # mod has both .bsa and .ba2 (rare), fall back to "Archive".
        groups = self._collect_groups()
        all_archives = [p for g in groups for p in g["archives"]]
        suffixes = {p.suffix.lower() for p in all_archives}
        if suffixes == {".ba2"}:
            kind_label = "BA2"
        elif suffixes == {".bsa"}:
            kind_label = "BSA"
        else:
            kind_label = "Archive"

        self._title_label = tk.Label(
            toolbar, text=f"Unpack {kind_label} — {self._mod_name}",
            font=TK_FONT_BOLD, fg=TEXT_MAIN, bg=BG_HEADER,
            anchor="w", justify="left",
        )
        self._title_label.grid(row=0, column=0, sticky="ew", padx=12, pady=8)

        ctk.CTkButton(
            toolbar, text="✕ Close", width=85, height=30,
            fg_color="#6b3333", hover_color="#8c4444", text_color="white",
            font=FONT_BOLD, command=self._on_close,
        ).grid(row=0, column=1, sticky="ne", padx=(6, 12), pady=5)

        # Archive list body
        body = tk.Frame(self, bg=BG_DEEP)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=12)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # Scrollable container so a mod with many archives (rare but
        # real for texture-pack splits) doesn't get clipped.
        canvas = tk.Canvas(body, bg=BG_DEEP, highlightthickness=0, bd=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb = tk.Scrollbar(
            body, orient="vertical", command=canvas.yview,
            bg="#383838", troughcolor=BG_DEEP, highlightthickness=0, bd=0,
        )
        vsb.grid(row=0, column=1, sticky="ns")
        canvas.configure(yscrollcommand=vsb.set)

        inner = tk.Frame(canvas, bg=BG_DEEP)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfigure(inner_id, width=e.width),
        )

        if not groups:
            tk.Label(
                inner,
                text="No archive files in this mod folder.",
                font=TK_FONT_NORMAL, fg=TEXT_DIM, bg=BG_DEEP,
            ).pack(anchor="w", padx=12, pady=12)
        else:
            for group in groups:
                self._add_group_row(inner, group)

        # Hint line — wraplength tracks the overlay width so it never
        # pushes content off-screen at narrow window sizes.
        self._hint_label = tk.Label(
            self,
            text=(
                "Unpacking will extract every archive listed under the "
                "selected plugin into this mod's folder, delete those "
                "archives, remove the plugin if it was a generated stub, "
                "and re-enable the unpacked files in the Mod Files tab."
            ),
            font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_DEEP, anchor="w",
            justify="left",
        )
        self._hint_label.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))

        # Update wraplengths whenever the overlay resizes.  Pad allowance
        # of 32px accounts for grid padx on each side (12+12) plus a
        # safety margin for the scrollbar gutter.
        self.bind("<Configure>", self._on_resize)

        self.bind("<Escape>", lambda e: self._on_close())
        self.focus_set()

    # ------------------------------------------------------------------

    # Suffixes that a same-stem archive can carry while still being
    # auto-loaded by a plugin.  Stripping these from an archive's stem
    # gives the "logical" plugin stem.  E.g. ``Foo - Main.ba2`` and
    # ``Foo - Textures.ba2`` both belong to plugin ``Foo``.
    _ARCHIVE_SIDECAR_SUFFIXES = (" - Main", " - Textures")

    @classmethod
    def _archive_plugin_stem(cls, archive_name: str) -> str:
        """Map an archive filename to its companion plugin's stem.

        ``Foo - Main.ba2`` → ``foo``
        ``Foo - Textures.ba2`` → ``foo``
        ``Foo.bsa`` → ``foo``
        Comparison is case-insensitive (Bethesda paths conventionally are).
        """
        stem = Path(archive_name).stem
        for suffix in cls._ARCHIVE_SIDECAR_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        return stem.lower()

    def _collect_groups(self) -> list[dict]:
        """Walk the mod folder and group archives by plugin stem.

        Returns a list of group dicts, one per plugin (or one for the
        orphan-archives bucket), sorted alphabetically.  Each group:

            {
                "label":       str,        # plugin filename or fallback
                "is_orphan":   bool,       # True for the "no matching plugin" group
                "archives":    list[Path], # all matching archives
                "total_bytes": int,
                "total_files": int,        # -1 if any archive failed to parse
            }
        """
        archives_by_stem: dict[str, list[Path]] = {}
        plugin_by_stem: dict[str, Path] = {}
        try:
            entries = list(self._mod_dir.iterdir())
        except OSError:
            return []

        for p in entries:
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext in (".bsa", ".ba2"):
                stem = self._archive_plugin_stem(p.name)
                archives_by_stem.setdefault(stem, []).append(p)
            elif ext in (".esp", ".esm", ".esl"):
                # Last writer wins if a mod ships both Foo.esp and
                # Foo.esm (rare); pick whichever sorts first so the
                # behaviour is deterministic.
                key = p.stem.lower()
                if key not in plugin_by_stem or p.name < plugin_by_stem[key].name:
                    plugin_by_stem[key] = p

        # Build groups: every plugin first, then orphans.
        groups: list[dict] = []
        seen_stems: set[str] = set()
        for key in sorted(plugin_by_stem.keys()):
            plugin = plugin_by_stem[key]
            archives = sorted(
                archives_by_stem.get(key, []),
                key=lambda p: p.name.lower(),
            )
            seen_stems.add(key)
            if not archives:
                continue  # plugin without any archive — nothing to unpack
            groups.append(self._build_group(plugin.name, archives, is_orphan=False))

        # Orphan archives — surface them so the user can still unpack
        # an archive whose plugin lives elsewhere or has been deleted.
        orphan_archives: list[Path] = []
        for stem, archs in archives_by_stem.items():
            if stem in seen_stems:
                continue
            orphan_archives.extend(archs)
        if orphan_archives:
            orphan_archives.sort(key=lambda p: p.name.lower())
            groups.append(self._build_group(
                "(no matching plugin)", orphan_archives, is_orphan=True,
            ))

        return groups

    def _build_group(
        self, label: str, archives: list[Path], *, is_orphan: bool,
    ) -> dict:
        total_bytes = 0
        total_files = 0
        any_unreadable = False
        for p in archives:
            try:
                total_bytes += p.stat().st_size
            except OSError:
                any_unreadable = True
            try:
                total_files += len(read_bsa_file_list(p))
            except Exception:
                any_unreadable = True
        return {
            "label": label,
            "is_orphan": is_orphan,
            "archives": archives,
            "total_bytes": total_bytes,
            "total_files": -1 if any_unreadable else total_files,
        }

    # ------------------------------------------------------------------

    def _add_group_row(self, parent: tk.Widget, group: dict) -> None:
        """Render one plugin row, listing its sibling archives, total
        size and file count.  Unpacking the row processes every archive
        in ``group["archives"]`` as a single operation."""
        row = tk.Frame(parent, bg=BG_PANEL, highlightthickness=1,
                       highlightbackground=BORDER)
        row.pack(fill="x", padx=2, pady=4)
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=0, minsize=130)

        info = tk.Frame(row, bg=BG_PANEL)
        info.grid(row=0, column=0, sticky="ew", padx=10, pady=8)
        info.grid_columnconfigure(0, weight=1)

        # Plugin name (or "(no matching plugin)") on the top line.
        name_lbl = tk.Label(
            info, text=group["label"],
            font=TK_FONT_BOLD,
            fg=TEXT_DIM if group["is_orphan"] else TEXT_MAIN,
            bg=BG_PANEL,
            anchor="w", justify="left",
        )
        name_lbl.grid(row=0, column=0, sticky="ew")

        # Each sibling archive on its own dim sub-line so users can see
        # exactly which files will be unpacked.
        archive_labels: list[tk.Label] = []
        for archive in group["archives"]:
            arch_lbl = tk.Label(
                info, text=f"  • {archive.name}",
                font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL,
                anchor="w", justify="left",
            )
            arch_lbl.grid(sticky="ew")
            archive_labels.append(arch_lbl)

        # Totals on the bottom line.
        size_mb = group["total_bytes"] / (1024 * 1024)
        if group["total_files"] >= 0:
            sub = f"{group['total_files']} file(s) — {size_mb:.1f} MiB"
        else:
            sub = f"unreadable — {size_mb:.1f} MiB"
        sub_lbl = tk.Label(
            info, text=sub,
            font=TK_FONT_SMALL, fg=TEXT_DIM, bg=BG_PANEL,
            anchor="w", justify="left",
        )
        sub_lbl.grid(sticky="ew")

        # Track the wrapping labels (everything in the info column) so
        # _on_resize can update their wraplengths.
        self._row_labels.append((name_lbl, sub_lbl))
        for arch_lbl in archive_labels:
            self._row_labels.append((arch_lbl, arch_lbl))  # same width

        archives = list(group["archives"])
        ctk.CTkButton(
            row, text="Unpack", width=110, height=32,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color=TEXT_ON_ACCENT,
            command=lambda paths=archives: self._handle_unpack(paths),
            state="normal" if archives else "disabled",
        ).grid(row=0, column=1, sticky="e", padx=10, pady=8)

    def _handle_unpack(self, paths: list[Path]) -> None:
        # Hand off to the caller — the overlay's job ends here.  Caller
        # is expected to close us via on_close once it has a progress
        # popup in flight.
        self._on_unpack(paths)

    # ------------------------------------------------------------------

    def _on_resize(self, event: tk.Event) -> None:
        """Keep label wraplengths in sync with the overlay width so text
        never pushes the Unpack / Close buttons off the right edge.

        Filtering on ``event.widget is self`` avoids reacting to every
        descendant <Configure> event."""
        if event.widget is not self:
            return

        overlay_w = max(event.width, 1)

        # Hint line — full width minus outer padding.
        if self._hint_label is not None:
            self._hint_label.configure(wraplength=max(overlay_w - 32, 100))

        # Title — toolbar minus the close button column (~110px) and padx.
        if self._title_label is not None:
            self._title_label.configure(
                wraplength=max(overlay_w - 130, 80),
            )

        # Per-row info labels — overlay minus button column (130 minsize),
        # row internal padding (~30), and outer body padx (~24) and the
        # scrollbar gutter (~16).  Floor at 100px so labels don't collapse.
        row_wrap = max(overlay_w - 200, 100)
        for name_lbl, sub_lbl in self._row_labels:
            name_lbl.configure(wraplength=row_wrap)
            sub_lbl.configure(wraplength=row_wrap)

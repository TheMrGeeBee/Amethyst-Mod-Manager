"""
plugin_audit.py — Plugin Audit & Cleanup wizard.

Scans load order for safe-to-disable plugins and removes orphaned
SkyGen BOS/SkyPatcher INIs for plugins that must stay enabled.

Safety rules:
1. A plugin is safe only if ALL dependents are also safe (transitive).
2. Higher Amethyst priority wins conflicts.
3. "Disable Selected" writes to plugins.txt directly.
"""

from __future__ import annotations

import mmap
import re
import threading
import tkinter.messagebox as tkmb
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

import customtkinter as ctk

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT,
    ACCENT_HOV,
    BG_DEEP,
    BG_HEADER,
    BG_PANEL,
    FONT_BOLD,
    FONT_NORMAL,
    TEXT_DIM,
    TEXT_MAIN,
    TEXT_ON_ACCENT,
)
# -----------------------------------------------
# Constants & helpers
# -----------------------------------------------

BASE_GAME_PLUGINS = {
    "Skyrim.esm", "Update.esm", "Dawnguard.esm", "HearthFires.esm",
    "Dragonborn.esm", "ccBGSSSE001-Fish.esm",
}

# Patch-type labels and colours
_PATCH_LABELS = {
    "PRE_PATCHED_BOS":       ("Base Object Swapper", "#5ba8e0"),
    "PRE_PATCHED_SP":        ("SkyPatcher",          "#e0a85b"),
    "PRE_PATCHED_SPID":      ("SPID",                "#e0a85b"),
    "PRE_PATCHED_KID":       ("KID",                 "#e0a85b"),
    "PRE_PATCHED_DSD":       ("DSD",                 "#e0a85b"),
    "PRE_PATCHED_OAR":       ("OAR",                 "#e0a85b"),
    "PRE_PATCHED_SYNTHESIS": ("Synthesis",            "#e0a85b"),
    "PRE_PATCHED_BASHED":    ("Bashed Patch",         "#8b5cf6"),
}

SAFE_COLOR   = "#6bc76b"
UNSAFE_COLOR = "#e06c6c"
WARN_COLOR   = "#e0c45b"

# Helpers

def _read_active_profile(game: "BaseGame") -> str:
    active_dir = getattr(game, "_active_profile_dir", None)
    if active_dir is not None:
        name = Path(active_dir).name
        if name:
            return name
    try:
        last = game.get_last_active_profile()
        if last and last != "default":
            candidate = game.get_profile_root() / "profiles" / last
            if candidate.is_dir():
                return last
    except Exception:
        pass
    try:
        profiles_root = game.get_profile_root() / "profiles"
        if profiles_root.is_dir():
            candidates = sorted(
                d for d in profiles_root.iterdir()
                if d.is_dir() and d.name != "default" and (d / "loadorder.txt").is_file()
            )
            if candidates:
                return candidates[0].name
    except Exception:
        pass
    return "default"


def _profile_dir(game: "BaseGame", profile: str) -> Path:
    return game.get_profile_root() / "profiles" / profile


def _read_loadorder(game: "BaseGame", profile: str) -> List[str]:
    pdir = _profile_dir(game, profile)
    lo_path = pdir / "loadorder.txt"
    pl_path = pdir / "plugins.txt"

    full_order: List[str] = []
    if lo_path.is_file():
        for line in lo_path.read_text(encoding="utf-8").splitlines():
            line = line.split("#")[0].strip()
            if line:
                full_order.append(line)

    active_set: Set[str] = set()
    if pl_path.is_file():
        for line in pl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("*"):
                active_set.add(line[1:].strip())
            elif not line.startswith("#") and line:
                active_set.add(line)

    if not full_order:
        return list(active_set)
    return [p for p in full_order if p in BASE_GAME_PLUGINS or p in active_set]


def _read_modlist_priorities(game: "BaseGame", profile: str) -> Dict[str, int]:
    """{mod_name: priority}. Higher = wins conflicts. modlist.txt line 0 = highest."""
    pdir = _profile_dir(game, profile)
    ml_path = pdir / "modlist.txt"
    if not ml_path.is_file():
        # Fall back to shared modlist
        ml_path = game.get_profile_root() / "profiles" / profile / "modlist.txt"
    if not ml_path.is_file():
        return {}

    real_mods: List[str] = []
    for line in ml_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line[0] not in ("+", "-", "*"):
            continue
        name = line[1:]
        if name.endswith("_separator"):
            continue
        real_mods.append(name)

    total = len(real_mods)
    return {name: (total - 1 - i) for i, name in enumerate(real_mods)}


_plugin_file_cache: Dict[str, Optional[Path]] = {}

def _find_plugin_file(plugin_name: str, mods_path: Path,
                      game: "BaseGame") -> Optional[Path]:
    """Locate plugin file on disk. Result cached per run."""
    key = f"{mods_path}|{plugin_name}"
    if key in _plugin_file_cache:
        return _plugin_file_cache[key]
    result: Optional[Path] = None
    # 1. Inside staging mods
    if mods_path and mods_path.is_dir():
        for mod_dir in mods_path.iterdir():
            if not mod_dir.is_dir():
                continue
            for candidate in (mod_dir / plugin_name, mod_dir / "Data" / plugin_name):
                if candidate.is_file():
                    result = candidate
                    break
            if result:
                break
    # 2. Vanilla data dir
    if result is None:
        try:
            data_path = game.get_game_path() / "Data" / plugin_name
            if data_path.is_file():
                result = data_path
        except Exception:
            pass
    _plugin_file_cache[key] = result
    return result


def _find_mod_folder(plugin_path: Path, mods_path: Path) -> Optional[Path]:
    try:
        rel = plugin_path.relative_to(mods_path)
        return mods_path / rel.parts[0]
    except ValueError:
        return None


def _parse_header(path: Path) -> Tuple[str, List[str]]:
    try:
        with open(path, "rb") as f:
            data = f.read(8192)
        if len(data) < 24 or data[:4] != b"TES4":
            return "Unknown", []
        tes4_size = int.from_bytes(data[4:8], "little")
        offset, end = 24, min(24 + tes4_size, len(data))
        author, masters = "Unknown", []
        while offset < end - 6:
            st = data[offset:offset + 4]
            ss = int.from_bytes(data[offset + 4:offset + 6], "little")
            offset += 6
            if offset + ss > len(data):
                break
            chunk = data[offset:offset + ss]
            if chunk and chunk[-1] == 0:
                chunk = chunk[:-1]
            text = chunk.decode("utf-8", errors="ignore").strip()
            if st == b"CNAM":
                author = text or "Unknown"
            elif st == b"MAST" and text:
                masters.append(text)
            offset += ss
        return author, masters
    except Exception:
        return "Unknown", []


def _plugin_has_new_records(path: Path) -> bool:
    """True if plugin adds FormIDs not owned by any master → cannot disable even with patch."""
    try:
        with open(path, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                return _scan_data_for_new_records(data)
    except OSError:
        return True


def _scan_data_for_new_records(data: bytes) -> bool:
    """Core scan — accepts bytes, mmap, or any buffer object."""

    if len(data) < 28 or data[:4] != b"TES4":
        return True

    tes4_size = int.from_bytes(data[4:8], "little", signed=False)
    if tes4_size < 4:
        return True

    # ESL flag (bit 11) at offset 8–11
    flags = int.from_bytes(data[8:12], "little", signed=False)
    is_esl = bool(flags & 0x00000800)

    # Count masters from MAST subrecords
    num_masters = 0
    offset = 24
    tes4_end = min(24 + tes4_size, len(data))
    while offset <= tes4_end - 6:
        st = data[offset:offset + 4]
        ss = int.from_bytes(data[offset + 4:offset + 6], "little", signed=False)
        offset += 6
        if offset + ss > len(data):
            break
        if st == b"MAST":
            num_masters += 1
        offset += ss

    master_set: Set[int] = set(range(num_masters))

    # Recursively scan GRUP tree for record FormIDs
    def _scan(pos: int, end: int) -> int:
        count = 0
        while pos < end - 20:
            if data[pos:pos + 4] == b"GRUP":
                gs = int.from_bytes(data[pos + 4:pos + 8], "little", signed=False)
                if gs < 20 or pos + gs > end:
                    pos += 1
                    continue
                count += _scan(pos + 20, pos + gs)
                pos += gs
                continue

            sig = data[pos:pos + 4]
            if not all(65 <= b <= 90 for b in sig):
                pos += 1
                continue
            if sig == b"TES4":
                ds = int.from_bytes(data[pos + 4:pos + 8], "little", signed=False)
                pos += 24 + ds
                continue

            ds = int.from_bytes(data[pos + 4:pos + 8], "little", signed=False)
            if ds > 100_000_000:
                pos += 1
                continue

            formid = int.from_bytes(data[pos + 12:pos + 16], "little", signed=False)
            src = (formid >> 24) & 0xFF

            if is_esl:
            # ESL-compacted new records have FormID < 0x1000.
            # Skyrim.esm overrides are >> 0x1000, so this distinguishes them.
                if src == 0x00 and 0 < formid < 0x1000:
                    count += 1
                elif src not in master_set:
                    count += 1
            else:
                if src not in master_set:
                    count += 1

            pos += 24 + ds
            while pos & 3:
                pos += 1

        return count

    new_count = _scan(24 + tes4_size, len(data))
    return new_count > 0


def _build_patch_index(mods_path: Path) -> Tuple[Set[str], Set[str]]:
    """Return (bos_patched, sp_patched) — lower-case plugin name sets."""
    bos_patched: Set[str] = set()
    sp_patched:  Set[str] = set()
    if not mods_path or not mods_path.is_dir():
        return bos_patched, sp_patched
    _for_re = re.compile(r"for\s+([^\s\r\n]+\.es[pml])", re.I)

    try:
        for mod_dir in mods_path.iterdir():
            if not mod_dir.is_dir():
                continue
            bos_dir = mod_dir / "SKSE" / "Plugins" / "Data" / "Base Object Swapper"
            if bos_dir.is_dir():
                for ini in bos_dir.rglob("*.ini"):
                    try:
                        header = ini.read_text(encoding="utf-8", errors="ignore")[:512]
                    except OSError:
                        continue
                    m = _for_re.search(header)
                    if m:
                        bos_patched.add(m.group(1).lower())
                    else:
                        stem = ini.stem.lower()
                        for suffix in ("_skygen_swap", "_swap"):
                            if stem.endswith(suffix):
                                stem = stem[:-len(suffix)]
                                break
                        for ext in (".esp", ".esm", ".esl"):
                            bos_patched.add(stem + ext)
            try:
                for ini in mod_dir.rglob("*_swap.ini"):
                    stem = ini.stem.lower()
                    if stem.endswith("_swap"):
                        stem = stem[:-5]
                    for ext in (".esp", ".esm", ".esl"):
                        bos_patched.add(stem + ext)
            except OSError:
                pass
            for sp_name in ("SkyPatcher", "SkyPatcher2"):
                sp_dir = mod_dir / "SKSE" / "Plugins" / sp_name
                if sp_dir.is_dir():
                    for ini in sp_dir.rglob("*.ini"):
                        try:
                            header = ini.read_text(encoding="utf-8", errors="ignore")[:512]
                        except OSError:
                            continue
                        m = _for_re.search(header)
                        if m:
                            sp_patched.add(m.group(1).lower())
                        else:
                            stem = ini.stem.lower()
                            stem = re.sub(r"^[0-9a-f]+-", "", stem)
                            if stem.endswith("_skygen"):
                                stem = stem[:-7]
                            for ext in (".esp", ".esm", ".esl"):
                                sp_patched.add(stem + ext)
                    break
            # SPID / KID / DSD — scan relevant extensions only
            try:
                for pattern in ("*.ini", "*.json", "*.yaml", "*.yml"):
                    for f in mod_dir.rglob(pattern):
                        if not f.is_file():
                            continue
                        nl = f.name.lower()
                        stem = f.stem.lower()
                        base_stem = stem
                        for suffix in ("_distr", "_kid", "_dsd"):
                            if stem.endswith(suffix):
                                base_stem = stem[:-len(suffix)]
                                break
                        if nl.endswith("_distr.ini"):
                            sp_patched.add(base_stem + ".esp")
                            sp_patched.add(base_stem + ".esm")
                            sp_patched.add(base_stem + ".esl")
                        elif nl.endswith("_kid.ini"):
                            sp_patched.add(base_stem + ".esp")
                            sp_patched.add(base_stem + ".esm")
                            sp_patched.add(base_stem + ".esl")
                        elif nl.endswith("_dsd.json") or nl.endswith("_dsd.yaml"):
                            sp_patched.add(base_stem + ".esp")
                            sp_patched.add(base_stem + ".esm")
                            sp_patched.add(base_stem + ".esl")
            except OSError:
                pass
    except OSError:
        pass
    return bos_patched, sp_patched


def _get_priority_for_plugin(plugin_name: str, mods_path: Path,
                              priorities: Dict[str, int]) -> int:
    """Amethyst mod priority for the plugin's owner. -1 if not found."""
    if not mods_path or not mods_path.is_dir():
        return -1
    for mod_dir in mods_path.iterdir():
        if not mod_dir.is_dir():
            continue
        for sub in (mod_dir, mod_dir / "Data"):
            if (sub / plugin_name).is_file():
                return priorities.get(mod_dir.name, -1)
    return -1


def _has_skygen_ini(plugin_name: str, mods_path: Optional[Path]) -> bool:
    """True if a SkyGen INI for this plugin exists in the SkyGen output directories."""
    if not mods_path or not mods_path.is_dir():
        return False
    stem = Path(plugin_name).stem.lower()
    base = mods_path

    # BOS: SkyGen BOS/SKSE/Plugins/Data/Base Object Swapper/{stem}_SkyGen_SWAP.ini
    ini = base / "SkyGen BOS" / "SKSE" / "Plugins" / "Data" / "Base Object Swapper" / f"{stem}_SkyGen_SWAP.ini"
    if ini.is_file():
        return True

    # BOS (legacy naming): {stem}_SkyGen_SWAP.ini anywhere in mod
    bos_mod = base / "SkyGen BOS"
    if bos_mod.is_dir():
        for f in bos_mod.rglob("*.ini"):
            fn = f.name.lower()
            if fn == f"{stem}_skygen_swap.ini" or fn.endswith(f"{stem}_skygen_swap.ini"):
                return True

    # SkyPatcher: uses load-order prefix, so we check within the SP dir
    for sp_dir_name in ("SkyPatcher", "SkyPatcher2"):
        sp_dir = base / "SkyGen SkyPatcher" / "SKSE" / "Plugins" / sp_dir_name
        if not sp_dir.is_dir():
            continue
        for f in sp_dir.rglob("*.ini"):
            fn_lower = f.stem.lower()
            # Strip load-order prefix (e.g. "03-") then check for _skygen
            cleaned = re.sub(r"^[0-9a-f]+-", "", fn_lower)
            if cleaned == f"{stem}_skygen" or cleaned == stem:
                return True
        break

    return False


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AuditEntry:
    plugin_name:    str
    masters:        List[str]
    patch_scents:   Set[str]           # PRE_PATCHED_* flags
    mod_name:       str                # owning mod folder name
    priority:       int                # Amethyst priority (higher = wins)
    dependents:     List[str] = field(default_factory=list)
    transitively_safe: bool = False
    has_new_records: bool = False      # plugin adds its own FormIDs
    patch_is_skygen: bool = False      # patch INI resides in SkyGen output dir

    @property
    def is_patched(self) -> bool:
        return bool(self.patch_scents)

    @property
    def can_disable(self) -> bool:
        if self.has_new_records:
            return False
        return self.is_patched and (not self.dependents or self.transitively_safe)

    @property
    def primary_patch_label(self) -> Tuple[str, str]:
        """(label, colour) for dominant patch type."""
        for key in ("PRE_PATCHED_BOS", "PRE_PATCHED_SP", "PRE_PATCHED_SPID",
                    "PRE_PATCHED_KID", "PRE_PATCHED_DSD", "PRE_PATCHED_OAR",
                    "PRE_PATCHED_SYNTHESIS"):
            if key in self.patch_scents:
                return _PATCH_LABELS[key]
        return ("Unknown", TEXT_DIM)

    @property
    def unsafe_reason(self) -> str:
        parts: List[str] = []
        if self.dependents and not self.transitively_safe:
            names = ", ".join(self.dependents[:4])
            extra = f" (+{len(self.dependents)-4} more)" if len(self.dependents) > 4 else ""
            parts.append(f"Required by: {names}{extra}")
        if self.has_new_records:
            parts.append("Adds new records (can't be disabled)")
        return "; ".join(parts)


# Wizard

class PluginAuditWizard(ctk.CTkFrame):
    """Standalone Plugin Audit & Cleanup wizard."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
        **_kwargs,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._game      = game
        self._log_fn    = log_fn or (lambda msg: None)
        self._on_close  = on_close or (lambda: None)
        self._entries: Dict[str, AuditEntry] = {}
        self._scan_done = False

        self._build_header()
        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self._show_scan_step()

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _build_header(self):
        hdr = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0)
        hdr.pack(fill="x")
        ctk.CTkLabel(
            hdr,
            text=f"Plugin Audit & Cleanup \u2014 {self._game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            hdr, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_close,
        ).pack(side="right", padx=4, pady=4)

    # ------------------------------------------------------------------
    # Step — Scan
    # ------------------------------------------------------------------

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _show_scan_step(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body,
            text="Scan Load Order",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(8, 4))

        ctk.CTkLabel(
            self._body,
            text=(
                "Scans your active load order and identifies plugins that are safe "
                "to disable because a patch already replicates their function."
                "\n\nTakes into account:"
                "\n  \u2022 BOS, SkyPatcher, SPID, KID, DSD, OAR, Synthesis patches"
                "\n  \u2022 Master dependencies (transitive resolution)"
                "\n  \u2022 Amethyst mod priority (higher priority wins conflicts)"
                "\n  \u2022 New record detection (plugins that add their own FormIDs"
                "\n    cannot be disabled even with a patch)"
                "\n  \u2022 Orphaned INI cleanup — removes BOS/SkyPatcher INIs for"
                "\n    plugins that cannot be disabled"
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="left", wraplength=380,
        ).pack(pady=(0, 12), fill="x")

        self._log_var = ctk.StringVar(value="Ready.")
        ctk.CTkLabel(
            self._body, textvariable=self._log_var,
            font=FONT_NORMAL, text_color=TEXT_DIM, wraplength=380,
        ).pack(pady=(0, 6))

        self._progress = ctk.CTkProgressBar(self._body)
        self._progress.pack(fill="x", padx=20, pady=(0, 12))
        self._progress.set(0)

        ctk.CTkButton(
            self._body,
            text="Start Scan",
            font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV,
            text_color=TEXT_ON_ACCENT, width=160, height=40,
            command=self._start_scan,
        ).pack()

    def _log(self, msg: str):
        self._log_fn(msg)
        self.after(0, lambda m=msg: self._log_var.set(m))

    def _start_scan(self):
        for w in self._body.winfo_children():
            try:
                w.configure(state="disabled")
            except Exception:
                pass
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _do_scan(self):
        try:
            game    = self._game
            profile = _read_active_profile(game)
            self._log(f"Active profile: '{profile}'")

            lo = _read_loadorder(game, profile)
            if not lo:
                self.after(0, lambda: self._log_var.set("No active plugins found."))
                return

            try:
                mods_path = game.get_effective_mod_staging_path()
            except Exception:
                mods_path = game.get_mod_staging_path()

            self._log("Reading mod priorities…")
            priorities = _read_modlist_priorities(game, profile)

            # Pre-build plugin→path index in one pass so per-plugin lookups are O(1)
            self._log("Indexing plugin files…")
            _plugin_file_cache.clear()
            if mods_path and mods_path.is_dir():
                for mod_dir in mods_path.iterdir():
                    if not mod_dir.is_dir():
                        continue
                    for candidate in mod_dir.iterdir():
                        if candidate.is_file() and candidate.suffix.lower() in (".esp", ".esm", ".esl"):
                            key = f"{mods_path}|{candidate.name}"
                            if key not in _plugin_file_cache:
                                _plugin_file_cache[key] = candidate
                    data_sub = mod_dir / "Data"
                    if data_sub.is_dir():
                        for candidate in data_sub.iterdir():
                            if candidate.is_file() and candidate.suffix.lower() in (".esp", ".esm", ".esl"):
                                key = f"{mods_path}|{candidate.name}"
                                if key not in _plugin_file_cache:
                                    _plugin_file_cache[key] = candidate

            self._log("Building cross-mod patch index\u2026")
            bos_idx, sp_idx = _build_patch_index(mods_path)
            self._log(f"Patch index: {len(bos_idx)} BOS, {len(sp_idx)} SP/other targets")

            total   = len(lo)
            entries: Dict[str, AuditEntry] = {}

            for i, plugin_name in enumerate(lo):
                if i % 10 == 0 or i == total - 1:
                    prog = (i + 1) / total
                    self.after(0, lambda p=prog: self._progress.set(p))

                if i % 20 == 0 or i == total - 1:
                    self._log(f"Scanning {i+1}/{total}: {plugin_name}")

                plugin_path = _find_plugin_file(plugin_name, mods_path, game)
                if plugin_path is None:
                    continue

                _, masters = _parse_header(plugin_path)
                mod_folder  = _find_mod_folder(plugin_path, mods_path)
                mod_name    = mod_folder.name if mod_folder else ""
                priority    = priorities.get(mod_name, -1)

                has_new = _plugin_has_new_records(plugin_path)

                scents: Set[str] = set()
                pkey = plugin_name.lower()
                if pkey in bos_idx:
                    scents.add("PRE_PATCHED_BOS")
                if pkey in sp_idx:
                    scents.add("PRE_PATCHED_SP")

                # Synthesis
                if plugin_name.lower() in ("synthesis.esp", "synthesis.esm"):
                    scents.add("PRE_PATCHED_SYNTHESIS")

                # OAR — mod folder has OpenAnimationReplacer dir
                if mod_folder:
                    oar_dir = mod_folder / "meshes" / "animationdatasinglefile"
                    oar_dir2 = mod_folder / "SKSE" / "Plugins" / "OpenAnimationReplacer"
                    if oar_dir.is_dir() or oar_dir2.is_dir():
                        scents.add("PRE_PATCHED_OAR")

                entries[plugin_name] = AuditEntry(
                    plugin_name=plugin_name,
                    masters=masters,
                    patch_scents=scents,
                    mod_name=mod_name,
                    priority=priority,
                    has_new_records=has_new,
                    patch_is_skygen=_has_skygen_ini(plugin_name, mods_path),
                )

            # --- Bashed Patch coverage ---
            # Any plugin listed as a master of "Bashed Patch, *.esp" has its
            # changes merged and is therefore patched (safe to disable).
            _bp_masters: Set[str] = set()
            for pname, entry in entries.items():
                if pname.lower().startswith("bashed patch,"):
                    for master in entry.masters:
                        ml = master.lower()
                        if ml not in BASE_GAME_PLUGINS:
                            _bp_masters.add(ml)
            if _bp_masters:
                for pname, entry in entries.items():
                    if pname.lower() in _bp_masters:
                        entry.patch_scents.add("PRE_PATCHED_BASHED")

            # Build reverse dependency map
            master_to_deps: Dict[str, List[str]] = {}
            for pname, entry in entries.items():
                for master in entry.masters:
                    master_to_deps.setdefault(master.lower(), []).append(pname)
            for pname, entry in entries.items():
                entry.dependents = master_to_deps.get(pname.lower(), [])

            # Transitive safe-set resolution
            # has_new_records plugins cannot be disabled, so they must NOT enter
            # safe_set — otherwise plugins they depend on are wrongly
            # marked safe and their orphaned INIs are missed on the
            # first cleanup pass.
            def _is_patched(e: AuditEntry) -> bool:
                return e.is_patched and not e.has_new_records

            safe_set: Set[str] = set()
            changed = True
            while changed:
                changed = False
                for pname, entry in entries.items():
                    if pname in safe_set:
                        continue
                    if not _is_patched(entry):
                        continue
                    all_deps_safe = all(
                        dep in safe_set or dep not in entries
                        for dep in entry.dependents
                    )
                    if all_deps_safe:
                        safe_set.add(pname)
                        changed = True

            for pname, entry in entries.items():
                if pname in safe_set and entry.dependents:
                    entry.transitively_safe = True
                    entry.dependents = []

            self._entries = entries
            self._scan_done = True
            self.after(0, self._show_audit_step)

        except Exception as exc:
            self.after(0, lambda e=exc: self._log_var.set(f"Error: {e}"))

    # ------------------------------------------------------------------
    # Step — Audit
    # ------------------------------------------------------------------

    def _show_audit_step(self):
        self._clear_body()

        entries = self._entries

        safe             = {n: e for n, e in entries.items() if e.can_disable}
        blocked_by_new   = {
            n: e for n, e in entries.items()
            if not e.can_disable and e.is_patched and e.has_new_records
        }
        blocked_by_deps  = {
            n: e for n, e in entries.items()
            if not e.can_disable and e.is_patched and not e.has_new_records and e.dependents
        }

        # Header counts
        parts = [f"Audit complete — {len(entries)} plugins scanned, {len(safe)} safe to disable"]
        if blocked_by_new:
            parts.append(f"{len(blocked_by_new)} blocked (new records)")
        if blocked_by_deps:
            parts.append(f"{len(blocked_by_deps)} blocked (dependents)")
        ctk.CTkLabel(
            self._body,
            text=", ".join(parts),
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(8, 4))

        if not safe and not blocked_by_new and not blocked_by_deps:
            ctk.CTkLabel(
                self._body,
                text=(
                    "No disableable plugins detected.\n\n"
                    "None of your active plugins have a BOS, SkyPatcher, SPID, KID, "
                    "DSD, or OAR patch that replaces their function."
                ),
                font=FONT_NORMAL, text_color=TEXT_DIM, justify="left", wraplength=380,
            ).pack(pady=20)
        else:
            # Scrollable list
            scroll = ctk.CTkScrollableFrame(
                self._body, fg_color=BG_PANEL,
            )
            scroll.pack(fill="both", expand=True, pady=(4, 8))

            # Header row
            hdr = ctk.CTkFrame(scroll, fg_color=BG_HEADER)
            hdr.pack(fill="x", pady=(0, 4))
            for col, (label, w) in enumerate([
                ("",         24),   # checkbox column
                ("Plugin",   210),
                ("Patch",    140),
                ("Priority", 70),
                ("Status",   220),
            ]):
                ctk.CTkLabel(hdr, text=label, font=FONT_BOLD, text_color=TEXT_MAIN,
                             width=w, anchor="w").grid(row=0, column=col, padx=6, pady=4)

            self._check_vars: Dict[str, ctk.BooleanVar] = {}

            # Safe entries first
            for plugin_name, entry in sorted(safe.items(),
                                             key=lambda kv: -kv[1].priority):
                self._add_audit_row(scroll, plugin_name, entry, selectable=True)

            # Blocked by new records
            if blocked_by_new:
                sep = ctk.CTkFrame(scroll, fg_color=WARN_COLOR, height=1)
                sep.pack(fill="x", pady=(8, 2))
                ctk.CTkLabel(
                    scroll,
                    text="\u26a0  Patched but adds new records—cannot disable (records won't exist without ESP)",
                    font=FONT_BOLD, text_color=WARN_COLOR,
                ).pack(anchor="w", padx=8, pady=(0, 4))
                for plugin_name, entry in sorted(blocked_by_new.items(),
                                                 key=lambda kv: -kv[1].priority):
                    self._add_audit_row(scroll, plugin_name, entry, selectable=False)

            # Blocked by dependents
            if blocked_by_deps:
                sep = ctk.CTkFrame(scroll, fg_color=UNSAFE_COLOR, height=1)
                sep.pack(fill="x", pady=(8, 2))
                ctk.CTkLabel(
                    scroll,
                    text="\u26a0  Patched but blocked—other plugins depend on these as masters",
                    font=FONT_BOLD, text_color=UNSAFE_COLOR,
                ).pack(anchor="w", padx=8, pady=(0, 4))
                for plugin_name, entry in sorted(blocked_by_deps.items(),
                                                 key=lambda kv: -kv[1].priority):
                    self._add_audit_row(scroll, plugin_name, entry, selectable=False)

        # Bottom buttons
        btn_row = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_row.pack(side="bottom", pady=(8, 0))

        ctk.CTkButton(
            btn_row, text="\u2190 Re-Scan", width=110, height=36,
            font=FONT_BOLD, fg_color=BG_HEADER, hover_color="#3d3d3d",
            text_color=TEXT_MAIN, command=self._show_scan_step,
        ).pack(side="left", padx=(0, 8))

        if safe:
            ctk.CTkButton(
                btn_row,
                text="Select All Safe",
                width=130, height=36,
                font=FONT_BOLD, fg_color=BG_HEADER, hover_color="#3d3d3d",
                text_color=TEXT_MAIN,
                command=self._select_all,
            ).pack(side="left", padx=(0, 8))

            ctk.CTkButton(
                btn_row,
                text="Disable Selected",
                width=150, height=36,
                font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV,
                text_color=TEXT_ON_ACCENT,
                command=self._disable_selected,
            ).pack(side="left")

        # Cleanup orphaned INIs — show whenever any patched plugin can't be disabled
        any_blocked = bool(blocked_by_new or blocked_by_deps)
        if any_blocked:
            ctk.CTkButton(
                btn_row,
                text=f"Clean Orphaned INIs",
                width=160, height=36,
                font=FONT_BOLD, fg_color=BG_HEADER, hover_color="#3d3d3d",
                text_color=TEXT_MAIN,
                command=self._cleanup_orphaned_inis,
            ).pack(side="right", padx=(8, 0))

    def _add_audit_row(self, parent, plugin_name: str, entry: AuditEntry,
                       selectable: bool):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=1)

        # Checkbox
        if selectable:
            var = ctk.BooleanVar(value=False)
            self._check_vars[plugin_name] = var
            ctk.CTkCheckBox(row, text="", variable=var, width=24,
                            checkbox_width=16, checkbox_height=16,
                            ).grid(row=0, column=0, padx=6)
        else:
            ctk.CTkLabel(row, text="", width=24).grid(row=0, column=0, padx=6)

        # Plugin name
        ctk.CTkLabel(row, text=plugin_name, font=FONT_NORMAL,
                     text_color=TEXT_MAIN, width=210, anchor="w",
                     ).grid(row=0, column=1, padx=6)

        # Patch type
        label, color = entry.primary_patch_label
        # If multiple patches apply, append "+"
        n_patches = len(entry.patch_scents)
        if n_patches > 1:
            label += f" +{n_patches - 1}"
        ctk.CTkLabel(row, text=label, font=FONT_BOLD,
                     text_color=color, width=140, anchor="w",
                     ).grid(row=0, column=2, padx=6)

        # Priority
        prio_text = str(entry.priority) if entry.priority >= 0 else "—"
        ctk.CTkLabel(row, text=prio_text, font=FONT_NORMAL,
                     text_color=TEXT_DIM, width=70, anchor="center",
                     ).grid(row=0, column=3, padx=6)

        # Status
        if selectable:
            if entry.transitively_safe:
                status_text  = "\u2713 Safe (deps also patched)"
                status_color = SAFE_COLOR
            else:
                status_text  = "\u2713 Safe to disable"
                status_color = SAFE_COLOR
        else:
            reason = entry.unsafe_reason
            if entry.has_new_records:
                status_text  = "\u26a0 New records"
                status_color = WARN_COLOR
            elif entry.dependents:
                status_text  = "\u26a0 Blocked (deps)"
                status_color = UNSAFE_COLOR
            else:
                status_text  = reason or "\u26a0 Blocked"
                status_color = UNSAFE_COLOR
            # Append origin note if not already clear from context
            if entry.is_patched:
                if entry.patch_is_skygen:
                    status_text += "  INI\u2192SkyGen"
                else:
                    status_text += "  INI\u2192mod"
        ctk.CTkLabel(row, text=status_text, font=FONT_NORMAL,
                     text_color=status_color, width=220, anchor="w",
                     ).grid(row=0, column=4, padx=6)

    def _select_all(self):
        for var in self._check_vars.values():
            var.set(True)

    def _disable_selected(self):
        selected = [name for name, var in self._check_vars.items() if var.get()]
        if not selected:
            return

        # Confirmation dialog
        ok = tkmb.askyesno(
            title="Disable Selected Plugins",
            message=(
                f"Disable {len(selected)} plugin(s)?\n\n"
                + "\n".join(f"  \u2022 {n}" for n in selected[:5])
                + ("\n  \u2022 ..." if len(selected) > 5 else "")
                + "\n\nThe patches for these plugins will still apply at runtime."
            ),
            icon="warning",
        )
        if not ok:
            return

        game    = self._game
        profile = _read_active_profile(game)
        pdir    = _profile_dir(game, profile)
        plugins_path = pdir / "plugins.txt"
        lo_path      = pdir / "loadorder.txt"

        if not plugins_path.is_file():
            self._log_var.set("plugins.txt not found — cannot disable.")
            return

        # Read current plugins.txt
        lines = plugins_path.read_text(encoding="utf-8").splitlines()
        selected_lower = {n.lower() for n in selected}

        new_lines = []
        disabled_count = 0
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("*"):
                name = stripped[1:]
                if name.lower() in selected_lower:
                    new_lines.append(name)   # remove * to disable
                    disabled_count += 1
                    continue
            new_lines.append(line)

        plugins_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        # Trigger plugin panel reload — same pattern as pgpatcher/_on_done
        try:
            topbar = self.winfo_toplevel()._topbar
        except Exception:
            topbar = None

        self._on_close()

        if topbar is not None:
            try:
                topbar.after(0, topbar._reload_mod_panel)
            except Exception:
                pass

    def _cleanup_orphaned_inis(self):
        """Delete SkyGen INIs for plugins that can't be disabled."""
        try:
            mods_path = self._game.get_effective_mod_staging_path()
        except Exception:
            mods_path = self._game.get_mod_staging_path()
        if not mods_path or not mods_path.is_dir():
            self._log("Cannot find mod staging path \u2014 aborting cleanup.")
            return

        # Collect target plugin names (lowered) — all patched plugins that
        # *cannot* be disabled, regardless of *why* (new records OR dependents).
        targets: Set[str] = set()
        for pname, entry in self._entries.items():
            if not entry.can_disable and entry.is_patched:
                targets.add(pname.lower())

        if not targets:
            self._log("No orphaned INIs to clean.")
            return

        # Confirmation dialog
        ok = tkmb.askyesno(
            title="Clean Orphaned INIs",
            message=(
                f"Delete SkyGen-generated INI files for {len(targets)} plugin(s) "
                "that cannot be disabled?\n\n"
                "This removes INIs in the SkyGen BOS and SkyGen SkyPatcher "
                "output mods. INIs that ship with original mods are not affected."
            ),
            icon="warning",
        )
        if not ok:
            return

        deleted = 0
        found = 0
        _for_re = re.compile(r"for\s+([^\s\r\n]+\.es[pml])", re.I)
        _extensions = (".esp", ".esm", ".esl")

        def _resolve_plugin(ini: Path, is_sp: bool = False) -> Optional[str]:
            """Derive target plugin name from INI. Tries .esp/.esm/.esl against target set."""
            try:
                header = ini.read_text(encoding="utf-8", errors="ignore")[:512]
            except OSError:
                self._log(f"  Cannot read {ini}")
                return None
            m = _for_re.search(header)
            if m:
                raw = m.group(1).lower()
                if raw in targets:
                    return raw
                # Regex may have captured only part of a space-containing name.
                # Fall through to stem logic below.
            stem = ini.stem.lower()
            if is_sp:
                stem = re.sub(r"^[0-9a-f]+-", "", stem)
                if stem.endswith("_skygen"):
                    stem = stem[:-7]
            else:
                for suffix in ("_skygen_swap", "_swap"):
                    if stem.endswith(suffix):
                        stem = stem[:-len(suffix)]
                        break
            # Try each extension against the target set
            for ext in _extensions:
                candidate = stem + ext
                if candidate in targets:
                    return candidate
            # Fallback — caller checks ``pkey in targets`` so this won't match
            return stem + ".esp"

        # Only scan SkyGen output directories — never touch mod-author-supplied INIs
        skygen_mods = ("SkyGen BOS", "SkyGen SkyPatcher")
        for mod_name in skygen_mods:
            mod_dir = mods_path / mod_name
            if not mod_dir.is_dir():
                continue

            # BOS: SKSE/Plugins/Data/Base Object Swapper/
            bos_dir = mod_dir / "SKSE" / "Plugins" / "Data" / "Base Object Swapper"
            if bos_dir.is_dir():
                for ini in list(bos_dir.rglob("*.ini")):
                    if not ini.is_file():
                        continue
                    found += 1
                    pkey = _resolve_plugin(ini, is_sp=False)
                    if pkey and pkey in targets:
                        try:
                            ini.unlink()
                            deleted += 1
                            self._log(f"  Deleted {ini.name}")
                        except OSError as exc:
                            self._log(f"  Cannot delete {ini}: {exc}")
                    else:
                        self._log(f"  Skipped {ini.name} (plugin '{pkey}' not in blocked list)")

            # SkyPatcher
            for sp_name in ("SkyPatcher", "SkyPatcher2"):
                sp_dir = mod_dir / "SKSE" / "Plugins" / sp_name
                if not sp_dir.is_dir():
                    continue
                for ini in list(sp_dir.rglob("*.ini")):
                    if not ini.is_file():
                        continue
                    found += 1
                    pkey = _resolve_plugin(ini, is_sp=True)
                    if pkey and pkey in targets:
                        try:
                            ini.unlink()
                            deleted += 1
                            self._log(f"  Deleted {ini.name}")
                        except OSError as exc:
                            self._log(f"  Cannot delete {ini}: {exc}")
                    else:
                        self._log(f"  Skipped {ini.name} (plugin '{pkey}' not in blocked list)")
                break

        self._log(f"Cleanup: scanned {found} INI file(s) across all mods, deleted {deleted} orphaned INI(s) for {len(targets)} plugin(s).")
        self._show_cleanup_result(deleted)

    def _show_cleanup_result(self, deleted: int) -> None:
        """Result page after cleanup with Re-Scan button."""
        plugin_count = len([
            p for p, e in self._entries.items()
            if not e.can_disable and e.is_patched
        ])
        self._clear_body()
        ctk.CTkLabel(
            self._body,
            text="\u2705 Cleanup Complete" if deleted else "\u26a0 No INIs Found",
            font=FONT_BOLD, text_color="#6bc76b" if deleted else "#e0c45b",
        ).pack(pady=(16, 8))

        ctk.CTkLabel(
            self._body,
            text=(
                f"Removed {deleted} INI file(s) for {plugin_count} plugin(s)."
                if deleted
                else "No matching INI files were found for the blocked plugins."
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, wraplength=380,
        ).pack(pady=(0, 16))

        ctk.CTkButton(
            self._body, text="Re-Scan to Verify",
            width=160, height=40,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._show_scan_step,
        ).pack()

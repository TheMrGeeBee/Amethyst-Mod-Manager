"""
skygen.py — BOS/SkyPatcher patch generator wizard.

Adapted from SkyGen (Mayhem). Replaces MO2/PyQt6 layer for native Amethyst use.
Step 1: Scan load order. Step 2: Generate INI patches.
"""

from __future__ import annotations

import mmap
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

import customtkinter as ctk

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.wheel_compat import bind_scrollable_wheel
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

# Frankensnoop — pure Python plugin parser (no MO2/PyQt6)

BASE_GAME_PLUGINS: Set[str] = {
    "Skyrim.esm", "Update.esm", "Dawnguard.esm",
    "HearthFires.esm", "Dragonborn.esm",
    "_ResourcePack.esl", "SkyrimVR.esm",
}

GLOBAL_IGNORE_PLUGINS: Set[str] = {
    "Skyrim.esm", "Update.esm", "Dawnguard.esm",
    "HearthFires.esm", "Dragonborn.esm",
    "_ResourcePack.esl", "SkyrimVR.esm",
}

# Signatures indicating pure framework/utility mods
GLOBAL_FRAMEWORK_SIGNATURES: Set[str] = {
    "SCPT", "SKSE", "SNDR", "SOUN", "CLMT", "WTHR", "MESG",
    "DLBR", "GMST", "KYWD",
}

# Protected authors (Bethesda/CC)
PROTECTED_AUTHORS: Set[str] = {
    "creationclub", "cc", "bethesda game studios",
    "bethesda", "mcarofano", "bnesmith", "rsalvatore",
}

# BOS Categories — must be defined before BOS_SIGS
BOS_CATEGORIES: Dict[str, Set[str]] = {
    "Tree": {"TREE"},
    "Furniture": {"FURN", "MSTT"},
    "Container": {"CONT"},
    "Light": {"LIGH"},
    "Misc": {"STAT", "ACTI", "DOOR", "FLOR"},
    "Body": {"ARMO", "ARMA", "NPC_", "RACE"},
    "Skin": {"ARMO", "ARMA", "NPC_", "RACE", "ASSET_SKIN", "ASSET_BODY"},
}

# BOS-eligible signatures — static objects BOS can swap
BOS_SIGS: Set[str] = set()
for _sigs in BOS_CATEGORIES.values():
    BOS_SIGS.update(_sigs)

# SkyPatcher-eligible signatures
SP_SIGS: Set[str] = {
    "NPC_", "WEAP", "ARMO", "AMMO", "ALCH", "BOOK", "LVLI", "LVLN", "FLST",
    "SPEL", "RACE", "ARMA", "HDPT", "HAIR", "FURN", "INGR", "KEYM",
}

# Signature → category mapping (BOS vs SkyPatcher)
SIGNATURE_TO_CATEGORIES: Dict[str, List[str]] = {
    "ARMO": ["Armor", "SkyPatcher"],
    "WEAP": ["Weapons", "SkyPatcher"],
    "AMMO": ["Ammo", "SkyPatcher"],
    "BOOK": ["Books", "SkyPatcher"],
    "ALCH": ["Alchemy", "SkyPatcher"],
    "INGR": ["Ingredients", "SkyPatcher"],
    "MISC": ["Misc", "BOS"],
    "STAT": ["Statics", "BOS"],
    "FURN": ["Furniture", "BOS"],
    "CONT": ["Containers", "BOS"],
    "LIGH": ["Lights", "BOS"],
    "DOOR": ["Doors", "BOS"],
    "ACTI": ["Activators", "BOS"],
    "TREE": ["Trees", "BOS"],
    "FLOR": ["Flora", "BOS"],
    "NPC_": ["NPCs", "SkyPatcher"],
    "SPEL": ["Spells", "SkyPatcher"],
    "RACE": ["Races", "SkyPatcher"],
    "LVLI": ["Leveled Items", "SkyPatcher"],
    "LVLN": ["Leveled NPCs", "SkyPatcher"],
    "FLST": ["Form Lists", "SkyPatcher"],
}

LOGIC_SIGS: Set[str] = {
    "QUST", "MGEF", "KYWD", "VMAD", "SCRP", "DLBR", "INFO", "DIAL",
}

# SkyPatcher example action per record signature
_SP_EXAMPLE_ACTION: Dict[str, str] = {
    "NPC_":  "SetEssential=0",
    "ARMO":  "SetWeight=0",
    "WEAP":  "SetWeight=0",
    "AMMO":  "SetWeight=0",
    "ALCH":  "SetWeight=0",
    "BOOK":  "SetWeight=0",
    "MISC":  "SetWeight=0",
    "INGR":  "SetWeight=0",
    "KEYM":  "SetWeight=0",
    "LVLI":  "ChangeChance=100",
    "LVLN":  "ChangeChance=100",
    "FLST":  "; AddToFormList=Skyrim.esm|<FormID>",
    "SPEL":  "; addSpell=Skyrim.esm|<FormID>",
    "RACE":  "; AddSpell=Skyrim.esm|<FormID>",
    "ARMA":  "; SetRace=Skyrim.esm|<FormID>",
    "HDPT":  "; SetRace=Skyrim.esm|<FormID>",
    "FURN":  "; SetKeyword=Skyrim.esm|<FormID>",
}
_SCENT_PATTERNS: Dict[str, re.Pattern] = {
    "SKSE":       re.compile(r"SKSE|skse|Script Extender", re.I),
    "DynDOLOD":   re.compile(r"DynDOLOD|dyndolod|LOD", re.I),
    "xEdit":      re.compile(r"SSEEdit|xEdit|TES5Edit|FO4Edit", re.I),
    "Synthesis":  re.compile(r"Synthesis|Mutagen", re.I),
    "Menu":       re.compile(r"SkyUI|AddItemMenu|RaceMenu|UIExtensions", re.I),
    "MCMHelper":  re.compile(r"MCMHelper|MCM", re.I),
    "PapyrusUtil": re.compile(r"PapyrusUtil", re.I),
    "ConsoleUtil": re.compile(r"ConsoleUtil", re.I),
    "JContainers": re.compile(r"JContainers", re.I),
    "AddressLib": re.compile(r"Address.*Library", re.I),
    "PO3":        re.compile(r"powerofthree|po3", re.I),
    "SPID":       re.compile(r"SPID|Spell.*Perk", re.I),
    "Framework":  re.compile(r"Framework|Base Object Swapper|Papyrus Extender|Address Library", re.I),
    "Engine":     re.compile(r"Engine Fixes|Bug Fixes|SSE Fixes", re.I),
    "BaseObjectSwapper": re.compile(r"BaseObjectSwapper|_SWAP\.ini", re.I),
    "SkyPatcher": re.compile(r"SkyPatcher", re.I),
    "AnimationFramework": re.compile(r"XPMSE|XP32|FNIS|Nemesis", re.I),
}
_BLACKLIST_AUTHORS = {
    "jaxonz", "expired6978", "meh321", "sheson", "doodlum",
    "powerofthree", "ryan-rsm-mckenzie", "aers", "nu_p_p",
    "tudoran", "towawot", "umgak", "versuchdrei", "xenius",
    "maskedrpgfan", "andrelo12", "krisv777", "edzio", "gopher",
    "banjobunny", "acro", "nemesis", "xpmse", "team xpmse", "groovtama",
    "mrowka", "colinswrath", "po3", "erkeil",
}


@dataclass
class PluginDNA:
    """Minimal plugin fingerprint — sufficient for the audit + patch gen."""
    plugin_name:   str
    signatures:    Set[str]
    author:        str
    masters:       List[str]
    folder_scents: Set[str] = field(default_factory=set)
    file_size:     int = 0
    dependents:    List[str] = field(default_factory=list)  # plugins that list this as a master

    @property
    def has_dependents(self) -> bool:
        return bool(self.dependents)

    @property
    def is_framework(self) -> bool:
        """True if this is a patcher/engine, not a content mod.
        
        Checks folder scents, author blacklist, masters-only plugins,
        and framework-only signatures.
        """
        if "BOS_FRAMEWORK" in self.folder_scents or "SKYPATCHER_FRAMEWORK" in self.folder_scents:
            return True
        al = self.author.lower()
        if any(a in al for a in _BLACKLIST_AUTHORS):
            return True
        if not self.signatures and self.masters:
            return True
    # Framework-only signatures
        if self.signatures and self.signatures <= GLOBAL_FRAMEWORK_SIGNATURES:
            return True
        return False

    @property
    def pre_patched_bos(self) -> bool:
        return "PRE_PATCHED_BOS" in self.folder_scents

    @property
    def pre_patched_sp(self) -> bool:
        return "PRE_PATCHED_SP" in self.folder_scents

    @property
    def pre_patched_bashed(self) -> bool:
        return "PRE_PATCHED_BASHED" in self.folder_scents

    @property
    def is_micro(self) -> bool:
        return "MICRO_FILE" in self.folder_scents

    @property
    def can_disable(self) -> bool:
        """True if patch exists AND no plugin depends on this as a master."""
        if self.has_dependents:
            return False
        return (self.pre_patched_bos or self.pre_patched_sp or self.pre_patched_bashed
                or "PRE_PATCHED_SPID" in self.folder_scents
                or "PRE_PATCHED_KID" in self.folder_scents
                or "PRE_PATCHED_DSD" in self.folder_scents
                or "PRE_PATCHED_OAR" in self.folder_scents
                or "PRE_PATCHED_SYNTHESIS" in self.folder_scents)

    @property
    def unsafe_reason(self) -> str:
        """Why this plugin can't be disabled. Empty if safe."""
        if self.has_dependents:
            deps = ", ".join(self.dependents[:5])
            extra = f" (+{len(self.dependents)-5} more)" if len(self.dependents) > 5 else ""
            return f"Required by: {deps}{extra}"
        return ""

    @property
    def disable_reason(self) -> str:
        reasons = []
        if self.pre_patched_bos:
            reasons.append("Base Object Swapper patch found")
        if self.pre_patched_sp:
            reasons.append("SkyPatcher INI patch found")
        if "PRE_PATCHED_SPID" in self.folder_scents:
            reasons.append("SPID distributor INI found")
        if "PRE_PATCHED_KID" in self.folder_scents:
            reasons.append("KID distributor INI found")
        if "PRE_PATCHED_DSD" in self.folder_scents:
            reasons.append("DSD JSON/YAML patch found")
        if "PRE_PATCHED_OAR" in self.folder_scents:
            reasons.append("OAR animation replacer found")
        if "PRE_PATCHED_SYNTHESIS" in self.folder_scents:
            reasons.append("Synthesis patcher output")
        if self.pre_patched_bashed:
            reasons.append("Bashed Patch merged")
        return " + ".join(reasons) if reasons else ""

    @property
    def bos_eligible(self) -> bool:
        """True if plugin has static object signatures (STAT, DOOR, FURN, etc.) eligible for BOS.
        
        Excludes Body/Skin unless pluginless body mod.
        """
        # Get static object signatures from BOS_CATEGORIES (exclude "Body"/"Skin" which are for pluginless mods)
        static_sigs = set()
        for cat_name, sigs in BOS_CATEGORIES.items():
            if cat_name in ("Body", "Skin"):
                continue  # pluginless body mods only
            static_sigs.update(sigs)

        has_static = bool(self.signatures & static_sigs)
        is_pluginless_body = "ASSET_SKIN" in self.folder_scents or "ASSET_BODY" in self.folder_scents

        if is_pluginless_body:
            return True

        return has_static

    @property
    def sp_eligible(self) -> bool:
        return bool(self.signatures & SP_SIGS)


def _scan_memoryview(mv) -> Tuple[Set[str], bool]:
    """Scan a bytes-like object for GRUP signatures."""
    signatures: Set[str] = set()
    if len(mv) < 24 or bytes(mv[:4]) != b"TES4":
        return signatures, False
    tes4_size = int.from_bytes(bytes(mv[4:8]), "little")
    offset = 24 + tes4_size
    if offset >= len(mv):
        return signatures, True
    file_len = len(mv)
    while offset < file_len - 24:
        if bytes(mv[offset:offset + 4]) == b"GRUP":
            rt = bytes(mv[offset + 8:offset + 12]).decode("ascii", errors="ignore")
            if len(rt) == 4 and rt.replace("_", "").isalnum():
                signatures.add(rt)
            grup_size = int.from_bytes(bytes(mv[offset + 4:offset + 8]), "little")
            if grup_size < 24:
                break
            offset += grup_size
        else:
            offset += 1
    return signatures, True


def _decompress_record(payload: bytes, uncompressed_size: int) -> Optional[bytes]:
    """Decompress a compressed TES4 record payload.

    Skyrim SE uses LZ4 block compression for most modern records; legacy LE
    plugins use zlib. Try each in turn. Returns None if all attempts fail —
    caller then falls back to FormID-only extraction.
    """
    if uncompressed_size <= 0 or not payload:
        return None
    try:
        import lz4.block as _lz4_block  # type: ignore
        try:
            out = _lz4_block.decompress(payload, uncompressed_size=uncompressed_size)
            if len(out) == uncompressed_size:
                return out
        except Exception:
            pass
    except ImportError:
        pass
    try:
        import lz4.frame as _lz4_frame  # type: ignore
        try:
            out = _lz4_frame.decompress(payload)
            if len(out) == uncompressed_size:
                return out
        except Exception:
            pass
    except ImportError:
        pass
    try:
        import zlib as _zlib
        out = _zlib.decompress(payload)
        if len(out) == uncompressed_size:
            return out
    except Exception:
        pass
    return None


def _read_edid_full(data: bytes, start: int, end: int) -> Tuple[str, str]:
    """Walk subrecords in ``data[start:end]`` and return (EDID, FULL).

    Handles XXXX extended-length subrecord prefix. Used for both compressed
    (decompressed buffer) and uncompressed records.
    """
    edid = ""
    full_name = ""
    sub = start
    while sub < end - 6:
        sub_tag = data[sub:sub + 4]
        if sub_tag == b"XXXX":
            sub_len = int.from_bytes(data[sub + 4:sub + 8], "little")
            sub += 8
            if sub + 4 > end:
                break
            sub_tag = data[sub:sub + 4]
            sub += 6
        else:
            sub_len = int.from_bytes(data[sub + 4:sub + 6], "little")
            sub += 6
        if sub + sub_len > end:
            break
        if sub_tag == b"EDID":
            raw = data[sub:sub + sub_len]
            edid = raw.split(b"\x00")[0].decode("ascii", errors="ignore").strip()
        elif sub_tag == b"FULL":
            raw = data[sub:sub + sub_len]
            full_name = raw.split(b"\x00")[0].decode("utf-8", errors="ignore").strip()
        sub += sub_len
    return edid, full_name


def _extract_records(path: Path, wanted_sigs: Set[str]) -> List[Dict]:
    """Extract FormIDs for records matching *wanted_sigs*.

    Returns [{form_id, local_id, signature, editor_id, name}].
    Walks GRUP blocks only (skips TES4 header). Safe from bg thread.
    Compressed records are decompressed (LZ4 block → LZ4 frame → zlib) so
    EDID/FULL are still recovered; if decompression fails the record is
    still emitted with just its FormID.
    """
    records: List[Dict] = []
    try:
        data = path.read_bytes()
    except OSError:
        return records

    if len(data) < 24 or data[:4] != b"TES4":
        return records

    tes4_size = int.from_bytes(data[4:8], "little")
    offset = 24 + tes4_size
    file_len = len(data)
    wanted_bytes = {s.encode("ascii") for s in wanted_sigs}

    while offset < file_len - 24:
        tag = data[offset:offset + 4]
        if tag == b"GRUP":
            grup_size = int.from_bytes(data[offset + 4:offset + 8], "little")
            grup_type_sig = data[offset + 8:offset + 12]
            if grup_size < 24:
                offset += 1
                continue
            if grup_type_sig in wanted_bytes:
                # Walk records inside this GRUP
                rec_offset = offset + 24
                grup_end = offset + grup_size
                sig_str = grup_type_sig.decode("ascii", errors="ignore")
                while rec_offset < grup_end - 24:
                    rec_tag = data[rec_offset:rec_offset + 4]
                    rec_size = int.from_bytes(data[rec_offset + 4:rec_offset + 8], "little")
                    rec_flags = int.from_bytes(data[rec_offset + 8:rec_offset + 12], "little")
                    form_id_raw = int.from_bytes(data[rec_offset + 12:rec_offset + 16], "little")
                    rec_start = rec_offset + 24
                    rec_end = rec_start + rec_size
                    if rec_tag == b"GRUP":
                        # Nested GRUP (cell children etc.) — skip
                        nested_size = int.from_bytes(data[rec_offset + 4:rec_offset + 8], "little")
                        rec_offset += max(nested_size, 24)
                        continue
                    if rec_end > file_len or rec_size > 10 * 1024 * 1024:
                        rec_offset += 24
                        continue
                    is_compressed = bool(rec_flags & 0x00040000)
                    edid = ""
                    full_name = ""
                    if rec_size > 0:
                        if is_compressed:
                            # Compressed records start with a 4-byte
                            # uncompressed size, then the compressed payload.
                            if rec_size >= 4:
                                uncomp_size = int.from_bytes(
                                    data[rec_start:rec_start + 4], "little"
                                )
                                payload = data[rec_start + 4:rec_end]
                                buf = _decompress_record(payload, uncomp_size)
                                if buf is not None:
                                    edid, full_name = _read_edid_full(
                                        buf, 0, len(buf)
                                    )
                        else:
                            edid, full_name = _read_edid_full(
                                data, rec_start, rec_end
                            )
                    if form_id_raw:
                        fid_str = f"{form_id_raw:08X}"
                        local_id = fid_str[-6:]
                        records.append({
                            "form_id": fid_str,
                            "local_id": local_id,
                            "editor_id": edid,
                            "name": full_name,
                            "signature": sig_str,
                        })
                    rec_offset = rec_end
            offset += grup_size
        else:
            offset += 1
    return records



def _extract_signatures(path: Path) -> Set[str]:
    try:
        with open(path, "rb") as f:
            try:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    sigs, ok = _scan_memoryview(mm)
                    if ok:
                        return sigs
            except (OSError, ValueError, mmap.error):
                pass
            f.seek(0)
            data = f.read()
            sigs, _ = _scan_memoryview(memoryview(data))
            return sigs
    except Exception:
        return set()


def _parse_header(path: Path) -> Tuple[str, List[str]]:
    """Return (author, [masters])."""
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


def _detect_scents(plugin_path: Path, mod_folder: Optional[Path]) -> Set[str]:
    scents: Set[str] = set()
    if mod_folder and mod_folder.is_dir():
        stem = plugin_path.stem
        name = plugin_path.name
        # Search mod root + FOMOD Data/ subfolder
        search_roots = [mod_folder]
        data_sub = mod_folder / "Data"
        if data_sub.is_dir():
            search_roots.append(data_sub)

        for search_root in search_roots:
            # BOS: <stem>_SWAP.ini anywhere in mod
            try:
                for f in search_root.rglob("*.ini"):
                    if f.is_file() and f.name.lower() == f"{stem.lower()}_swap.ini":
                        scents.add("PRE_PATCHED_BOS")
                        scents.add("BOS_FRAMEWORK")
                        break
            except OSError:
                pass
            # Also check SKSE/Plugins/Data/Base Object Swapper/
            bos_dir = search_root / "SKSE" / "Plugins" / "Data" / "Base Object Swapper"
            if bos_dir.is_dir():
                try:
                    for ini in bos_dir.rglob("*.ini"):
                        txt = ini.read_text(encoding="utf-8", errors="ignore")
                        if name in txt or stem in txt:
                            scents.add("PRE_PATCHED_BOS")
                            scents.add("BOS_FRAMEWORK")
                            break
                except Exception:
                    pass

        # SkyPatcher: SKSE/Plugins/SkyPatcher/ or SkyPatcher2/
        for sp_dir_name in ("SkyPatcher", "SkyPatcher2"):
            sp_dir = mod_folder / "SKSE" / "Plugins" / sp_dir_name
            if sp_dir.is_dir():
                scents.add("SKYPATCHER_FRAMEWORK")
                try:
                    for ini in sp_dir.rglob("*.ini"):
                        chunk = ini.read_text(encoding="utf-8", errors="ignore")[:2048]
                        if name in chunk or stem in chunk:
                            scents.add("PRE_PATCHED_SP")
                            break
                except Exception:
                    pass
                break

        # Additional patch frameworks
        for search_root in search_roots:
            # SPID: <stem>_DISTR.ini
            try:
                for f in search_root.rglob("*.ini"):
                    if f.is_file() and f.name.lower() == f"{stem.lower()}_distr.ini":
                        scents.add("PRE_PATCHED_SPID")
                        break
            except OSError:
                pass
            # KID: <stem>_KID.ini
            try:
                for f in search_root.rglob("*.ini"):
                    if f.is_file() and f.name.lower() == f"{stem.lower()}_kid.ini":
                        scents.add("PRE_PATCHED_KID")
                        break
            except OSError:
                pass
            # DSD: <stem>_DSD.json / _DSD.yaml
            try:
                for f in search_root.rglob("*.json"):
                    if f.is_file() and f.name.lower() == f"{stem.lower()}_dsd.json":
                        scents.add("PRE_PATCHED_DSD")
                        break
            except OSError:
                pass
            try:
                for f in search_root.rglob("*.yaml"):
                    if f.is_file() and f.name.lower() == f"{stem.lower()}_dsd.yaml":
                        scents.add("PRE_PATCHED_DSD")
                        break
            except OSError:
                pass

        # OAR: SKSE/Plugins/OpenAnimationReplacer/
        oar_dir = mod_folder / "SKSE" / "Plugins" / "OpenAnimationReplacer"
        if oar_dir.is_dir():
            scents.add("PRE_PATCHED_OAR")

        # Synthesis: plugin named Synthesis.esp
        if stem.lower() == "synthesis":
            scents.add("PRE_PATCHED_SYNTHESIS")

        # Micro file: < 2 KB ESL stub
        try:
            if plugin_path.exists() and plugin_path.stat().st_size < 2 * 1024:
                scents.add("MICRO_FILE")
        except Exception:
            pass
    # Name-pattern scents
    for sname, pattern in _SCENT_PATTERNS.items():
        if pattern.search(plugin_path.name):
            scents.add(sname)
    return scents


def _sniff_plugin(plugin_path: Path, mod_folder: Optional[Path]) -> PluginDNA:
    sigs = _extract_signatures(plugin_path)
    author, masters = _parse_header(plugin_path)
    scents = _detect_scents(plugin_path, mod_folder)
    return PluginDNA(
        plugin_name=plugin_path.name,
        signatures=sigs,
        author=author,
        masters=masters,
        folder_scents=scents,
        file_size=plugin_path.stat().st_size if plugin_path.exists() else 0,
    )


# Path helpers

def _read_active_profile(game: "BaseGame") -> str:
    """Current profile name: _active_profile_dir → last active → default."""
    # 1. Live profile dir set by Amethyst
    active_dir = getattr(game, "_active_profile_dir", None)
    if active_dir is not None:
        name = Path(active_dir).name
        if name:
            return name

    # 2. Persisted last active
    try:
        last = game.get_last_active_profile()
        if last and last != "default":
            # Verify the profile dir actually exists
            candidate = game.get_profile_root() / "profiles" / last
            if candidate.is_dir():
                return last
    except Exception:
        pass

    # 3. First non-default with loadorder.txt, else "default"
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
    """Return ordered list of *active* plugin names for the given profile."""
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
                # Older format without star prefix — treat all as active
                active_set.add(line)

    if not full_order:
        return list(active_set)

    # Preserve order, filter to active only (base masters always included)
    return [p for p in full_order if p in BASE_GAME_PLUGINS or p in active_set]


def _find_plugin_file(plugin_name: str, mods_path: Path, data_path: Optional[Path]) -> Optional[Path]:
    """3-tier search: mods/ → mods/<n>/Data/ → game Data/."""
    if mods_path.is_dir():
        for mod_dir in mods_path.iterdir():
            if not mod_dir.is_dir():
                continue
            cand = mod_dir / plugin_name
            if cand.is_file():
                return cand
            cand2 = mod_dir / "Data" / plugin_name
            if cand2.is_file():
                return cand2
    if data_path and data_path.is_dir():
        cand = data_path / plugin_name
        if cand.is_file():
            return cand
    return None


def _build_patch_index(mods_path: Path) -> Tuple[Set[str], Set[str]]:
    """Scan staging directory for existing patches.
    
    Returns (bos_patched, sp_patched) — lower-case plugin name sets.
    Catches patches in different mods than the target plugin.
    """
    bos_patched: Set[str] = set()
    sp_patched: Set[str] = set()

    if not mods_path or not mods_path.is_dir():
        return bos_patched, sp_patched

    _for_re = re.compile(r"for\s+([^\s\r\n]+\.es[pml])", re.I)

    try:
        for mod_dir in mods_path.iterdir():
            if not mod_dir.is_dir():
                continue

            # --- BOS: any *_SWAP.ini anywhere in the mod tree ---
            bos_dir = mod_dir / "SKSE" / "Plugins" / "Data" / "Base Object Swapper"
            if bos_dir.is_dir():
                try:
                    for ini in bos_dir.rglob("*.ini"):
                        try:
                            header = ini.read_text(encoding="utf-8", errors="ignore")[:512]
                        except OSError:
                            continue
                        m = _for_re.search(header)
                        if m:
                            bos_patched.add(m.group(1).lower())
                        else:
                            # Fall back: strip _SkyGen_SWAP / _SWAP suffix from filename
                            stem = ini.stem.lower()
                            for suffix in ("_skygen_swap", "_swap"):
                                if stem.endswith(suffix):
                                    stem = stem[: -len(suffix)]
                                    break
                            # stem could map to .esp/.esm/.esl — try all
                            for ext in (".esp", ".esm", ".esl"):
                                bos_patched.add(stem + ext)
                except OSError:
                    pass

            # Also catch bare <stem>_SWAP.ini files at mod root (e.g. hand-authored)
            try:
                for ini in mod_dir.rglob("*_swap.ini"):
                    stem = ini.stem.lower()
                    if stem.endswith("_swap"):
                        stem = stem[:-5]
                    for ext in (".esp", ".esm", ".esl"):
                        bos_patched.add(stem + ext)
            except OSError:
                pass

            # --- SkyPatcher: SKSE/Plugins/SkyPatcher[2]/ ---
            for sp_name in ("SkyPatcher", "SkyPatcher2"):
                sp_dir = mod_dir / "SKSE" / "Plugins" / sp_name
                if sp_dir.is_dir():
                    try:
                        for ini in sp_dir.rglob("*.ini"):
                            try:
                                header = ini.read_text(encoding="utf-8", errors="ignore")[:512]
                            except OSError:
                                continue
                            m = _for_re.search(header)
                            if m:
                                sp_patched.add(m.group(1).lower())
                            else:
                                # Strip LO prefix (e.g. "1F-") and _SkyGen suffix
                                stem = ini.stem.lower()
                                stem = re.sub(r"^[0-9a-f]+-", "", stem)
                                if stem.endswith("_skygen"):
                                    stem = stem[:-7]
                                for ext in (".esp", ".esm", ".esl"):
                                    sp_patched.add(stem + ext)
                    except OSError:
                        pass
                    break  # found a SP dir, no need to check SP2
    except OSError:
        pass

    return bos_patched, sp_patched


def _find_mod_folder(plugin_path: Path, mods_path: Path) -> Optional[Path]:
    """Resolve the mod staging folder that owns this plugin."""
    try:
        rel = plugin_path.relative_to(mods_path)
        # rel is <ModFolder>/... so first part is the mod folder
        return mods_path / rel.parts[0]
    except ValueError:
        return None



# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------

class SkyGenWizard(ctk.CTkFrame):
    """BOS/SkyPatcher patch generator. Step 1: scan, Step 2: generate."""

    _wizard_title = "SkyGen — Patch Generator"

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
        **_kwargs,
    ) -> None:
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game        = game
        self._log         = log_fn or print
        self._dna_map:    Dict[str, PluginDNA] = {}   # plugin_name -> DNA
        self._plugin_vars: Dict[str, ctk.BooleanVar] = {}  # plugin_name -> checkbox var
        self._done_fired  = False
        self._cancel_flag = False

        # ------ Title bar ------
        tbar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        tbar.pack(fill="x")
        tbar.pack_propagate(False)
        ctk.CTkLabel(
            tbar,
            text=f"{self._wizard_title}  —  {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            tbar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._close,
        ).pack(side="right", padx=4, pady=4)

        # ------ Body ------
        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=12, pady=12)

        self._show_step_scan()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _set_label(self, attr: str, text: str, color: str = TEXT_DIM):
        def _apply():
            try:
                w = getattr(self, attr, None)
                if w and w.winfo_exists():
                    w.configure(text=text, text_color=color)
            except Exception:
                pass
        self.after(0, _apply)

    def _log_ui(self, msg: str):
        self._log(f"[SkyGen] {msg}")

    def _close(self):
        if self._done_fired:
            return
        self._done_fired = True
        try:
            topbar = self.winfo_toplevel()._topbar
        except Exception:
            topbar = None
        self._on_close_cb()
        if topbar:
            try:
                topbar.after(0, topbar._reload_mod_panel)
            except Exception:
                pass

    def _offer_open_folder(self, out_dir: Path) -> None:
        """Show button to open output folder in system file manager."""
        try:
            # Only add the button once
            if getattr(self, "_open_folder_btn", None) is not None:
                return
            self._open_folder_btn = ctk.CTkButton(
                self._body,
                text=f"Open output folder",
                width=220, height=30,
                font=FONT_NORMAL,
                fg_color=BG_PANEL, hover_color="#3d3d3d", text_color=TEXT_DIM,
                command=lambda: self._xdg_open(out_dir),
            )
            self._open_folder_btn.pack(pady=(0, 4))
        except Exception:
            pass

    @staticmethod
    def _xdg_open(path: Path) -> None:
        from Utils.xdg import xdg_open
        xdg_open(path)

    # ------------------------------------------------------------------
    # Step 1 — Scan
    # ------------------------------------------------------------------

    def _show_step_scan(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Scan Active Plugins",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        info = (
            "SkyGen will read your active load order and scan every plugin "
            "for record signatures that Base Object Swapper and SkyPatcher can "
            "override at runtime. This identifies:"
            "\n\n  \u2022  Which plugins are eligible for new BOS / SkyPatcher patches "
            "(override-only STAT, DOOR, FURN, and similar records that BOS can swap)."
            "\n  \u2022  Which plugins already have patches in place (skipped)."
            "\n\nNote: Plugins that add their own new records (references, items, "
            "NPCs) will still need their ESP/ESL loaded even after patching \u2014 "
            "the INI only swaps existing base forms, it does not create new ones."
        )
        ctk.CTkLabel(
            self._body, text=info,
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="left", wraplength=760,
        ).pack(pady=(0, 12), fill="x")

        self._scan_status = ctk.CTkLabel(
            self._body, text="Ready to scan.",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=760,
        )
        self._scan_status.pack(pady=(0, 6))

        self._scan_progress = ctk.CTkProgressBar(self._body)
        self._scan_progress.set(0)
        self._scan_progress.pack(fill="x", padx=20, pady=(0, 12))

        btn_row = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_row.pack(side="bottom", pady=(8, 0))
        self._scan_btn_row = btn_row

        ctk.CTkButton(
            btn_row, text="Close", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._close,
        ).pack(side="left", padx=(0, 8))

        self._scan_btn = ctk.CTkButton(
            btn_row, text="Scan \u2192", width=140, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._start_scan,
        )
        self._scan_btn.pack(side="right")

        # Store cancel button reference (created on demand)
        self._scan_cancel_btn: Optional[ctk.CTkButton] = None

    def _start_scan(self):
        self._cancel_flag = False
        self._scan_btn.configure(state="disabled", text="Scanning\u2026")
        # Show a Cancel button
        if self._scan_cancel_btn is None:
            self._scan_cancel_btn = ctk.CTkButton(
                self._scan_btn_row, text="Cancel", width=100, height=36,
                font=FONT_BOLD, fg_color=BG_HEADER, hover_color="#3d3d3d",
                text_color=TEXT_MAIN, command=self._cancel,
            )
            self._scan_cancel_btn.pack(before=self._scan_btn, side="right", padx=(0, 8))
        else:
            self._scan_cancel_btn.pack(before=self._scan_btn, side="right", padx=(0, 8))
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _cancel(self):
        """Signal the worker thread to stop."""
        self._cancel_flag = True
        self._log_ui("Cancelled by user.")
        self.after(0, self._show_step_scan)


    def _do_scan(self):
        try:
            game = self._game
            profile = _read_active_profile(game)
            self._log_ui(f"Active profile: '{profile}'")
            lo = _read_loadorder(game, profile)
            if not lo:
                self.after(0, lambda: self._set_label(
                    "_scan_status",
                    "No active plugins found.\nMake sure a profile is loaded and has an active load order.",
                    "#e06c6c",
                ))
                self.after(0, lambda: self._scan_btn.configure(state="normal", text="Scan"))
                return
            # Prefer profile-aware staging path when available
            get_staging = getattr(game, "get_effective_mod_staging_path", None) or game.get_mod_staging_path
            mods_path = get_staging()
            game_path = game.get_game_path()
            data_path = game_path / "Data" if game_path else None

            total = len(lo)
            dna_map: Dict[str, PluginDNA] = {}

            # Build a cross-mod patch index once before the plugin loop so we
            # can flag plugins whose patches live in a different mod folder
            # (e.g. a SkyGen Output mod, a hand-crafted BOS pack, etc.)
            self._log_ui("Building cross-mod patch index…")
            bos_index, sp_index = _build_patch_index(mods_path)
            self._log_ui(f"Patch index: {len(bos_index)} BOS targets, {len(sp_index)} SP targets")

            for i, plugin_name in enumerate(lo):
                if self._cancel_flag:
                    self.after(0, lambda: self._show_step_scan())
                    return
                if plugin_name in GLOBAL_IGNORE_PLUGINS:
                    continue
                plugin_path = _find_plugin_file(plugin_name, mods_path, data_path)
                if not plugin_path:
                    continue
                mod_folder = _find_mod_folder(plugin_path, mods_path)
                try:
                    dna = _sniff_plugin(plugin_path, mod_folder)
                except Exception as exc:
                    self._log_ui(f"Sniff failed for {plugin_name}: {exc}")
                    continue
                # Apply cross-mod patch index — patches in other mod folders
                pkey = plugin_name.lower()
                if pkey in bos_index:
                    dna.folder_scents.add("PRE_PATCHED_BOS")
                if pkey in sp_index:
                    dna.folder_scents.add("PRE_PATCHED_SP")
                dna_map[plugin_name] = dna
                # Batch UI updates every 50 plugins to avoid flooding the Tk
                # event queue with thousands of callbacks for large load orders.
                if i % 50 == 0 or i == total - 1:
                    frac = (i + 1) / total if total else 0
                    self.after(0, lambda f=frac, n=plugin_name, idx=i, t=total: (
                        self._scan_progress.set(f),
                        self._set_label("_scan_status",
                                        f"({idx+1}/{t}) {n}", TEXT_DIM),
                    ))

            self._dna_map = dna_map
            self._lo_index_map = {name: i for i, name in enumerate(lo)}

            # --- Bashed Patch coverage ---
            # Plugins merged into "Bashed Patch, *.esp" are already patched.
            _bp_masters: Set[str] = set()
            for pname, dna in dna_map.items():
                if pname.lower().startswith("bashed patch,"):
                    for master in dna.masters:
                        ml = master.lower()
                        if ml not in BASE_GAME_PLUGINS:
                            _bp_masters.add(ml)
            if _bp_masters:
                for pname, dna in dna_map.items():
                    if pname.lower() in _bp_masters:
                        dna.folder_scents.add("PRE_PATCHED_BASHED")

            # Build reverse dependency map — flag any plugin that is listed as a
            # master by another active plugin so we never recommend disabling it.
            master_to_dependents: Dict[str, List[str]] = {}
            for pname, dna in dna_map.items():
                for master in dna.masters:
                    master_to_dependents.setdefault(master.lower(), []).append(pname)
            for pname, dna in dna_map.items():
                deps = master_to_dependents.get(pname.lower(), [])
                if deps:
                    dna.dependents = deps

            # Transitive safe-to-disable resolution:
            # A patched plugin whose ONLY dependents are themselves patched (and
            # thus also removable) is also safe to disable.  Iterate until stable.
            def _is_patched(d: PluginDNA) -> bool:
                return (d.pre_patched_bos or d.pre_patched_sp or d.pre_patched_bashed
                        or "PRE_PATCHED_SPID" in d.folder_scents
                        or "PRE_PATCHED_KID"  in d.folder_scents
                        or "PRE_PATCHED_DSD"  in d.folder_scents
                        or "PRE_PATCHED_OAR"  in d.folder_scents
                        or "PRE_PATCHED_SYNTHESIS" in d.folder_scents)

            safe_set: Set[str] = set()
            changed = True
            while changed:
                changed = False
                for pname, dna in dna_map.items():
                    if pname in safe_set:
                        continue
                    if not _is_patched(dna):
                        continue
                    # All dependents must themselves be in safe_set (or not in
                    # the active load order at all — e.g. a vanilla master)
                    all_deps_safe = all(
                        dep in safe_set or dep not in dna_map
                        for dep in dna.dependents
                    )
                    if all_deps_safe:
                        safe_set.add(pname)
                        changed = True

            # Stamp transitively-safe plugins: clear their dependents so
            # can_disable returns True, but record the original list for the UI.
            for pname, dna in dna_map.items():
                if pname in safe_set and dna.dependents:
                    dna.folder_scents.add("TRANSITIVELY_SAFE")
                    dna.dependents = []   # unblock can_disable
            disableable = sum(1 for d in dna_map.values() if d.can_disable)
            bos_eligible = sum(1 for d in dna_map.values() if d.bos_eligible and not d.is_framework)
            sp_eligible  = sum(1 for d in dna_map.values() if d.sp_eligible  and not d.is_framework)

            summary = (
                f"  Plugins scanned:                {len(dna_map)}\n"
                f"  BOS-eligible for new patches:   {bos_eligible}\n"
                f"  SkyPatcher-eligible:            {sp_eligible}"
            )
            self.after(0, lambda: self._set_label("_scan_status", summary, "#6bc76b"))
            self.after(300, self._show_step_generate)

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            self._log_ui(f"Scan error: {exc}\n{tb}")
            self.after(0, lambda exc=exc: self._set_label("_scan_status", f"Error: {exc}", "#e06c6c"))

    # ------------------------------------------------------------------
    # Step 2 — Generate patches
    # ------------------------------------------------------------------

    def _show_step_generate(self):
        self._clear_body()
        self._plugin_vars.clear()

        ctk.CTkLabel(
            self._body, text="Step 2: Generate Patches",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 8))

        # Mode selector
        mode_frame = ctk.CTkFrame(self._body, fg_color=BG_PANEL)
        mode_frame.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(mode_frame, text="Output type:", font=FONT_BOLD,
                     text_color=TEXT_MAIN).grid(row=0, column=0, padx=12, pady=8)
        self._mode_var = ctk.StringVar(value="BOS")
        for i, opt in enumerate(("BOS", "SkyPatcher")):
            ctk.CTkRadioButton(
                mode_frame, text=opt, variable=self._mode_var, value=opt,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                command=self._refresh_plugin_list,
            ).grid(row=0, column=i + 1, padx=16, pady=8)

        ctk.CTkLabel(
            self._body,
            text=(
                "Select the plugins to generate patches for. Only override records "
                "of supported types (STAT, DOOR, FURN, LIGH, MISC, CONT, etc.) are "
                "extracted. The generated INI will be placed under the SkyGen mod:"
                f"\n  \u2022 BOS:  SKSE/Plugins/Data/Base Object Swapper/"
                f"\n  \u2022 SkyPatcher:  SKSE/Plugins/SkyPatcher/"
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="left", wraplength=760,
        ).pack(fill="x", pady=(0, 12))

        # Select / Deselect buttons
        select_frame = ctk.CTkFrame(self._body, fg_color="transparent")
        select_frame.pack(fill="x", pady=(0, 8))
        ctk.CTkButton(
            select_frame, text="Select All", width=100, height=28,
            font=FONT_NORMAL,
            fg_color=BG_PANEL, hover_color="#3d3d3d", text_color=TEXT_DIM,
            command=self._select_all_plugins,
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            select_frame, text="Deselect All", width=100, height=28,
            font=FONT_NORMAL,
            fg_color=BG_PANEL, hover_color="#3d3d3d", text_color=TEXT_DIM,
            command=self._deselect_all_plugins,
        ).pack(side="left", padx=4)

        # Plugin count label
        self._plugin_count_label = ctk.CTkLabel(
            select_frame, text="", font=FONT_NORMAL, text_color=TEXT_DIM,
        )
        self._plugin_count_label.pack(side="left", padx=12)

        # Scrollable plugin list
        self._plugin_scroll = ctk.CTkScrollableFrame(
            self._body, fg_color=BG_PANEL, corner_radius=4,
        )
        self._plugin_scroll.pack(fill="both", expand=True, pady=(0, 12))
        bind_scrollable_wheel(self._plugin_scroll)

        self._gen_status = ctk.CTkLabel(
            self._body, text="",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=760,
        )
        self._gen_status.pack(pady=(0, 4))

        # Generate button
        btn_row = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_row.pack(side="bottom", pady=(8, 0))
        self._gen_btn_row = btn_row

        ctk.CTkButton(
            btn_row, text="Back", width=100, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._show_step_scan,
        ).pack(side="left", padx=(0, 8))

        self._gen_btn = ctk.CTkButton(
            btn_row, text="Generate", width=140, height=36,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._start_generate,
        )
        self._gen_btn.pack(side="right")

        # Populate the plugin list
        self._refresh_plugin_list()

    def _refresh_plugin_list(self):
        # Clear existing checkboxes
        for widget in self._plugin_scroll.winfo_children():
            widget.destroy()
        self._plugin_vars.clear()

        mode = self._mode_var.get()
        if mode == "BOS":
            eligible = {n: d for n, d in self._dna_map.items()
                        if d.bos_eligible and not d.is_framework and not d.pre_patched_bashed}
            sig_set = BOS_SIGS
        else:
            eligible = {n: d for n, d in self._dna_map.items()
                        if d.sp_eligible and not d.is_framework and not d.pre_patched_bashed}
            sig_set = SP_SIGS

        sorted_plugins = sorted(eligible.keys())

        # Update count label
        self._plugin_count_label.configure(
            text=f"{len(sorted_plugins)} {mode}-eligible plugins found"
        )

        if not sorted_plugins:
            ctk.CTkLabel(
                self._plugin_scroll, text=f"No {mode}-eligible plugins found.",
                font=FONT_NORMAL, text_color=TEXT_DIM,
            ).pack(pady=12)
            return

        # Add checkboxes for each eligible plugin
        for plugin_name in sorted_plugins:
            var = ctk.BooleanVar(value=True)  # preselected by default
            self._plugin_vars[plugin_name] = var
            dna = eligible[plugin_name]
            sigs = ", ".join(sorted(dna.signatures & sig_set))
            display_name = plugin_name if len(plugin_name) <= 50 else plugin_name[:47] + "..."
            text = f"{display_name} ({sigs})" if sigs else display_name
            ctk.CTkCheckBox(
                self._plugin_scroll, text=text, variable=var,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                checkbox_width=22, checkbox_height=22,
            ).pack(fill="x", padx=8, pady=2)

    def _select_all_plugins(self):
        for var in self._plugin_vars.values():
            var.set(True)

    def _deselect_all_plugins(self):
        for var in self._plugin_vars.values():
            var.set(False)

    def _start_generate(self):
        self._cancel_flag = False
        self._gen_btn.configure(state="disabled", text="Generating\u2026")
        # Show a Cancel button
        if not hasattr(self, "_gen_cancel_btn") or self._gen_cancel_btn is None:
            self._gen_cancel_btn = ctk.CTkButton(
                self._gen_btn_row, text="Cancel", width=100, height=36,
                font=FONT_BOLD, fg_color=BG_HEADER, hover_color="#3d3d3d",
                text_color=TEXT_MAIN, command=self._cancel,
            )
            self._gen_cancel_btn.pack(side="right", padx=(0, 8), before=self._gen_btn)
        else:
            self._gen_cancel_btn.pack(side="right", padx=(0, 8), before=self._gen_btn)
        threading.Thread(target=self._do_generate, daemon=True).start()

    def _do_generate(self):
        try:
            mode = self._mode_var.get()          # "BOS" or "SkyPatcher"
            out_name = "SkyGen BOS" if mode == "BOS" else "SkyGen SkyPatcher"
            get_staging = getattr(self._game, "get_effective_mod_staging_path", None) or self._game.get_mod_staging_path
            mods_path = get_staging()
            out_dir = mods_path / out_name

            # Remove previous output so users always get a clean result
            if out_dir.exists():
                import shutil
                shutil.rmtree(out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)

            if mode == "BOS":
                written = self._gen_bos(out_dir)
            else:
                written = self._gen_skypatcher(out_dir)

            # Register as a managed mod (meta.ini + modlist entry)
            self._register_output_mod(out_name, out_dir)

            self._log_ui(f"Patch generation complete. Output: {out_dir}")
            # Show Done step so user sees confirmation, then can close
            self.after(0, lambda m=mode, w=written, d=out_dir: self._show_step_done(m, w, d))

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            self._log_ui(f"Generate error: {exc}\n{tb}")
            self.after(0, lambda exc=exc: self._set_label("_gen_status", f"Error: {exc}", "#e06c6c"))
            self.after(0, lambda: self._gen_btn.configure(
                state="normal", text="Generate", command=self._start_generate
            ))
            if hasattr(self, "_gen_cancel_btn") and self._gen_cancel_btn:
                self.after(0, lambda: self._gen_cancel_btn.pack_forget())

    def _register_output_mod(self, mod_name: str, mod_dir: Path) -> None:
        """Write meta.ini and prepend the mod to modlist.txt so Amethyst treats
        the output folder as a managed, enabled mod — same pattern as Pandora."""
        from Nexus.nexus_meta import NexusModMeta, write_meta
        from Utils.modlist import prepend_mod

        meta = NexusModMeta(mod_name=mod_name, installation_file="SkyGen", root_folder=False)
        write_meta(mod_dir / "meta.ini", meta)

        # Resolve active profile modlist
        try:
            profile = _read_active_profile(self._game)
        except Exception:
            profile = "default"
        modlist_path = _profile_dir(self._game, profile) / "modlist.txt"
        if not modlist_path.is_file():
            # Fallback: try profile root directly
            modlist_path = self._game.get_profile_root() / "modlist.txt"

        prepend_mod(modlist_path, mod_name, enabled=True)
        self._log_ui(f"Registered '{mod_name}' in modlist as enabled mod.")

        # Trigger mod panel refresh if reachable
        try:
            toplevel = self.winfo_toplevel()
            mod_panel = getattr(toplevel, "_mod_panel", None)
            if mod_panel is not None:
                mod_panel.after(0, mod_panel.reload_after_install)
        except Exception:
            pass

    def _gen_bos(self, out_dir: Path) -> int:
        """Write BOS swap INIs using object names (EditorIDs).

        Format: sourceObject|replacementObject|NONE|chanceR(percent)
        Based on real-world BOS INI examples.
        """
        bos_dir = out_dir / "SKSE" / "Plugins" / "Data" / "Base Object Swapper"
        bos_dir.mkdir(parents=True, exist_ok=True)
        lo_map = getattr(self, "_lo_index_map", {})

        written = 0
        for plugin_name, dna in self._dna_map.items():
            if self._cancel_flag:
                break
            if not dna.bos_eligible or dna.is_framework:
                continue
            var = self._plugin_vars.get(plugin_name)
            if not var or not var.get():
                continue
            bos_sigs = dna.signatures & BOS_SIGS
            if not bos_sigs:
                continue

            get_staging = getattr(self._game, "get_effective_mod_staging_path", None) or self._game.get_mod_staging_path
            mods_path = get_staging()
            game_path = self._game.get_game_path()
            data_path = game_path / "Data" if game_path else None
            plugin_path = _find_plugin_file(plugin_name, mods_path, data_path)

            records = _extract_records(plugin_path, bos_sigs) if plugin_path else []

            stem = Path(plugin_name).stem
            out_ini = bos_dir / f"{stem}_SkyGen_SWAP.ini"
            lines = [
                f"; Auto-generated by SkyGen (Amethyst) for {plugin_name}",
                f"; Record types: {', '.join(sorted(bos_sigs))}",
                "; Format: sourceObject|replacementObject|NONE|chanceR(percent)",
                "; Edit the right side and chance values for your setup.",
                "[Forms]",
                "",
            ]
            if records:
                    for rec in records:
                        label = rec["editor_id"] or rec["name"] or rec["local_id"]
                        # BOS format: 0x{local_id}~PluginName|0x{local_id}~ReplacementPlugin|props|chance
                        # Left side: object being replaced (from scanned plugin)
                        # Right side: replacement (user edits this)
                        orig = f"0x{rec['local_id']}~{plugin_name}"
                        placeholder = f"0x{rec['local_id']}~<ReplacementPlugin>"
                        entry = f"{orig}|{placeholder}|NONE|chanceR(100)"
                        lines.append(f"; [{rec['signature']}] {label}")
                        lines.append(entry)
                    lines.append("")
            else:
                lines += [
                    f"; Could not extract records from {plugin_name}.",
                    f"; Format: objectName|replacementName|NONE|chanceR(100)",
                ]

            out_ini.write_text("\n".join(lines), encoding="utf-8")
            written += 1
            self._log_ui(f"BOS: {out_ini.name} ({len(records)} records)")

        self._log_ui(f"BOS done: {written} INIs written to {bos_dir}")
        return written

    def _gen_skypatcher(self, out_dir: Path) -> int:
        """Write SkyPatcher INIs with filterByFormID entries.
        Returns number of INI files written."""
        sp_dir = out_dir / "SKSE" / "Plugins" / "SkyPatcher"
        sp_dir.mkdir(parents=True, exist_ok=True)
        lo_map = getattr(self, "_lo_index_map", {})

        written = 0
        for plugin_name, dna in self._dna_map.items():
            if self._cancel_flag:
                break
            if not dna.sp_eligible or dna.is_framework:
                continue
            # Check if plugin is selected by user
            var = self._plugin_vars.get(plugin_name)
            if not var or not var.get():
                continue
            sp_sigs = dna.signatures & SP_SIGS
            if not sp_sigs:
                continue

            get_staging = getattr(self._game, "get_effective_mod_staging_path", None) or self._game.get_mod_staging_path
            mods_path = get_staging()
            game_path = self._game.get_game_path()
            data_path = game_path / "Data" if game_path else None
            plugin_path = _find_plugin_file(plugin_name, mods_path, data_path)

            records = _extract_records(plugin_path, sp_sigs) if plugin_path else []

            # Use LO-index prefix for filename: ESL → FExx, others → 2-digit hex
            lo_idx = lo_map.get(plugin_name, 999)
            if plugin_name.lower().endswith(".esl"):
                prefix = f"FE{lo_idx:03X}"
            else:
                prefix = f"{lo_idx:02X}"
            stem = Path(plugin_name).stem
            out_ini = sp_dir / f"{prefix}-{stem}_SkyGen.ini"

            lines = [
                f"; Auto-generated by SkyGen (Amethyst) for {plugin_name}",
                f"; Record types: {', '.join(sorted(sp_sigs))}",
                f"; Edit the right-hand value of each Set<Field> line to apply your change.",
                "; SkyPatcher filterByFormID uses full 8-char FormID (with load order).",
                "",
            ]
            # Group entries by signature
            from collections import defaultdict
            by_sig: dict = defaultdict(list)
            for rec in records:
                by_sig[rec["signature"]].append(rec)

            if by_sig:
                for sig in sorted(by_sig.keys()):
                    lines.append(f"; --- {sig} records ---")
                    for rec in by_sig[sig]:
                        label = rec["editor_id"] or rec["name"] or rec["local_id"]
                        lines.append(f"; {label}")
                        # SkyPatcher format: PluginName|FormID:operation=value
                        # Use full 8-char FormID (with load order byte)
                        form_id = rec["form_id"]  # Already 8-char with load order
                        lines.append(f"filterByFormID={plugin_name}|{form_id}")
                        # Emit a commented-out example action for the sig type
                        example = _SP_EXAMPLE_ACTION.get(sig, "; Set<Field> = <Value>")
                        lines.append(f"; {example}")
                        lines.append("")
            else:
                # Fallback
                for sig in sorted(sp_sigs):
                    lines += [
                        f"; [{sig}] — could not extract FormIDs from {plugin_name}",
                        f"; filterByFormID={plugin_name}|<FormID>",
                        f"; {_SP_EXAMPLE_ACTION.get(sig, 'Set<Field> = <Value>')}",
                        "",
                    ]

            out_ini.write_text("\n".join(lines), encoding="utf-8")
            written += 1
            self._log_ui(f"SP: {out_ini.name} ({len(records)} records)")

        self._log_ui(f"SkyPatcher done: {written} INIs written to {sp_dir}")
        return written

    # ------------------------------------------------------------------
    # Step 3 — Done
    # ------------------------------------------------------------------

    def _show_step_done(self, mode: str, written: int, out_dir: Path) -> None:
        """Completion summary with Open Folder button."""
        self._clear_body()
        if hasattr(self, "_gen_cancel_btn") and self._gen_cancel_btn:
            try:
                self._gen_cancel_btn.pack_forget()
            except Exception:
                pass

        ctk.CTkLabel(
            self._body, text="\u2705 Generation Complete",
            font=FONT_BOLD, text_color="#6bc76b",
        ).pack(pady=(0, 8))

        ctk.CTkLabel(
            self._body,
            text=(
                f"Output type:  {mode}"
                f"\nINI files written:  {written}"
                f"\n\nOutput mod:  {out_dir.name}"
                f"\n{out_dir}"
            ),
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="left", wraplength=760,
        ).pack(pady=(0, 12))

        # Open output folder
        ctk.CTkButton(
            self._body, text="Open output folder",
            width=200, height=36,
            font=FONT_BOLD,
            fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=lambda: self._xdg_open(out_dir),
        ).pack(pady=(0, 8))

        # Close button
        ctk.CTkButton(
            self._body, text="Close", width=140, height=40,
            font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._close,
        ).pack()


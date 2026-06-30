"""
modlist.py
Read and write a MO2-compatible modlist.txt file.

Format (one mod per line):
  +ModName          — enabled mod
  -ModName          — disabled mod
  *ModName          — enabled, always-on (cannot be toggled)
  +Name_separator   — separator (MO2 sometimes writes these with +)
  -Name_separator   — separator (canonical form, written with - prefix)

Priority: line 0 (top) = highest priority, last line = priority 0.
Separators do not count toward priority numbering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_SEPARATOR_SUFFIX = "_separator"


@dataclass
class ModEntry:
    name: str
    enabled: bool        # + or *  (always True for separators)
    locked: bool         # * prefix — cannot be toggled
    is_separator: bool = field(default=False)

    @property
    def display_name(self) -> str:
        """Human-readable name: strip _separator suffix for separators."""
        if self.is_separator and self.name.endswith(_SEPARATOR_SUFFIX):
            return self.name[: -len(_SEPARATOR_SUFFIX)]
        return self.name

    @property
    def bundle_name(self) -> str | None:
        """Bundle group name if this entry is a bundle variant, else None.

        Bundle variants are stored as ``<bundle_name>__<variant_name>`` in
        modlist.txt.  The double-underscore is the delimiter.
        """
        if self.is_separator:
            return None
        if "__" in self.name:
            return self.name.split("__", 1)[0]
        return None

    @property
    def variant_name(self) -> str | None:
        """Variant name within bundle, or None if not a bundle variant."""
        if self.is_separator:
            return None
        if "__" in self.name:
            return self.name.split("__", 1)[1]
        return None


def _is_separator(name: str) -> bool:
    return name.endswith(_SEPARATOR_SUFFIX)


def read_modlist(modlist_path: Path) -> list[ModEntry]:
    """
    Parse modlist.txt and return entries in file order (index 0 = highest priority).
    Lines that are blank or don't start with +/-/* are skipped.
    """
    entries: list[ModEntry] = []
    if not modlist_path.is_file():
        return entries
    for line in modlist_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        prefix = line[0]
        name = line[1:]
        if not name:
            continue
        if prefix == "+":
            entries.append(ModEntry(name=name, enabled=True, locked=False,
                                    is_separator=_is_separator(name)))
        elif prefix == "-":
            if _is_separator(name):
                entries.append(ModEntry(name=name, enabled=True, locked=True,
                                        is_separator=True))
            else:
                entries.append(ModEntry(name=name, enabled=False, locked=False,
                                        is_separator=False))
        elif prefix == "*":
            entries.append(ModEntry(name=name, enabled=True,  locked=True,
                                    is_separator=False))
        # else: ignore unknown lines
    return entries


def write_modlist(modlist_path: Path, entries: list[ModEntry]) -> None:
    """
    Write entries back to modlist.txt.
    Creates parent directories if needed.
    """
    modlist_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for e in entries:
        if e.is_separator:
            prefix = "-"          # separators always written with -
        elif e.locked:
            prefix = "*"
        elif e.enabled:
            prefix = "+"
        else:
            prefix = "-"
        lines.append(f"{prefix}{e.name}")
    modlist_path.write_text("\n".join(lines) + ("\n" if lines else ""),
                            encoding="utf-8")


def prepend_mod(modlist_path: Path, mod_name: str, enabled: bool = True) -> None:
    """
    Add a new mod at the top of modlist.txt (highest priority).
    If an entry with the same name already exists it is moved to the top.
    """
    entries = read_modlist(modlist_path)
    # Remove any existing entry with the same name
    entries = [e for e in entries if e.name != mod_name]
    entries.insert(0, ModEntry(name=mod_name, enabled=enabled, locked=False))
    write_modlist(modlist_path, entries)


def ensure_mod_preserving_position(
    modlist_path: Path,
    mod_name: str,
    enabled: bool = True,
) -> None:
    """
    Ensure a mod exists in modlist.txt without changing its existing position.

    If an entry with the same name already exists, its order is preserved and
    only the enabled flag is updated. If no entry exists, the mod is added at
    the top (highest priority), matching prepend_mod's behaviour for new mods.
    """
    entries = read_modlist(modlist_path)
    for e in entries:
        if e.name == mod_name:
            e.enabled = enabled
            write_modlist(modlist_path, entries)
            return

    # If not already present, add as a new top-priority entry.
    entries.insert(0, ModEntry(name=mod_name, enabled=enabled, locked=False))
    write_modlist(modlist_path, entries)


# Profile-root infrastructure folder names. If one of these turns up *inside*
# the mods/ staging folder it's almost always stray/test pollution (a mirror of
# the profile-root layout), never a real mod — so the sync must never adopt it
# into modlist.txt. Case-insensitive match.
_RESERVED_STAGING_NAMES = frozenset({
    "mods", "overwrite", "profiles", "backups",
    "root_folder", "applications",
})


def sync_modlist_with_mods_folder(modlist_path: Path, mods_dir: Path) -> None:
    """Sync modlist_path against mods_dir:

      - Prepend any mod folders not yet in modlist as disabled entries.
      - Remove any non-separator entries whose folder no longer exists.

    Skips MO2 separator dummy folders (_separator suffix) and profile-root
    infrastructure folder names (see _RESERVED_STAGING_NAMES). Creates
    modlist_path if it does not exist. Pure pathlib — no GUI toolkit — so both
    the Tk and Qt Refresh paths can call it (the Tk add-game dialog re-imports
    it from here).
    """
    if not mods_dir.is_dir():
        if not modlist_path.exists():
            modlist_path.touch()
        return

    on_disk: set[str] = {
        d.name for d in mods_dir.iterdir()
        if d.is_dir()
        and not d.name.endswith("_separator")
        and d.name.lower() not in _RESERVED_STAGING_NAMES
    }

    # Parse existing modlist lines, dropping entries whose folder is gone.
    existing_lines: list[str] = []
    existing_names: set[str] = set()
    if modlist_path.exists():
        for line in modlist_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped[0] in ("+", "-", "*"):
                name = stripped[1:]
                # Keep separators always; only keep mods that exist on disk.
                if name.endswith("_separator") or name in on_disk:
                    existing_lines.append(stripped)
                    existing_names.add(name)
            else:
                existing_lines.append(stripped)

    new_mods = sorted(on_disk - existing_names)
    new_lines = [f"-{name}" for name in new_mods]

    all_lines = new_lines + existing_lines
    modlist_path.write_text(
        "\n".join(all_lines) + ("\n" if all_lines else ""), encoding="utf-8")

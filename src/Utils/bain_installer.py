"""
bain_installer.py
Stateless logic engine for BAIN (Wrye Bash bundled archive) installation.
No UI. detect_bain/resolve_bain_files mirror the role of fomod_installer.py.

Detection follows Wrye Bash's structural rule for a "complex" (type 2) package
rather than a numeric-prefix heuristic: a top-level folder is a sub-package if
it directly contains either a recognised data sub-folder (Meshes, Textures, …)
or a top-level data/doc file (.esp, .esm, .bsa, .ini, .txt, …). Folders that
Wrye Bash silently skips (``--`` prefix, ``bash``, ``omod conversion data``,
``wizard images``) don't count. See Mopy/bash/bosh/bain.py in the Wrye Bash
source (``_reset_cache`` / ``dataDirsPlus`` / ``_silentSkipsStart``).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Recognised Data sub-folders (Wrye Bash: Bain.data_dirs ∪ wrye_bash_data_dirs ∪
# screenshot_dirs ∪ {'docs'}). A top-level folder containing one of these as an
# immediate child behaves like a simple package, so its parent is a sub-package.
_DATA_DIRS = {
    # game asset dirs
    "meshes", "textures", "sound", "music", "video", "ini", "scripts",
    "interface", "strings", "shaders", "materials", "sdf", "trees", "facegen",
    "distantlod", "lodsettings", "menus", "fonts", "dialogueviews", "grass",
    "seq", "asi", "calientetools", "skse", "obse", "f4se", "nvse", "mwse",
    "source", "tools", "docs",
    # screenshots
    "screenshots", "screens", "ss",
    # wrye bash dirs
    "bash patches", "bashtags", "ini tweaks",
}

# Data/config file extensions (Wrye Bash: _top_files_extensions, WITHOUT docs).
# A loose file with one of these at the archive root makes it a *simple*
# package (type 1); inside a sub-folder it marks that folder as a sub-package.
_DATA_EXTS = {
    ".esp", ".esm", ".esu", ".esl", ".bsa", ".ba2", ".bsl", ".ckm", ".csv",
    ".ini", ".modgroups", ".toml",
}

# Documentation extensions (Wrye Bash: docExts). These count toward sub-package
# detection (_top_files_plus_docs) but a doc at the archive *root* must NOT make
# the package look like a simple/type-1 package — e.g. a Wrye Bash package.txt.
_DOC_EXTS = {
    ".txt", ".rtf", ".htm", ".html", ".doc", ".docx", ".odt", ".mht", ".pdf",
    ".css", ".xls", ".xlsx", ".ods", ".odp", ".ppt", ".pptx", ".md", ".rst",
    ".url",
}

# Extensions that mark a folder as a sub-package (data + docs).
_TOP_EXTS = _DATA_EXTS | _DOC_EXTS

# Folders/files Wrye Bash silently skips when classifying a package. Lower-case.
_SKIP_DIR_NAMES = {"bash", "omod conversion data", "wizard images"}
_SKIP_PREFIXES = ("--",)

# A sub-package folder name like "00 Core" / "01 Faction Integration" /
# "01a Variant" — Wrye Bash allows an optional letter suffix after the digits
# to group mutually-exclusive variants (01a, 01b, …). Used only to prettify the
# display name; it is not required for detection.
_NUMERIC_PREFIX_RE = re.compile(r"^(\d+)([a-zA-Z]*)[\s_.\-]")


@dataclass
class BainSubPackage:
    name: str            # full folder name, e.g. "00 Core"
    display_name: str    # prefix stripped, e.g. "Core"
    path: str            # absolute path to the folder
    default_selected: bool = True


def _strip_numeric_prefix(name: str) -> str:
    """'00 Core' -> 'Core'. Leaves names without a numeric prefix unchanged."""
    m = _NUMERIC_PREFIX_RE.match(name)
    if not m:
        return name
    return name[m.end():].strip() or name


def _is_skipped_dir(name: str) -> bool:
    low = name.lower()
    return low in _SKIP_DIR_NAMES or low.startswith(_SKIP_PREFIXES)


def _looks_like_subpackage(dir_path: str) -> bool:
    """True if *dir_path* behaves like a simple package: it directly contains a
    recognised data sub-folder or a top-level data/doc file."""
    try:
        with os.scandir(dir_path) as it:
            for e in it:
                if e.is_dir():
                    if e.name.lower() in _DATA_DIRS:
                        return True
                else:
                    ext = os.path.splitext(e.name)[1].lower()
                    if ext in _TOP_EXTS:
                        return True
    except OSError:
        return False
    return False


def detect_bain(extract_dir: str) -> list[BainSubPackage] | None:
    """Detect a BAIN complex package: a root holding ≥2 sub-package folders,
    each of which behaves like a simple package (contains data sub-folders or
    top-level data/doc files).

    Returns the ordered list of sub-packages, or ``None`` if the directory
    doesn't look like a complex BAIN package. The caller is expected to pass an
    already single-folder-unwrapped path.
    """
    try:
        entries = list(os.scandir(extract_dir))
    except OSError:
        return None

    # If the root itself holds loose data files or recognised data dirs, it's a
    # *simple* package (type 1), not a complex one — don't show a picker. Note:
    # doc files at the root (e.g. a Wrye Bash package.txt) do NOT count, matching
    # Wrye Bash's _re_top_extensions (which excludes docExts).
    for e in entries:
        if e.is_dir():
            if e.name.lower() in _DATA_DIRS:
                return None
        else:
            if os.path.splitext(e.name)[1].lower() in _DATA_EXTS:
                return None

    subpackages: list[BainSubPackage] = []
    for e in entries:
        if not e.is_dir() or _is_skipped_dir(e.name):
            continue
        if _looks_like_subpackage(e.path):
            subpackages.append(BainSubPackage(
                name=e.name,
                display_name=_strip_numeric_prefix(e.name),
                path=e.path,
            ))

    if len(subpackages) < 2:
        return None

    # Wrye Bash orders sub-packages alphabetically (dict_sort). Numbered
    # prefixes ("00", "01", "10") sort correctly as strings.
    subpackages.sort(key=lambda p: p.name.lower())
    return subpackages


def resolve_bain_files(subpackages: list[BainSubPackage],
                       selected_names: set[str]) -> list[tuple[str, str, bool]]:
    """Build the (src_rel, dst_rel, is_folder) install list for the chosen
    sub-packages.

    ``src_rel`` is relative to the extract root (e.g. ``00 Core/MWSE/x``);
    ``dst_rel`` strips the sub-package folder so chosen packages merge into a
    single namespace (e.g. ``MWSE/x``). Files are emitted in sub-package order
    so later packages overwrite earlier ones on conflict (BAIN semantics).
    """
    result: list[tuple[str, str, bool]] = []
    for pkg in subpackages:
        if pkg.name not in selected_names:
            continue
        root = pkg.path
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                abs_path = os.path.join(dirpath, fn)
                dst_rel = os.path.relpath(abs_path, root)
                src_rel = os.path.join(pkg.name, dst_rel)
                result.append((src_rel, dst_rel, False))
    return result

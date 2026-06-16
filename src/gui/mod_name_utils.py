"""
Mod name parsing: strip title metadata and suggest display names from filename stems.
Used by install_mod, dialogs (NameModDialog), and modlist_panel. No dependency on other gui modules.
"""

import re


# Characters Windows/Wine forbid in a path component.  Mods are deployed and
# read through Wine tools (xEdit, PGPatcher, BodySlide, …) and into Wine
# prefixes, so a folder name Wine can't address breaks those tools — and a
# trailing dot or space is silently stripped by Windows path normalisation,
# which makes the folder vanish from the tool's point of view.
_WINDOWS_RESERVED_CHARS = r'<>:"/\\|?*'


def sanitize_mod_folder_name(name: str) -> str:
    """Return *name* made safe for use as a Wine/Windows-addressable folder.

    - Strips characters Windows/Wine forbid in a path component.
    - Removes control characters.
    - Trims trailing dots and spaces (Windows path normalisation drops these,
      so a folder named "Foo." or "Foo " becomes unreachable to Wine tools).
    - Falls back to "Mod" if nothing usable remains.

    Leading/trailing whitespace is also trimmed.  This only affects the
    on-disk folder name; the user's chosen display name is unaffected
    elsewhere.
    """
    s = name.strip()
    # Drop reserved characters and ASCII control chars.
    s = re.sub(rf"[{re.escape(_WINDOWS_RESERVED_CHARS)}]", "", s)
    s = "".join(ch for ch in s if ord(ch) >= 32)
    # Windows strips trailing dots and spaces from each path component.
    s = s.rstrip(". ")
    # Reserved DOS device names (CON, PRN, NUL, COM1…) — extremely rare for a
    # mod name, but a folder so named is unusable under Wine.
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", s):
        s = s + "_"
    return s if s else "Mod"


def _strip_title_metadata(name: str) -> str:
    """
    Remove common metadata from a mod name: parenthesized/bracketed tags,
    version strings, underscores-as-spaces, Nexus remnant suffixes, and
    trailing noise.

    Examples:
        "SkyUI_5_2_SE"                    → "SkyUI"
        "All in one (all game versions)"  → "All in one"
        "Cool Mod (SE) v1.2.3"           → "Cool Mod"
        "My_Awesome_Mod_v2_0"            → "My Awesome Mod"
    """
    s = name

    # Strip residual Nexus-style suffix still containing alphanumeric version
    # parts (e.g. -12604-5-2SE that the strict numeric strip missed).
    s = re.sub(r"-\d{2,}(?:-[\w]+)*$", "", s)

    # Replace underscores with spaces (common in Nexus filenames)
    s = s.replace("_", " ")

    # Remove content in parentheses and square brackets (e.g. "(SE)", "[1.0]")
    s = re.sub(r"\s*\([^)]*\)", "", s)
    s = re.sub(r"\s*\[[^\]]*\]", "", s)

    # Remove trailing version-like patterns:  v1.2.3, V2.0, etc.
    s = re.sub(r"\s+[vV]\d+(?:[.\-]\w+)*\s*$", "", s)
    # Remove trailing dotted version:  1.0.0, 2.3.1
    s = re.sub(r"\s+\d+(?:\.\d+)+\s*$", "", s)

    # Remove trailing segments that are numeric or known edition/platform tags
    _EDITION_TAGS = r"(?:SE|AE|LE|VR|SSE|GOTY|HD|UHD)"
    s = re.sub(rf"(\s+(?:\d[\w]*|{_EDITION_TAGS})){{2,}}\s*$", "", s)
    s = re.sub(rf"\s+{_EDITION_TAGS}\s*$", "", s)
    s = re.sub(r"(?<=\d)\s+\d+\s*$", "", s)

    # Second pass for version patterns uncovered after stripping above
    s = re.sub(r"\s+[vV]\d+(?:[.\-]\w+)*\s*$", "", s)

    # Clean up any leftover dashes or whitespace at the edges
    s = re.sub(r"[\s\-]+$", "", s)
    s = re.sub(r"^[\s\-]+", "", s)

    return s if s else name


def _suggest_mod_names(filename_stem: str) -> list[str]:
    """
    Given a raw filename stem (no extension), return a list of name candidates
    for the install/rename dialog, **best (default) first**.

    Nexus Mods download names follow ``ModName-nexusid-version-timestamp``.
    The only suffix we strip for the *default* name is that Nexus tail — the
    title itself (including any parentheses, version, or descriptive tags the
    uploader chose) is preserved.  This mirrors Mod Organizer 2, whose
    name-guess regex treats ``( ) . -`` and spaces as legitimate mod-name
    characters and removes only the trailing id/version.

    The aggressively-cleaned name (parens/version/edition tags removed) is still
    offered as a *lower-priority* candidate so the rename dialog can suggest it,
    but it is no longer the default — too many real titles carry meaningful
    parentheses (Stardew framework tags "(CP)"/"(AT)", disambiguators like
    "(Black)" vs "(Silver)", etc.) that the old default silently destroyed.
    """
    # Step 1: strip duplicate-download suffix added by browsers/OS (e.g. " (1)", " (2)")
    stem = re.sub(r"\s*\(\d+\)\s*$", "", filename_stem).strip()

    # Step 2: strip the Nexus tail (-nexusid-version-timestamp).  Each segment is
    # a dash followed by digits and optional trailing letters (e.g. "-4a", "-2SE"
    # that Nexus appends for versioned uploads).  We require at least two such
    # segments so a single "-2" inside a real title (e.g. "Mod-2") is left alone.
    nexus_clean = re.sub(r"(?:-\d+[A-Za-z]*){2,}$", "", stem).strip()
    if nexus_clean == stem:
        # Fall back to the looser numeric-only strip for names with just one
        # trailing -digits segment (rare, but keep prior behaviour for those).
        nexus_clean = re.sub(r"(-\d+)+$", "", stem).strip()

    # Aggressively-cleaned variant: strip parens/brackets/version/edition tags.
    # Offered as a fallback candidate only — NOT the default (see docstring).
    title_clean = _strip_title_metadata(nexus_clean)

    # Build de-duplicated list, default (least-destructive) first.
    seen = set()
    result = []
    for candidate in (nexus_clean, title_clean, filename_stem):
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result

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


# New Nexus download format (rolled out 2026-06-11):
#   "<mod name>_<version>_<slug>"      e.g. "FDE_Senna_1.0.1_xDLPwGTYs"
#                                           "Canidae_-_A_Wolf_Replacer_1.3_587RqT2ex"
#                                           "Skyrim_Sewers_PL_1_FpnkHZi8m"  (version "1")
# Spaces in the title are encoded as underscores; <version> is a number that may
# be a plain integer ("1", "79"), dotted ("2.0.2"), or carry a trailing tag
# ("2.0.2BETA"); <slug> is a random base62 token Nexus appends per upload.
# Nexus has signalled they intend to drop the slug in a later change, leaving:
#   "<mod name>_<version>"             e.g. "FDE_Senna_1.0.1"
#
# Two anchors, because the version alone is too weak a signal:
#   * _WITH_SLUG: the trailing token IS a random slug (see ``_slug_like``), so we
#     trust the segment before it as the version and accept even a bare integer.
#     This is the live format and matches every real download.
#   * _NO_SLUG (future slug-less format): no random token to anchor on, so the
#     trailing "_<version>" is taken as the version even when it is a bare
#     integer ("Domain_Expansion_Release_V1_1" -> "Domain Expansion Release V1").
#     This can occasionally eat a real title number, so the un-stripped
#     ``filename_stem`` is still offered as a fallback candidate in the rename
#     dialog (see ``_suggest_mod_names``).
_NEXUS_SLUG = r"[A-Za-z0-9]{6,16}"
_NEW_NEXUS_WITH_SLUG_RE = re.compile(
    rf"^(?P<name>.+?)_(?P<ver>\d+(?:\.\d+)*[A-Za-z]*)_(?P<slug>{_NEXUS_SLUG})$"
)
_NEW_NEXUS_NO_SLUG_RE = re.compile(
    r"^(?P<name>.+?)_(?P<ver>\d+(?:\.\d+)*[A-Za-z]*)$"
)

# mod.io download names append a truncated-UUID tail: an underscore, the first
# two hex groups of the mod's UUID (``<8hex>-<4hex>``), then one or more short
# trailing groups (1-4 chars each) ending in a random token — e.g.
# ``bettercontainers_cb42bc3a-f1d2-afwl``, ``wingsunlocked_3d7eabb4-81bf-d9-bwww``,
# ``weightlessgold_81117bd5-de2f-a-du9y`` (note the 1-char ``a`` group).  The
# ``_<8hex>-<4hex>`` anchor is distinct from any Nexus tail, so matching is
# unambiguous and never touches Nexus names.
_MODIO_TAIL_RE = re.compile(r"_[0-9a-f]{8}-[0-9a-f]{4}(?:-[0-9a-z]{1,4})+$")


def _slug_like(token: str) -> bool:
    """True if *token* resembles a random Nexus upload slug (base62 noise)
    rather than a meaningful word an uploader might append after a version."""
    if not (6 <= len(token) <= 16):
        return False
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    return (has_digit and has_alpha) or (has_upper and has_lower)


def _strip_nexus_new_format(stem: str) -> str | None:
    """If *stem* matches the post-2026-06-11 Nexus download format
    ``<mod name>_<version>[_<slug>]``, return the decoded mod name (underscores
    → spaces).  Returns ``None`` when *stem* does not match, so callers can fall
    back to the legacy parsing untouched.
    """
    # Prefer the slug-anchored form: when the trailing token is a random Nexus
    # slug we can trust the segment before it as the version even if it is a
    # bare integer ("..._1_<slug>").
    m = _NEW_NEXUS_WITH_SLUG_RE.match(stem)
    if m and _slug_like(m.group("slug")):
        name = m.group("name").replace("_", " ").strip()
        return name or None

    # Future slug-less form ("<name>_<version>"): no random token to anchor on,
    # so the trailing "_<version>" is stripped even when it is a bare integer.
    # This may occasionally remove a real title number; the caller still offers
    # the un-stripped stem as a fallback candidate.
    m = _NEW_NEXUS_NO_SLUG_RE.match(stem)
    if m:
        name = m.group("name").replace("_", " ").strip()
        return name or None

    return None


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

    # Step 1b: strip the mod.io UUID-fragment tail (distinct from any Nexus
    # tail).  The cleaned name is the default; the raw stem stays as a fallback.
    modio_clean = _MODIO_TAIL_RE.sub("", stem)
    if modio_clean != stem:
        seen = set()
        result = []
        for candidate in (modio_clean, filename_stem):
            if candidate and candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        return result

    # Step 2: handle the post-2026-06-11 Nexus underscore format
    #   "<mod name>_<version>[_<slug>]".  When it matches, the decoded name is
    # the least-destructive (default) candidate.  Old-format downloads don't
    # match and fall through to the legacy dash-tail handling below unchanged.
    new_format = _strip_nexus_new_format(stem)
    if new_format:
        # Skip the dash-tail strip: the underscore format has no Nexus dash tail,
        # and the decoded name may legitimately contain dashes ("A - B").
        nexus_clean = new_format
        title_clean = _strip_title_metadata(nexus_clean)
        # The version was stripped to form the default.  Offer the
        # number-preserved name (underscores decoded) as a fallback so a real
        # title number eaten by the slug-less integer strip can be recovered.
        kept_number = stem.replace("_", " ").strip()
        seen = set()
        result = []
        for candidate in (nexus_clean, title_clean, kept_number, filename_stem):
            if candidate and candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        return result

    # Step 3: strip the legacy Nexus tail (-nexusid-version-timestamp).  Each segment is
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

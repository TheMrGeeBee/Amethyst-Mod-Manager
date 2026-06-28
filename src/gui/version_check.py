"""
App update check: fetch latest version from repo and compare.
Used by App. No dependency on other gui modules.
"""

import os
import re

from Utils.gh_cache import fetch_text as _gh_fetch_text

_APP_UPDATE_RELEASES_API_URL = "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/releases/latest"
_APP_UPDATE_RELEASES_LIST_API_URL = "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/releases?per_page=20"
_APP_UPDATE_RELEASES_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases"
_APP_UPDATE_INSTALLER_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/appimage/Amethyst-MM-installer.sh"
_AUR_API_URL = "https://aur.archlinux.org/rpc/v5/info/amethyst-mod-manager"
_AUR_PACKAGE_URL = "https://aur.archlinux.org/packages/amethyst-mod-manager"


def is_appimage() -> bool:
    """Return True if we are running inside an AppImage."""
    return bool(os.environ.get("APPIMAGE"))


def is_flatpak() -> bool:
    """Return True if we are running inside a Flatpak sandbox."""
    return os.path.exists("/.flatpak-info")


def _parse_version(s: str) -> tuple:
    """Parse a version string into a sortable tuple following SemVer pre-release rules.

    '1.3.1'         -> ((1, 3, 1), (1,))                # stable sorts last
    '1.3.1-beta.1'  -> ((1, 3, 1), (0, 'beta', 1))      # pre-release sorts before stable
    """
    s = s.strip().lstrip("v")
    if "-" in s:
        core, pre = s.split("-", 1)
    else:
        core, pre = s, ""
    nums = []
    for part in core.split("."):
        part = re.sub(r"[^0-9].*$", "", part)
        nums.append(int(part) if part.isdigit() else 0)
    if not pre:
        return (tuple(nums), (1,))
    pre_key: list = []
    for part in pre.split("."):
        pre_key.append(int(part) if part.isdigit() else part)
    return (tuple(nums), (0, *pre_key))


def _fetch_latest_version(
    allow_prerelease: bool = False,
    *,
    force: bool = False,
) -> tuple[str, bool] | None:
    """Return (tag, is_prerelease) of the highest applicable release, or None on error.

    With allow_prerelease=False, queries /releases/latest (stable-only).
    With allow_prerelease=True, lists recent releases and picks the highest non-draft
    by SemVer comparison — which may be either a stable or a pre-release.

    Uses ETag caching + a 1-hour throttle. Pass force=True to bypass the
    throttle (e.g. when the user manually toggles the pre-release channel).
    """
    import json
    try:
        if not allow_prerelease:
            raw = _gh_fetch_text(
                _APP_UPDATE_RELEASES_API_URL,
                timeout=10,
                min_interval=3600,
                force=force,
            )
            if raw is None:
                return None
            data = json.loads(raw)
            tag = data.get("tag_name", "").lstrip("v")
            return (tag, False) if tag else None

        raw = _gh_fetch_text(
            _APP_UPDATE_RELEASES_LIST_API_URL,
            timeout=10,
            min_interval=3600,
            force=force,
        )
        if raw is None:
            return None
        releases = json.loads(raw)
        candidates = [
            (r.get("tag_name", "").lstrip("v"), bool(r.get("prerelease", False)))
            for r in releases
            if not r.get("draft", False) and r.get("tag_name")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda tp: _parse_version(tp[0]), reverse=True)
        return candidates[0]
    except Exception:
        return None


def _fetch_aur_version(*, force: bool = False) -> str | None:
    """Fetch the current AUR package version; return None on error.

    The AUR version string includes a pkgrel suffix (e.g. '0.7.9-1').
    We strip everything from the first '-' onwards so callers get a plain
    version number comparable with __version__.

    Uses ETag caching + a 1-hour throttle (AUR supports conditional GETs too).
    """
    import json
    try:
        raw = _gh_fetch_text(
            _AUR_API_URL,
            accept="application/json",
            timeout=10,
            min_interval=3600,
            force=force,
        )
        if raw is None:
            return None
        data = json.loads(raw)
        results = data.get("results", [])
        if not results:
            return None
        ver = results[0].get("Version", "")
        # Strip pkgrel: '0.7.9-1' -> '0.7.9'
        ver = ver.split("-")[0]
        return ver if ver else None
    except Exception:
        return None


def _is_newer_version(current: str, latest: str) -> bool:
    """Return True if latest is newer than current (strictly greater)."""
    try:
        return _parse_version(latest) > _parse_version(current)
    except (ValueError, TypeError):
        return False


def _major_minor(s: str) -> tuple[int, int] | None:
    """Parse a version string and return (major, minor). Beta/pre-release suffix is ignored.

    '1.3'           -> (1, 3)
    '1.3.0'         -> (1, 3)
    '1.3.0-beta.3'  -> (1, 3)
    """
    if not s:
        return None
    try:
        core = s.strip().lstrip("v").split("-", 1)[0]
        parts = core.split(".")
        if len(parts) < 2:
            return None
        return (int(parts[0]), int(parts[1]))
    except (ValueError, AttributeError):
        return None


def _meets_min_app_version(min_ver: str, app_ver: str) -> bool:
    """Return True if app_ver satisfies a major.minor floor of min_ver.

    Beta builds satisfy the floor for their major.minor (e.g. 1.3.0-beta.2
    satisfies "1.3"). An empty/missing min_ver always returns True.
    """
    if not min_ver:
        return True
    floor = _major_minor(min_ver)
    have = _major_minor(app_ver)
    if floor is None or have is None:
        return True  # malformed → don't block
    return have >= floor

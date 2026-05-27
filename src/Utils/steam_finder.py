"""
steam_finder.py
Utilities for locating Steam game installations across all configured library paths.
No UI, no game-specific knowledge.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Known Steam base directories for different install methods
# ---------------------------------------------------------------------------
_HOME = Path.home()

_STEAM_CANDIDATES: list[Path] = [
    _HOME / ".local" / "share" / "Steam",                                          # Standard
    _HOME / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",  # Flatpak
    _HOME / "snap" / "steam" / "common" / ".local" / "share" / "Steam",            # Snap
    _HOME / ".steam" / "steam",                                                     # Symlink fallback
]

_VDF_FILENAME = "libraryfolders.vdf"
_COMMON_SUBDIR = Path("steamapps") / "common"


def _normalize_tool_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _proton_sort_key(name: str) -> tuple[int, tuple[int, ...], str]:
    """
    Sort key for Proton tool names.

    Priority:
      1) GE-Proton builds before Valve Proton builds
      2) Higher numeric versions first
      3) Name as a final tie-breaker
    """
    lower = name.lower()
    is_ge = lower.startswith("ge-proton")
    nums = tuple(int(n) for n in re.findall(r"\d+", lower))
    return (0 if is_ge else 1, tuple(-n for n in nums), lower)


def list_installed_proton() -> list[Path]:
    """Return all installed Proton launcher scripts, sorted by _proton_sort_key.

    Deduplicates by resolved path so symlinked Steam roots (e.g. ~/.steam/steam)
    don't produce duplicate entries.
    """
    seen: set[Path] = set()
    candidates: list[Path] = []
    for steam_root in _all_proton_search_roots():
        for search_dir in (
            steam_root / "compatibilitytools.d",
            steam_root / "steamapps" / "common",
        ):
            if not search_dir.is_dir():
                continue
            try:
                for entry in search_dir.iterdir():
                    if not entry.is_dir():
                        continue
                    proton_script = entry / "proton"
                    if not proton_script.is_file():
                        continue
                    resolved = proton_script.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    candidates.append(proton_script)
            except OSError:
                continue
    candidates.sort(key=lambda p: _proton_sort_key(p.parent.name))
    return candidates


def find_any_installed_proton(preferred_name: str = "") -> Path | None:
    """
    Find an installed Proton launcher script without requiring a Steam app mapping.

    This is used as a fallback for custom prefixes where Steam does not maintain
    a CompatToolMapping entry for the selected app ID.

    Args:
        preferred_name: Optional Proton tool directory name to prefer,
                        e.g. "GE-Proton10-28".
    """
    preferred_norm = _normalize_tool_name(preferred_name) if preferred_name else ""

    candidates: list[Path] = []
    seen: set[Path] = set()
    for steam_root in _all_proton_search_roots():
        for search_dir in (
            steam_root / "compatibilitytools.d",
            steam_root / "steamapps" / "common",
        ):
            if not search_dir.is_dir():
                continue
            try:
                for entry in search_dir.iterdir():
                    if not entry.is_dir():
                        continue
                    proton_script = entry / "proton"
                    if not proton_script.is_file():
                        continue
                    resolved = proton_script.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    candidates.append(proton_script)
            except OSError:
                continue

    if not candidates:
        return None

    if preferred_norm:
        for candidate in candidates:
            if _normalize_tool_name(candidate.parent.name) == preferred_norm:
                return candidate

    proton_like = [
        c for c in candidates
        if c.parent.name.lower().startswith(("proton", "ge-proton"))
    ]
    if not proton_like:
        proton_like = candidates

    proton_like.sort(key=lambda p: _proton_sort_key(p.parent.name))
    return proton_like[0]


def find_steam_root_for_proton_script(proton_script: Path) -> Path | None:
    """
    Resolve the Steam client root to use as STEAM_COMPAT_CLIENT_INSTALL_PATH
    for a given Proton script.

    Supports Proton installs from both:
      - <steam_root>/steamapps/common/<Tool>/proton
      - <steam_root>/compatibilitytools.d/<Tool>/proton

    When the Proton tool lives in a *secondary* library (SD card, second
    drive), that library is not a real Steam client install — Proton needs the
    runtime that lives under the main Steam root. In that case we fall back to
    the first real Steam client candidate so the runtime is still found.
    """
    script = proton_script.resolve()

    # Prefer a genuine Steam client root that owns this script directly.
    for steam_root in _STEAM_CANDIDATES:
        try:
            rel = script.relative_to(steam_root.resolve())
        except Exception:
            continue

        if len(rel.parts) < 2:
            continue

        if rel.parts[0] == "steamapps" and len(rel.parts) >= 4 and rel.parts[1] == "common":
            return steam_root
        if rel.parts[0] == "compatibilitytools.d" and len(rel.parts) >= 3:
            return steam_root

    # The script lives outside every known client root (e.g. a secondary
    # library). Use the first real Steam client install for the runtime.
    for steam_root in _STEAM_CANDIDATES:
        if (steam_root / "steamapps").is_dir():
            return steam_root

    # Last resort: derive a root from the path itself.
    parts = script.parts
    try:
        idx = parts.index("compatibilitytools.d")
        if idx > 0:
            return Path(*parts[:idx])
    except ValueError:
        pass

    try:
        idx = parts.index("steamapps")
        if idx > 0:
            return Path(*parts[:idx])
    except ValueError:
        pass

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_steam_libraries() -> list[Path]:
    """
    Parse libraryfolders.vdf from all known Steam install locations.
    Returns a deduplicated list of existing steamapps/common/ directories.
    """
    seen: set[Path] = set()
    libraries: list[Path] = []

    vdf_candidates: list[Path] = []

    # User-configured VDF path takes highest priority
    try:
        from Utils.ui_config import load_steam_libraries_vdf_path
        custom = load_steam_libraries_vdf_path()
        if custom:
            vdf_candidates.append(Path(custom))
    except Exception:
        pass

    # Built-in fallbacks
    for steam_root in _STEAM_CANDIDATES:
        vdf_candidates.append(steam_root / "steamapps" / _VDF_FILENAME)

    for vdf_path in vdf_candidates:
        if vdf_path.is_file():
            for common in parse_vdf_libraries(vdf_path):
                resolved = common.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    libraries.append(common)

    return libraries


# Warn-once tracking so the same library path doesn't spam the log every time
# find_steam_libraries() runs (it's called frequently by GUI refreshes).
_vdf_warned_missing: set[str] = set()
_vdf_warned_readonly: set[str] = set()


def _warn_vdf_library(raw: str, common: Path) -> None:
    """Log a one-time warning for a library that is listed in VDF but
    unusable (drive unmounted, read-only, etc.). Best-effort — swallows
    import errors so this module stays UI-free.
    """
    try:
        from Utils.app_log import app_log
    except Exception:
        return

    if not common.is_dir():
        if raw not in _vdf_warned_missing:
            _vdf_warned_missing.add(raw)
            app_log(
                f"Steam library listed in libraryfolders.vdf but not accessible: "
                f"{raw} — drive may be unmounted or disconnected"
            )
        return

    # Directory exists; check writability. Deploy + install need write access
    # to the steamapps/common path (and the parent for compatdata etc.).
    if not os.access(str(common), os.W_OK):
        if raw not in _vdf_warned_readonly:
            _vdf_warned_readonly.add(raw)
            app_log(
                f"Steam library is read-only: {common} — mod deployment to games "
                f"in this library will fail until the mount is remounted writable"
            )


def parse_vdf_libraries(vdf_path: Path) -> list[Path]:
    """
    Parse a libraryfolders.vdf file and return all steamapps/common paths
    that currently exist on disk.

    The VDF format contains lines like:
        "path"    "/home/user/.local/share/Steam"
    We extract every "path" value and append steamapps/common to each.
    The Steam root containing the VDF is always included as the first entry.

    Libraries listed in the VDF but currently inaccessible (drive unmounted,
    disconnected USB, NAS offline) or mounted read-only are logged via
    app_log the first time we see them, so users get a clue when a game
    that used to work suddenly "vanishes" from the library list.
    """
    libraries: list[Path] = []
    pattern = re.compile(r'"path"\s+"([^"]+)"')

    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libraries

    for match in pattern.finditer(text):
        raw = match.group(1)
        common = Path(raw) / "steamapps" / "common"
        _warn_vdf_library(raw, common)
        if common.is_dir():
            libraries.append(common)

    return libraries


def _all_proton_search_roots() -> list[Path]:
    """Every directory that may hold a Proton install: the known Steam roots
    plus the steamapps/common of each extra library listed in libraryfolders.vdf.

    Proton tools live alongside games, so a tool selected via CompatToolMapping
    can sit in a secondary library (SD card, second drive) even though the
    config.vdf that names it only exists in the main Steam root.
    """
    roots: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            resolved = p.resolve()
        except Exception:
            resolved = p
        if resolved not in seen:
            seen.add(resolved)
            roots.append(p)

    for steam_root in _STEAM_CANDIDATES:
        _add(steam_root)

    for steam_root in _STEAM_CANDIDATES:
        vdf_path = steam_root / "steamapps" / _VDF_FILENAME
        if not vdf_path.is_file():
            continue
        for common in parse_vdf_libraries(vdf_path):
            # common is .../steamapps/common; the Steam-root-equivalent is its grandparent
            _add(common.parent.parent)

    return roots


def find_prefix(steam_id: str) -> Path | None:
    """
    Locate the Steam compatibility prefix directory for a given App ID.

    Steam stores per-game Proton prefixes under:
        <steam_root>/steamapps/compatdata/<steam_id>/pfx/

    Searches every known Steam root candidate first, then falls back to
    extra library folders parsed from libraryfolders.vdf (e.g. SD card or
    secondary drive libraries), since compatdata lives alongside the game.

    Args:
        steam_id: The Steam App ID as a string, e.g. '377160' for Fallout 4.
    """
    if not steam_id:
        return None

    def _check_compatdata(compatdata: Path) -> Path | None:
        """Return the prefix dir for a compatdata/<id> folder, or None."""
        pfx = compatdata / "pfx"
        if pfx.is_dir():
            return pfx
        if (compatdata / "drive_c").is_dir():
            return compatdata
        return None

    # Primary: check known Steam root candidates
    for steam_root in _STEAM_CANDIDATES:
        result = _check_compatdata(steam_root / "steamapps" / "compatdata" / steam_id)
        if result:
            return result

    # Secondary: check extra library folders (SD card, secondary drives, etc.)
    # parse_vdf_libraries returns steamapps/common paths; parent is steamapps/
    seen: set[Path] = set()
    for steam_root in _STEAM_CANDIDATES:
        vdf_path = steam_root / "steamapps" / _VDF_FILENAME
        if not vdf_path.is_file():
            continue
        for common in parse_vdf_libraries(vdf_path):
            steamapps = common.parent
            resolved = steamapps.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            result = _check_compatdata(steamapps / "compatdata" / steam_id)
            if result:
                return result

    return None


def find_proton_for_game(steam_id: str) -> Path | None:
    """
    Find the Proton launcher script assigned to a Steam game.

    Reads CompatToolMapping from Steam's config files to determine which Proton
    version the game uses, then locates the 'proton' script in steamapps/common/.

    Steam rewrites config.vdf atomically, so the live file may temporarily lack
    CompatToolMapping — we also check .bak and .tmp variants of the file.

    Returns the path to the 'proton' script, or None if the game's assigned
    Proton cannot be found (never falls back to an arbitrary version).

    The returned path can be used to run a Windows exe via:
        STEAM_COMPAT_DATA_PATH=<pfx_parent>
        STEAM_COMPAT_CLIENT_INSTALL_PATH=<steam_root>
        python3 <proton_script> run <exe_path>
    """
    import re as _re
    import glob as _glob

    _COMPAT_TOOL_NAMES: dict[str, str] = {
        "proton_experimental": "Proton - Experimental",
        "proton_hotfix":       "Proton Hotfix",
        "proton_10":           "Proton 10.0",
        "proton_9":            "Proton 9.0 (Beta)",
        "proton_8":            "Proton 8.0",
        "proton_7":            "Proton 7.0",
    }

    _ID_PATTERN = _re.compile(
        r'"' + _re.escape(steam_id) + r'"\s*\{[^}]*?"name"\s*"([^"]+)"',
        _re.DOTALL,
    )

    if not steam_id:
        return None

    for steam_root in _STEAM_CANDIDATES:
        config_dir = steam_root / "config"
        if not config_dir.is_dir():
            continue

        # Collect all config.vdf variants: live, .bak, and any .tmp files.
        # Steam writes atomically so the live file may be mid-swap.
        candidates_vdf: list[Path] = []
        for pattern in ("config.vdf", "config.vdf.bak", "config.vdf.*.tmp"):
            candidates_vdf.extend(
                Path(p) for p in _glob.glob(str(config_dir / pattern))
            )

        tool_name: str | None = None
        for vdf_path in candidates_vdf:
            try:
                text = vdf_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Only search inside the CompatToolMapping block
            compat_idx = text.find("CompatToolMapping")
            if compat_idx < 0:
                continue
            m = _ID_PATTERN.search(text, compat_idx)
            if m:
                tool_name = m.group(1)
                break

        if tool_name is None:
            continue

        # Map internal short names to steamapps/common directory names
        dir_name = _COMPAT_TOOL_NAMES.get(tool_name, tool_name)

        # Search steamapps/common/ and compatibilitytools.d/ (GE-Proton, etc.)
        # across every Steam root AND secondary library — Proton tools live
        # alongside games, so the mapped tool may sit on an SD card / second
        # drive even though config.vdf only exists in the main Steam root.
        search_dirs: list[Path] = []
        for root in _all_proton_search_roots():
            search_dirs.append(root / "steamapps" / "common")
            search_dirs.append(root / "compatibilitytools.d")

        for search_dir in search_dirs:
            # Exact match first
            candidate = search_dir / dir_name / "proton"
            if candidate.is_file():
                return candidate

            # Case-insensitive match (handles minor name variations)
            if search_dir.is_dir():
                dir_lower = dir_name.lower()
                for entry in search_dir.iterdir():
                    if entry.name.lower() == dir_lower:
                        p = entry / "proton"
                        if p.is_file():
                            return p

    # --- Fallback: read compatdata/<steam_id>/config_info ----------------
    # When Steam uses the default Proton for a game it may not write an
    # entry to CompatToolMapping.  The compatdata directory, however,
    # stores a config_info file whose lines include the Proton path used
    # to create the prefix (e.g. ".../common/Proton 10.0/files/...").
    # We extract the Proton directory name from that path.
    for steam_root in _all_proton_search_roots():
        config_info = (steam_root / "steamapps" / "compatdata"
                       / steam_id / "config_info")
        if not config_info.is_file():
            continue
        try:
            lines = config_info.read_text(encoding="utf-8",
                                          errors="replace").splitlines()
        except OSError:
            continue
        # Check for Proton paths in both steamapps/common/ and
        # compatibilitytools.d/ (GE-Proton, custom builds, etc.)
        _PROTON_PATH_MARKERS = [
            "/steamapps/common/",
            "/compatibilitytools.d/",
        ]
        for line in lines:
            for marker in _PROTON_PATH_MARKERS:
                if marker not in line:
                    continue
                idx = line.find(marker)
                after = line[idx + len(marker):]
                proton_dir_name = after.split("/")[0]
                if not proton_dir_name.lower().startswith(("proton", "ge-proton")):
                    continue
                # Reconstruct the parent directory from the marker
                parent_dir = Path(line[:idx + len(marker)].rstrip("/"))
                candidate = parent_dir / proton_dir_name / "proton"
                if candidate.is_file():
                    return candidate

    return None


def _parse_acf_installdir(acf_path: Path) -> str | None:
    """Parse installdir from a Steam appmanifest_*.acf file. Returns None if not found."""
    installdir_pattern = re.compile(r'"installdir"\s+"([^"]+)"')
    try:
        text = acf_path.read_text(encoding="utf-8", errors="replace")
        m = installdir_pattern.search(text)
        return m.group(1) if m else None
    except OSError:
        return None


def find_game_by_steam_id(
    libraries: list[Path], steam_id: str, exe_name: str
) -> Path | None:
    """
    Locate a game's install folder using its Steam App ID and appmanifest.

    When multiple games share the same exe name (e.g. Enderal and Enderal SE both
    use "Enderal Launcher.exe"), the exe-based search returns the first match.
    This function uses Steam's app manifest to find the exact folder for the
    given steam_id, then verifies the exe is present.

    Returns the game root directory or None if not found.
    """
    if not steam_id:
        return None

    exe_lower = exe_name.lower()
    has_subdir = "/" in exe_name or "\\" in exe_name

    for common in libraries:
        steamapps = common.parent  # steamapps/common -> steamapps
        acf = steamapps / f"appmanifest_{steam_id}.acf"
        if not acf.is_file():
            continue
        installdir = _parse_acf_installdir(acf)
        if not installdir:
            continue
        game_dir = common / installdir
        if not game_dir.is_dir():
            continue
        if has_subdir:
            candidate = game_dir / exe_name
            if candidate.is_file():
                return game_dir
            parts = exe_lower.replace("\\", "/").split("/")
            cur = game_dir
            for part in parts:
                match = None
                try:
                    for entry in cur.iterdir():
                        if entry.name.lower() == part:
                            match = entry
                            break
                except PermissionError:
                    break
                if match is None:
                    break
                cur = match
            else:
                if cur.is_file():
                    return game_dir
        else:
            try:
                for entry in game_dir.iterdir():
                    if entry.name.lower() == exe_lower and entry.is_file():
                        return game_dir
            except PermissionError:
                pass

    return None


def find_game_in_libraries(libraries: list[Path], exe_name: str) -> Path | None:
    """
    Search each library's steamapps/common/* subfolder for exe_name.
    Returns the game root directory (the <GameFolder>) or None if not found.

    exe_name may be a bare filename (e.g. "SkyrimSE.exe") — searched one
    level deep — or a relative path with subdirectories (e.g. "bin/bg3.exe")
    which is checked as an exact relative path under each game folder.

    The search is case-insensitive on the exe name to handle Linux/Proton layouts.
    """
    exe_lower = exe_name.lower()
    has_subdir = "/" in exe_name or "\\" in exe_name

    for common in libraries:
        try:
            for game_dir in common.iterdir():
                if not game_dir.is_dir():
                    continue
                if has_subdir:
                    # Check as a relative path: <GameFolder>/bin/bg3.exe
                    candidate = game_dir / exe_name
                    if candidate.is_file():
                        return game_dir
                    # Case-insensitive fallback: walk the subpath segments
                    parts = exe_lower.replace("\\", "/").split("/")
                    cur = game_dir
                    for part in parts:
                        match = None
                        try:
                            for entry in cur.iterdir():
                                if entry.name.lower() == part:
                                    match = entry
                                    break
                        except PermissionError:
                            break
                        if match is None:
                            break
                        cur = match
                    else:
                        if cur.is_file():
                            return game_dir
                else:
                    for entry in game_dir.iterdir():
                        if entry.name.lower() == exe_lower and entry.is_file():
                            return game_dir
        except PermissionError:
            continue

    return None


def find_wine() -> tuple[str, Path]:
    """
    Finds a wine binary and the Proton root.
    This will first search for wine64 (Proton 9, 10), then fallback to wine (Proton 11+).
    Returns (wine_path_str, proton_files_dir).
    """
    proton_script = find_any_installed_proton()
    if proton_script is None:
        raise RuntimeError("No Proton/Wine installation found. Install proton via Steam.")
    files_dir = proton_script.parent / "files"
    wine = files_dir / "bin" / "wine64"
    if not wine.is_file():
        wine = files_dir / "bin" / "wine"
        if not wine.is_file():
            raise RuntimeError(f"wine/wine64 not found at expected path in {files_dir / 'bin'}")
    return str(wine), files_dir

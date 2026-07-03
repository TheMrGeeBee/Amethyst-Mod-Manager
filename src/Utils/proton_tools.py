"""Toolkit-neutral Proton tools — env resolution, wine-tool launching and the
dependency installers (VC++, d3dcompiler_47, .NET).

Single source of truth for both the Tk Proton-tools panel
(gui/dialogs.py:ProtonToolsPanel) and the Qt Proton dropdown
(gui_qt/app.py). All functions take a ``game`` object and a ``log_fn``; none
touch any GUI toolkit. Ported verbatim from the Tk panel so behaviour stays
identical across both front-ends.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

from Utils.steam_finder import proton_run_command

LogFn = Callable[[str], None]


# --- .NET desktop-runtime versions offered by the installer ----------------
# (label shown in the menu → exact runtime version → download URL).
DOTNET_VERSIONS: list[str] = ["5", "6", "7", "8", "9", "10"]

DOTNET_URLS: dict[str, str] = {
    "5":  "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/5.0.17/windowsdesktop-runtime-5.0.17-win-x64.exe",
    "6":  "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/6.0.36/windowsdesktop-runtime-6.0.36-win-x64.exe",
    "7":  "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/7.0.20/windowsdesktop-runtime-7.0.20-win-x64.exe",
    "8":  "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/8.0.25/windowsdesktop-runtime-8.0.25-win-x64.exe",
    "9":  "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/9.0.14/windowsdesktop-runtime-9.0.14-win-x64.exe",
    "10": "https://builds.dotnet.microsoft.com/dotnet/WindowsDesktop/10.0.5/windowsdesktop-runtime-10.0.5-win-x64.exe",
}


def _noop(_msg: str) -> None:
    pass


# --- shared .NET desktop-runtime installer ---------------------------------
# Exit codes from Microsoft's windowsdesktop-runtime bundle under proton/wine:
#   0    = installed successfully
#   102  = already installed / no-op
#   1638 = another (newer) version already present
#   3010 = installed, reboot required
#   1    = ambiguous under Wine but ~always means the runtime is already present
#          and the bundle declined to reinstall — treat as done, not a failure.
DOTNET_OK_CODES: frozenset[int] = frozenset({0, 102, 1638, 3010, 1})


def install_dotnet_runtime(
    version: str,
    proton_script: "Path",
    env: dict,
    prefix_path: "Path | None",
    *,
    log_fn: LogFn = _noop,
    status_fn: "Callable[[str], None] | None" = None,
    dep_key: "str | None" = None,
) -> bool:
    """Download (cached) + silently install the .NET desktop runtime *version*
    into the prefix behind *proton_script*/*env*.

    Single source of truth shared by the Proton dropdown and every wizard that
    needs .NET: given an already-resolved proton script + env + target prefix,
    it caches the official installer, runs it via ``proton run`` and records
    success in the prefix's dep marker. Returns ``True`` on success (including
    the already-installed exit codes in :data:`DOTNET_OK_CODES`).

    *status_fn* (optional) receives short user-facing status strings; *log_fn*
    receives detailed log lines.
    """
    from Utils.ca_bundle import download_file
    from Utils.config_paths import get_dotnet_cache_dir
    from Utils.protontricks import dotnet_dep_key, mark_dep_installed

    _status = status_fn or (lambda _m: None)

    dl_url = DOTNET_URLS.get(version)
    if dl_url is None:
        log_fn(f"no download URL known for .NET {version}.")
        return False

    cache_path = get_dotnet_cache_dir() / f"windowsdesktop-runtime-{version}-win-x64.exe"
    if not cache_path.is_file():
        _status(f"Downloading .NET {version} runtime…")
        log_fn(f"downloading .NET {version} runtime …")
        download_file(dl_url, cache_path)
        log_fn(f".NET {version} download complete.")
    else:
        log_fn(f"using cached .NET {version} installer.")

    _status(f"Installing .NET {version} (silent)…\n(this may take a few minutes)")
    log_fn(f"installing .NET {version} in prefix (silent) …")
    proc = subprocess.run(
        proton_run_command(proton_script, "run",
                           str(cache_path), "/quiet", "/norestart"),
        env=env, cwd=str(cache_path.parent),
    )
    if proc.returncode not in DOTNET_OK_CODES:
        log_fn(f".NET {version} installer exited with code {proc.returncode}.")
        return False

    if prefix_path and Path(prefix_path).is_dir():
        mark_dep_installed(Path(prefix_path), dep_key or dotnet_dep_key(version))

    if proc.returncode == 1:
        _status(f".NET {version} already installed — continuing.")
        log_fn(f".NET {version} already installed (installer exit 1) — marking done.")
    else:
        _status(f".NET {version} installed successfully.")
        log_fn(f".NET {version} installed (exit {proc.returncode}).")
    return True


# ---------------------------------------------------------------------------
# Proton environment resolution
# ---------------------------------------------------------------------------
def resolve_proton_env(game, log_fn: LogFn = _noop):
    """Resolve ``(proton_script, env)`` for *game*'s configured prefix.

    Returns ``(None, None)`` (after logging why) if no prefix / Proton tool /
    Steam root can be found. Mirrors the Tk panel's ``_get_proton_env``.
    """
    from Utils.steam_finder import (
        find_any_installed_proton,
        find_proton_for_game,
        game_steam_id,
        find_steam_root_for_proton_script,
    )

    prefix_path = game.get_prefix_path()
    if prefix_path is None or not prefix_path.is_dir():
        log_fn("Proton Tools: prefix not configured for this game.")
        return None, None

    steam_id = game_steam_id(game)
    proton_script = find_proton_for_game(steam_id) if steam_id else None

    from Utils.proton_prefix import resolve_compat_data, read_prefix_runner
    compat_data = resolve_compat_data(prefix_path)

    if proton_script is None:
        # Heroic-managed prefixes have no Steam CompatToolMapping, but the
        # exact Proton build is recorded in GamesConfig/<app>.json — use it.
        try:
            from Utils.heroic_finder import find_heroic_proton_for_prefix
            proton_script = find_heroic_proton_for_prefix(prefix_path)
        except Exception:
            proton_script = None
        if proton_script is not None:
            log_fn(f"Proton Tools: using Heroic-configured Proton "
                   f"{proton_script.parent.name}.")

    if proton_script is None:
        preferred_runner = read_prefix_runner(compat_data)
        proton_script = find_any_installed_proton(preferred_runner)
        if proton_script is None:
            if steam_id:
                log_fn(f"Proton Tools: could not find Proton version for app {steam_id}, "
                       "and no installed Proton tool was found.")
            else:
                log_fn("Proton Tools: no Steam ID and no installed Proton tool was found.")
            return None, None
        log_fn(f"Proton Tools: using fallback Proton tool {proton_script.parent.name} "
               "(no per-game Steam mapping found).")

    steam_root = find_steam_root_for_proton_script(proton_script)
    if steam_root is None:
        log_fn("Proton Tools: could not determine Steam root for the selected Proton tool.")
        return None, None

    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path:
        env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)
    if steam_id:
        env.setdefault("SteamAppId", steam_id)
        env.setdefault("SteamGameId", steam_id)
    return proton_script, env


def wine_tool_command(game, proton_script, env, tool: str, log_fn: LogFn = _noop):
    """Build a launch command for a bundled wine tool (winecfg/regedit).

    Prefers the Proton dist's bundled ``wine64`` binary directly (the way
    Heroic launches these), which avoids booting Proton's steam.exe shim that
    aborts when it can't reach a Steam client. Falls back to ``proton run``.
    Mutates *env* (sets WINEPREFIX) and returns the argv list.
    """
    proton_dir = Path(proton_script).parent
    log_fn(f"Proton Tools: resolving wine binary under {proton_dir}")
    if not proton_dir.is_dir():
        log_fn(f"Proton Tools: WARNING — Proton dir does not exist: {proton_dir}")
    wine_bin = None
    checked = []
    # Prefer wine64: GE-Proton's `wine` is a 32-bit ELF whose missing
    # /lib/ld-linux.so.2 interpreter makes exec fail with ENOENT on hosts
    # without 32-bit glibc.
    for sub in ("files/bin/wine64", "dist/bin/wine64",
                "files/bin/wine", "dist/bin/wine"):
        cand = proton_dir / sub
        checked.append(str(cand))
        if cand.is_file():
            wine_bin = cand
            break
    if wine_bin is None:
        log_fn("Proton Tools: WARNING — no bundled wine binary found "
               f"(checked: {', '.join(checked)}); falling back to "
               "'proton run', which boots the steam.exe shim and may crash "
               "with an lsteamclient assertion if Steam is unavailable.")
        return proton_run_command(proton_script, "run", tool)
    log_fn(f"Proton Tools: using bundled wine binary {wine_bin}")
    prefix_path = game.get_prefix_path()
    if prefix_path is not None:
        env["WINEPREFIX"] = str(prefix_path)
        log_fn(f"Proton Tools: WINEPREFIX set to {prefix_path}")
        if not prefix_path.is_dir():
            log_fn(f"Proton Tools: WARNING — WINEPREFIX path does not exist: {prefix_path}")
    else:
        log_fn("Proton Tools: WARNING — no prefix path for this game; "
               "wine will use its default prefix (~/.wine).")
    return [str(wine_bin), tool]


# ---------------------------------------------------------------------------
# Prefix tool launchers (fire-and-forget GUIs)
# ---------------------------------------------------------------------------
def launch_wine_tool(game, tool: str, log_fn: LogFn = _noop) -> bool:
    """Launch a bundled wine tool (``winecfg`` / ``regedit``). Returns False if
    the prefix/Proton couldn't be resolved."""
    proton_script, env = resolve_proton_env(game, log_fn)
    if proton_script is None:
        return False
    cmd = wine_tool_command(game, proton_script, env, tool, log_fn)
    log_fn(f"Proton Tools: launching {tool} …")
    try:
        subprocess.Popen(cmd, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log_fn(f"Proton Tools error: {e}")
        return False


def launch_winetricks(game, log_fn: LogFn = _noop) -> None:
    """Download winetricks/cabextract if needed, then launch the winetricks GUI
    against the game's prefix. Blocking on the (small) downloads — call from a
    worker thread."""
    from Utils.protontricks import (
        _bundled_winetricks,
        _get_proton_bin,
        cabextract_installed,
        install_cabextract,
        install_winetricks,
        winetricks_installed,
    )

    prefix_path = game.get_prefix_path()
    if prefix_path is None or not prefix_path.is_dir():
        log_fn("Proton Tools: prefix not configured for this game — cannot launch winetricks.")
        return

    if not winetricks_installed():
        log_fn("Proton Tools: winetricks not found — downloading …")
        if not install_winetricks(log_fn=lambda m: log_fn(f"Proton Tools: {m}")):
            return
    if not cabextract_installed():
        log_fn("Proton Tools: cabextract not found — downloading a portable copy …")
        if not install_cabextract(log_fn=lambda m: log_fn(f"Proton Tools: {m}")):
            return
    wt = _bundled_winetricks()
    env = os.environ.copy()
    env["WINEPREFIX"] = str(prefix_path)
    path_prefix = str(wt.parent)
    proton_bin = _get_proton_bin()
    if proton_bin:
        path_prefix = proton_bin + os.pathsep + path_prefix
    env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
    log_fn(f"Proton Tools: launching winetricks GUI against {prefix_path} …")
    try:
        subprocess.Popen([str(wt), "--gui"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except Exception as e:
        log_fn(f"Proton Tools error: {e}")


def launch_exe_in_prefix(game, exe_path, log_fn: LogFn = _noop) -> bool:
    """Run an arbitrary .exe inside the game's prefix via ``proton run``."""
    proton_script, env = resolve_proton_env(game, log_fn)
    if proton_script is None:
        return False
    exe_path = Path(exe_path)
    if not exe_path.is_file():
        log_fn(f"Proton Tools: file not found: {exe_path}")
        return False
    log_fn(f"Proton Tools: launching {exe_path.name} via {proton_script.parent.name} …")
    try:
        subprocess.Popen(proton_run_command(proton_script, "run", str(exe_path)),
                         env=env, cwd=exe_path.parent,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log_fn(f"Proton Tools error: {e}")
        return False


# ---------------------------------------------------------------------------
# Installers (run on a worker thread; return True on success)
# ---------------------------------------------------------------------------
def install_vcredist(game, log_fn: LogFn = _noop) -> bool:
    proton_script, env = resolve_proton_env(game, log_fn)
    if proton_script is None:
        return False
    prefix_path = getattr(game, "_prefix_path", None)
    from Utils.protontricks import install_vcredist as _impl
    return bool(_impl(proton_script, env, log_fn=log_fn, prefix_path=prefix_path))


def install_d3dcompiler_47(game, log_fn: LogFn = _noop) -> bool:
    from Utils.protontricks import install_d3dcompiler_47 as _impl
    from Utils.steam_finder import game_steam_id
    steam_id = game_steam_id(game)
    prefix_path = getattr(game, "_prefix_path", None)
    return bool(_impl(steam_id, log_fn=log_fn, prefix_path=prefix_path))


def install_dotnet(game, version: str, log_fn: LogFn = _noop) -> bool:
    """Download (cached) + silently install the .NET desktop runtime *version*
    into the game's prefix. Mirrors the Tk panel's ``_run_install_dotnet``
    worker. Thin wrapper over :func:`install_dotnet_runtime`."""
    proton_script, env = resolve_proton_env(game, log_fn)
    if proton_script is None:
        return False
    prefix_path = getattr(game, "_prefix_path", None)
    try:
        return install_dotnet_runtime(
            version, proton_script, env, prefix_path, log_fn=log_fn)
    except Exception as e:
        log_fn(f"Error: {e}")
        return False

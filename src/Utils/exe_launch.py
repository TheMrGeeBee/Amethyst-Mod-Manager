"""
Toolkit-neutral executable launch logic for the play bar.

Ports the persistence + launch dispatch out of the Tk exe launcher
(gui/plugin_panel_exe_launcher.py, gui/dialogs.py, wizards/_proton_prefix.py)
so the Qt GUI can use it without importing tkinter. File formats and paths are
identical to the Tk app so settings are shared between both:

- <staging>.parent/Applications/custom_exes.json        — manual exe list
- ~/.config/AmethystModManager/games/<game>/exe_launch_mode.json
      exe_name → "auto"|"steam"|"heroic"|"none"
      "__deploy_before_launch" → bool (default True)
      "__proton_override_<exe>" → Proton dir name ('' = game default)
      "__launch_options_<exe>" → Steam-style launch options string
- exe_args.json (global, or per-profile for profile-specific-mods profiles)
      exe_name → argument string

Launch entry points (launch_game / launch_exe_via_proton) are synchronous and
must be called from a worker thread; they only report through log_fn.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import re
from pathlib import Path

from Utils.config_paths import (
    get_exe_args_path,
    get_game_config_dir,
    get_game_config_path,
    get_profile_exe_args_path,
)
from Utils.xdg import spawn_watched, xdg_open

_LAUNCH_MODE_FILE = "exe_launch_mode.json"
_CUSTOM_EXES_FILE = "custom_exes.json"

EXE_PICKER_FILTERS = [
    ("Executables (*.exe, *.bat, *.jar)", ["*.exe", "*.bat", "*.jar"]),
    ("All files", ["*"]),
]

# Java-runtime modes for .jar entries, persisted per-exe in exe_launch_mode.json.
JAR_RUNTIME_HOST = "host"      # run with the host's `java` (no Proton) — default
JAR_RUNTIME_PROTON = "proton"  # run a Windows Java inside the game's Proton prefix


def is_jar(path) -> bool:
    return str(path).lower().endswith(".jar")


def _noop_log(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Custom exe registry — <staging>.parent/Applications/custom_exes.json
# ---------------------------------------------------------------------------

def custom_exes_path(game) -> Path | None:
    if game is None or not hasattr(game, "get_mod_staging_path"):
        return None
    return game.get_mod_staging_path().parent / "Applications" / _CUSTOM_EXES_FILE


def load_custom_exes(game) -> list[Path]:
    """Return the saved custom exe Paths (entries whose file still exists)."""
    p = custom_exes_path(game)
    if p is None or not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [Path(s) for s in data if Path(s).is_file()]
    except (OSError, ValueError):
        return []


def save_custom_exes(game, paths: list[Path]) -> None:
    p = custom_exes_path(game)
    if p is None:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([str(x) for x in paths], indent=2), encoding="utf-8")


def add_custom_exe(game, path: Path) -> None:
    existing = load_custom_exes(game)
    if path not in existing:
        existing.append(path)
        save_custom_exes(game, existing)


def remove_custom_exe(game, path: Path) -> None:
    existing = load_custom_exes(game)
    remaining = [p for p in existing if p != path]
    if len(remaining) != len(existing):
        save_custom_exes(game, remaining)


# ---------------------------------------------------------------------------
# exe_launch_mode.json — per-game launch settings
# ---------------------------------------------------------------------------

def _launch_mode_path(game) -> Path | None:
    if game is None:
        return None
    return get_game_config_dir(game.name) / _LAUNCH_MODE_FILE


def _read_launch_mode_data(game) -> dict:
    p = _launch_mode_path(game)
    if p is None or not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_launch_mode_key(game, key: str, value) -> None:
    """Set (or, when value is falsy-and-poppable, remove) one key."""
    p = _launch_mode_path(game)
    if p is None:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    data = _read_launch_mode_data(game)
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_launch_mode(game, exe_name: str) -> str:
    """Saved launch mode for exe_name: 'auto' | 'steam' | 'heroic' | 'none'."""
    return _read_launch_mode_data(game).get(exe_name, "auto")


def save_launch_mode(game, exe_name: str, mode: str) -> None:
    _write_launch_mode_key(game, exe_name, mode)


def load_deploy_before_launch(game) -> bool:
    return bool(_read_launch_mode_data(game).get("__deploy_before_launch", True))


def save_deploy_before_launch(game, enabled: bool) -> None:
    _write_launch_mode_key(game, "__deploy_before_launch", bool(enabled))


def load_proton_override(game, exe_name: str) -> str | None:
    """Saved Proton override name, '' for game default, None if never saved."""
    data = _read_launch_mode_data(game)
    return data.get(f"__proton_override_{exe_name}")


def save_proton_override(game, exe_name: str, proton_name: str) -> None:
    _write_launch_mode_key(game, f"__proton_override_{exe_name}",
                           proton_name if proton_name else None)


def load_launch_options(game, exe_name: str) -> str:
    return _read_launch_mode_data(game).get(f"__launch_options_{exe_name}", "")


def save_launch_options(game, exe_name: str, options: str) -> None:
    _write_launch_mode_key(game, f"__launch_options_{exe_name}",
                           options if options else None)


def load_jar_runtime(game, exe_name: str) -> str:
    """Saved Java runtime for a .jar entry: 'host' (default) or 'proton'."""
    val = _read_launch_mode_data(game).get(f"__jar_runtime_{exe_name}")
    return val if val == JAR_RUNTIME_PROTON else JAR_RUNTIME_HOST


def save_jar_runtime(game, exe_name: str, runtime: str) -> None:
    _write_launch_mode_key(
        game, f"__jar_runtime_{exe_name}",
        runtime if runtime == JAR_RUNTIME_PROTON else None)


# ---------------------------------------------------------------------------
# exe_args.json — per-exe launch arguments
# ---------------------------------------------------------------------------

def exe_args_file(game) -> Path:
    """The exe_args.json to use for *game*'s active profile.

    Profiles with the profile_specific_mods flag store args inside the profile
    dir so each profile can have independent tool output paths; everything
    else shares the global file.
    """
    try:
        active_dir = getattr(game, "_active_profile_dir", None)
        if active_dir is not None:
            from Utils.profile_state import profile_uses_specific_mods
            if profile_uses_specific_mods(Path(active_dir)):
                return get_profile_exe_args_path(Path(active_dir))
    except Exception:
        pass
    return get_exe_args_path()


def load_exe_args(game, exe_name: str) -> str:
    """Saved args for an exe, profile-local file first, then the global file."""
    try:
        profile_file = exe_args_file(game)
        if profile_file.is_file():
            data = json.loads(profile_file.read_text(encoding="utf-8"))
            if exe_name in data:
                return data[exe_name]
    except (OSError, ValueError):
        pass
    try:
        data = json.loads(get_exe_args_path().read_text(encoding="utf-8"))
        return data.get(exe_name, "")
    except (OSError, ValueError):
        return ""


def save_exe_args(game, exe_name: str, args_str: str) -> None:
    p = exe_args_file(game)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[exe_name] = args_str
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Launch options parser (Steam-style)
# ---------------------------------------------------------------------------

_ENV_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def split_preserving_backslash(s: str) -> list:
    """shlex.split but without treating ``\\`` as an escape character.

    Steam-style options for a Windows target contain paths like
    ``C:\\java8\\bin\\java.exe``; the default POSIX shlex would strip the
    backslashes (turning it into ``C:java8binjava.exe``). We keep whitespace
    splitting and quotes but disable escapes so Windows paths survive.
    """
    lex = shlex.shlex(s, posix=True)
    lex.whitespace_split = True
    lex.escape = ""  # don't treat backslash as an escape char
    try:
        return list(lex)
    except ValueError:
        return s.split()


def parse_launch_options(opts: str, command: list,
                         split_fn=shlex.split) -> tuple[dict, list]:
    """Parse Steam-style launch options into (env_vars, final_command).

    Tokens matching KEY=VALUE are extracted as environment variables.
    If ``%command%`` is present it is replaced by the actual *command* list
    (wrappers before it are prepended; tokens after it are appended).
    If ``%command%`` is absent the remaining tokens are appended as a suffix.

    *split_fn* tokenises each side; pass ``split_preserving_backslash`` when the
    options carry Windows paths (jar launches) so backslashes aren't eaten.
    """
    opts = (opts or "").strip()
    if not opts:
        return {}, list(command)

    env_vars: dict = {}

    if "%command%" in opts:
        idx = opts.index("%command%")
        prefix_str = opts[:idx]
        suffix_str = opts[idx + len("%command%"):]

        try:
            prefix_tokens = split_fn(prefix_str)
        except ValueError:
            prefix_tokens = prefix_str.split()
        try:
            suffix_tokens = split_fn(suffix_str)
        except ValueError:
            suffix_tokens = suffix_str.split()

        wrappers: list = []
        for token in prefix_tokens:
            if _ENV_VAR_RE.match(token):
                k, v = token.split("=", 1)
                env_vars[k] = v
            else:
                wrappers.append(token)

        suffix: list = []
        for token in suffix_tokens:
            if _ENV_VAR_RE.match(token):
                k, v = token.split("=", 1)
                env_vars[k] = v
            else:
                suffix.append(token)

        return env_vars, wrappers + list(command) + suffix
    else:
        try:
            tokens = split_fn(opts)
        except ValueError:
            tokens = opts.split()

        suffix = []
        for token in tokens:
            if _ENV_VAR_RE.match(token):
                k, v = token.split("=", 1)
                env_vars[k] = v
            else:
                suffix.append(token)

        return env_vars, list(command) + suffix


# ---------------------------------------------------------------------------
# Game exe resolution + install detection
# ---------------------------------------------------------------------------

def resolve_game_exe(game) -> Path | None:
    """Resolve the game's launch exe on disk.

    exe_name / exe_name_alts against game_path, recursive fallback for bare
    names (UE5 games keep the exe in Binaries/Win64/); a present
    preferred_launch_exe (e.g. a script extender) wins.
    """
    if game is None:
        return None
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path is None:
        return None
    exe_name = getattr(game, "exe_name", None)
    exe_name_alts = list(getattr(game, "exe_name_alts", []) or [])
    candidates_rel = [n for n in [exe_name, *exe_name_alts] if n]

    found_exe: Path | None = None
    for rel in candidates_rel:
        candidate = game_path / rel
        if candidate.is_file():
            found_exe = candidate
            break
    if found_exe is None:
        try:
            for rel in candidates_rel:
                bare = Path(rel).name
                for hit in game_path.rglob(bare):
                    if hit.is_file():
                        found_exe = hit
                        break
                if found_exe is not None:
                    break
        except OSError:
            pass

    preferred_rel = getattr(game, "preferred_launch_exe", "")
    if preferred_rel:
        preferred = game_path / preferred_rel
        if preferred.is_file():
            return preferred
    return found_exe


def game_exe_key(game) -> str:
    """The exe filename used to key the game's launch settings.

    Matches Tk, which keys exe_launch_mode.json by the resolved dropdown
    entry's filename (the preferred launch exe when present). Falls back to
    the configured exe_name when nothing resolves on disk.
    """
    resolved = resolve_game_exe(game)
    if resolved is not None:
        return resolved.name
    preferred_rel = getattr(game, "preferred_launch_exe", "")
    if preferred_rel:
        return Path(preferred_rel).name
    exe_name = getattr(game, "exe_name", None)
    return Path(exe_name).name if exe_name else ""


def effective_steam_id(game) -> str:
    from Utils.steam_finder import game_steam_id
    return game_steam_id(game)


def game_is_steam_install(game) -> bool:
    """True if the game folder lives inside a Steam library (steamapps/common)."""
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path is None:
        return False
    from Utils.steam_finder import find_steam_libraries
    try:
        resolved = game_path.resolve()
        for lib in find_steam_libraries():
            if resolved.is_relative_to(lib.resolve()):
                return True
    except Exception:
        pass
    return False


def heroic_app_names_for_launch(game) -> list:
    """Heroic app names for launch — detected by scanning Heroic's
    installed.json for the game's exe, plus legacy handler/paths.json values."""
    names: list[str] = []
    from Utils.heroic_finder import find_heroic_app_name_by_exe
    exe_names = [getattr(game, "exe_name", None)]
    exe_names += list(getattr(game, "exe_name_alts", []) or [])
    for exe in [e for e in exe_names if e]:
        try:
            found = find_heroic_app_name_by_exe(exe)
        except Exception:
            found = None
        if found and found not in names:
            names.append(found)

    names.extend(n for n in (getattr(game, "heroic_app_names", []) or []) if n not in names)

    if not names and hasattr(game, "name"):
        try:
            paths_file = get_game_config_path(game.name)
            if paths_file.is_file():
                data = json.loads(paths_file.read_text(encoding="utf-8"))
                saved = data.get("heroic_app_name", "").strip()
                if saved:
                    names = [saved]
        except (OSError, json.JSONDecodeError):
            pass
    return names


def game_is_heroic_install(game) -> bool:
    app_names = heroic_app_names_for_launch(game)
    if not app_names:
        return False
    from Utils.heroic_finder import find_heroic_launch_info
    try:
        return find_heroic_launch_info(app_names) is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Steam / Heroic launch
# ---------------------------------------------------------------------------

def launch_via_steam(steam_id: str, log_fn=_noop_log) -> None:
    """Launch through Steam (steam://rungameid) so the Steam API initialises.

    Inside a Flatpak sandbox the runtime has no `steam` binary and its own
    xdg-open can't resolve steam:// URLs, so we must forward to the host via
    ``flatpak-spawn --host``. A bare ``subprocess.Popen`` of that command
    "succeeds" (it finds flatpak-spawn) even when the *host* side fails —
    wrong host CWD, missing binary — which is why the Play button silently
    did nothing. ``spawn_watched`` fixes the CWD, watches the real exit code,
    and chains to the next candidate on failure.
    """
    log_fn(f"Play: launching via Steam (app {steam_id}) ...")
    url = f"steam://rungameid/{steam_id}"
    in_flatpak = Path("/.flatpak-info").exists()
    # Ordered candidates, each falling through to the next on non-zero exit.
    # Host xdg-open goes first: it routes steam:// to whichever Steam the user
    # actually has (native *or* Flatpak com.valvesoftware.Steam), whereas a
    # bare `steam` binary only exists for native installs.
    if in_flatpak and shutil.which("flatpak-spawn"):
        candidates = [
            ["flatpak-spawn", "--host", "xdg-open", url],
            ["flatpak-spawn", "--host", "steam", url],
            ["xdg-open", url],
        ]
    else:
        candidates = [
            ["xdg-open", url],
            ["steam", url],
        ]

    def _try(idx: int) -> None:
        if idx >= len(candidates):
            log_fn("Play error: could not reach Steam (no working launcher).")
            return
        spawn_watched(
            candidates[idx],
            f"Play steam://{steam_id}",
            log_fn,
            on_fail=lambda: _try(idx + 1),
        )

    _try(0)


def launch_via_heroic(heroic_app_names: list, log_fn=_noop_log) -> bool:
    """Launch through Heroic (heroic://launch). Returns False if the game
    isn't in a Heroic library (caller may fall through to Proton)."""
    from Utils.heroic_finder import find_heroic_launch_info
    info = find_heroic_launch_info(heroic_app_names)
    if info is None:
        log_fn("Play: game not found in Heroic library.")
        return False
    store, app_name = info
    log_fn(f"Play: launching via Heroic ({store}/{app_name}) ...")
    # xdg_open spawns asynchronously and reports failures through log_fn (it
    # doesn't raise), so pass it through rather than wrapping in try/except.
    xdg_open(f"heroic://launch/{store}/{app_name}", log_fn=log_fn)
    return True


# ---------------------------------------------------------------------------
# Bethesda tool-prefix setup (ported from wizards/_proton_prefix.py, which
# imports customtkinter and therefore can't be reused from Qt)
# ---------------------------------------------------------------------------

def link_plugins_txt(game, pfx: Path, log_fn=_noop_log) -> None:
    """Symlink the deployed profile's plugins.txt into a tool prefix.

    No-op for games without the Bethesda plugins.txt machinery.
    """
    if not hasattr(game, "_symlink_plugins_txt"):
        return
    profile = ""
    try:
        profile = game.get_last_deployed_profile() or ""
    except Exception:
        pass
    try:
        game._symlink_plugins_txt(profile or "default", log_fn, prefix_root=pfx)
    except Exception as exc:
        log_fn(f"plugins.txt link failed: {exc}")


def link_mygames(game, pfx: Path, log_fn=_noop_log) -> None:
    """Symlink the game prefix's My Games/<Game> dir into a tool prefix.

    Gives tools that read the game INIs (xEdit needs Skyrim.ini or it exits
    with a fatal error) the same files the game itself uses.
    """
    game_pfx = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    docs = getattr(game, "_MYGAMES_DOCS", None)
    sub = getattr(game, "_MYGAMES_SUBPATH", None)
    if game_pfx is None or docs is None or sub is None:
        return
    src = game_pfx / docs / sub
    if not src.is_dir():
        log_fn(f"game-prefix My Games folder not found ({src}) — skipping link.")
        return
    dst = pfx / docs / sub
    if dst.is_symlink() or dst.exists():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src, target_is_directory=True)
        log_fn(f"linked My Games → {dst}")
    except OSError as exc:
        log_fn(f"My Games link failed: {exc}")


_DOCUMENTS_REL = Path("drive_c/users/steamuser/Documents")


def link_game_documents(game, pfx: Path, subpath, log_fn=_noop_log) -> None:
    """Link the game prefix's Documents/<subpath> folder into a tool prefix.

    Some tools (e.g. Witcher 3 Script Merger) put a FileSystemWatcher on the
    game's user-documents folder (Documents\\The Witcher 3, where the mod
    load order lives) and crash on construction if it doesn't exist.  A fresh
    isolated/shared tool prefix has no such folder, so we symlink the game
    prefix's real one in (keeping the load order in sync).  If the game prefix
    doesn't have it either, create an empty directory so the watcher is happy.
    """
    sub = Path(subpath)
    dst = pfx / _DOCUMENTS_REL / sub
    if dst.is_symlink() or dst.exists():
        return
    game_pfx = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    src = (Path(game_pfx) / "pfx" / _DOCUMENTS_REL / sub
           if game_pfx is not None else None)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src is not None and src.is_dir():
            dst.symlink_to(src, target_is_directory=True)
            log_fn(f"linked Documents/{sub} → {dst}")
        else:
            dst.mkdir(parents=True, exist_ok=True)
            log_fn(f"created empty Documents/{sub} in tool prefix "
                   "(game prefix copy not found).")
    except OSError as exc:
        log_fn(f"Documents/{sub} link failed: {exc}")


def enable_show_dotfiles(proton_script: Path, env: dict,
                         log_fn=_noop_log) -> None:
    """Set Wine's ``ShowDotFiles=Y`` in the prefix behind *proton_script*/*env*.

    Enables browsing of Unix dot-dirs (e.g. under Z:) from Wine file dialogs so
    tools can reach mod-manager data. Idempotent — ``reg add /f`` overwrites any
    existing value — so it's safe to call on every prefix resolution, not just
    on first creation; that also repairs prefixes made before this behaviour.
    """
    from Utils.steam_finder import proton_run_command
    try:
        subprocess.run(
            proton_run_command(proton_script, "run", "reg", "add",
                               r"HKCU\Software\Wine", "/v", "ShowDotFiles",
                               "/t", "REG_SZ", "/d", "Y", "/f", env=env),
            env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception as exc:
        log_fn(f"ShowDotFiles: could not enable ({exc}).")


def get_tool_prefix_env(
    exe_path: Path, proton_name: str, prefix_dir: Path | None = None,
    steam_id: str | None = None,
) -> tuple[Path, Path, dict] | None:
    """Resolve (proton_script, prefix_dir, env) for a tool's isolated prefix.

    proton_name is the display name from the dropdown (e.g. "Proton 10.0").
    Returns None if the Proton version can't be found. The prefix directory is
    created if missing; wineboot initialises it when brand new (synchronous,
    up to 60s — call from a worker thread).
    """
    from Utils.steam_finder import (
        find_any_installed_proton,
        find_steam_root_for_proton_script,
        proton_run_command,
    )
    proton_script = find_any_installed_proton(proton_name)
    if proton_script is None:
        return None

    steam_root = find_steam_root_for_proton_script(proton_script)
    if steam_root is None:
        return None

    if prefix_dir is None:
        prefix_dir = exe_path.parent / f"prefix_{proton_script.parent.name}"
    is_new = not (prefix_dir / "pfx").is_dir()
    prefix_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"] = str(prefix_dir)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
    # lsteamclient asserts when it tries to attach to the Steam client with no
    # app context; tools in an isolated prefix have no AppId from Steam.
    if steam_id:
        env.setdefault("SteamAppId", steam_id)
        env.setdefault("SteamGameId", steam_id)

    if is_new:
        try:
            subprocess.run(
                proton_run_command(proton_script, "run", "wineboot", "--init",
                                   env=env),
                env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception:
            pass

    # Enable "Show dotfiles" so tools can browse Unix dot-dirs under Z:.
    # Applied on every resolution (idempotent) — always ensures the property is
    # set, and repairs prefixes created before this behaviour existed.
    enable_show_dotfiles(proton_script, env)

    return proton_script, prefix_dir, env


def prepare_tool_prefix(exe_path: Path, proton_name: str, game,
                        log_fn=_noop_log) -> tuple[Path, Path, dict] | None:
    """get_tool_prefix_env + the Bethesda registry/plugins.txt/My Games setup.

    Mirrors Tk's ExeConfigPanel._get_selected_tool_env. Synchronous (wineboot
    on first use) — call from a worker thread.
    """
    result = get_tool_prefix_env(
        exe_path, proton_name, steam_id=effective_steam_id(game),
    )
    if result is None:
        log_fn(f"Prefix tools: could not find Proton '{proton_name}'.")
        return None
    proton_script, prefix_dir, env = result
    if getattr(game, "synthesis_registry_name", None):
        from Utils.bethesda_registry import maybe_register_for_game
        maybe_register_for_game(
            prefix_dir=prefix_dir,
            proton_script=proton_script,
            env=env,
            game=game,
            log_fn=log_fn,
        )
    pfx = prefix_dir / "pfx"
    link_plugins_txt(game, pfx, lambda m: log_fn(f"Prefix tools: {m}"))
    link_mygames(game, pfx, lambda m: log_fn(f"Prefix tools: {m}"))
    return result


# ---------------------------------------------------------------------------
# Wizard-tool prefix placement (ported from wizards/_proton_prefix.py; file
# formats identical so choices are shared with the Tk wizards)
# ---------------------------------------------------------------------------

# Prefix-placement modes persisted per-exe alongside the Proton override.
PREFIX_MODE_ISOLATED = "isolated"  # prefix_<Proton>/ next to the exe (default)
PREFIX_MODE_SHARED = "shared"      # wine_prefixes/shared_<Proton>/, one per Proton
PREFIX_MODE_GAME = "game"          # reuse the game's own prefix

_LAUNCH_ENV_FILE = "launch_env.json"
_ENV_VAR_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def shared_prefix_dir(proton_dir_name: str) -> Path:
    """Return the shared tool prefix dir for a Proton version (one per version).

    Lives under the app config ``wine_prefixes/`` folder so it is shared by
    every wizard tool that opts into the shared prefix and survives Clear Cache.
    """
    from Utils.config_paths import get_wine_prefixes_dir
    return get_wine_prefixes_dir() / f"shared_{proton_dir_name}"


def load_prefix_mode(game, exe_name: str) -> str:
    """Return the saved prefix-placement mode for exe_name (isolated default)."""
    val = _read_launch_mode_data(game).get(f"__prefix_mode_{exe_name}")
    return val if val in (PREFIX_MODE_SHARED, PREFIX_MODE_GAME) else PREFIX_MODE_ISOLATED


def save_prefix_mode(game, exe_name: str, mode: str) -> None:
    """Persist the prefix-placement mode for exe_name (isolated = remove key)."""
    _write_launch_mode_key(
        game, f"__prefix_mode_{exe_name}",
        mode if mode in (PREFIX_MODE_SHARED, PREFIX_MODE_GAME) else None)


def load_tool_launch_env(exe: Path | None) -> str:
    """Return the saved env-var string for this exe ('' if none)."""
    if exe is None:
        return ""
    p = exe.parent / _LAUNCH_ENV_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(exe.name) or ""
    except (OSError, ValueError):
        return ""


def save_tool_launch_env(exe: Path | None, text: str) -> None:
    """Persist the env-var string in launch_env.json next to the exe."""
    if exe is None:
        return
    p = exe.parent / _LAUNCH_ENV_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        data = {}
    if text:
        data[exe.name] = text
    else:
        data.pop(exe.name, None)
    try:
        if data:
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        elif p.is_file():
            p.unlink()
    except OSError:
        pass


def parse_env_overrides(text: str) -> dict:
    """Parse a space-separated KEY=VALUE string into a dict (bad tokens skipped)."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    out: dict = {}
    for token in tokens:
        if _ENV_VAR_RE.match(token):
            k, v = token.split("=", 1)
            out[k] = v
    return out


def shutdown_prefix_wineserver(proton_script: Path, compat_data: Path,
                               log_fn=None) -> None:
    """Kill leftover wine processes still attached to a tool prefix.

    Proton sidecars (xalia.exe, services.exe, explorer.exe) can keep the
    prefix's wineserver alive indefinitely after the tool itself closes;
    they outlive the app and linger until the desktop session ends.
    """
    try:
        proton_dir = Path(proton_script).parent
        bin_dir = next(
            (proton_dir / d / "bin" for d in ("files", "dist")
             if (proton_dir / d / "bin" / "wineserver").is_file()),
            None,
        )
        if bin_dir is None:
            return
        env = os.environ.copy()
        env["WINEPREFIX"] = str(Path(compat_data) / "pfx")
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        subprocess.run(
            [str(bin_dir / "wineserver"), "-k"],
            env=env, timeout=15,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if log_fn is not None:
            log_fn("tool prefix wineserver shut down")
    except Exception:
        pass


def get_game_prefix_env(game, log_fn=_noop_log, *,
                        allow_runner_fallback: bool = False):
    """Resolve (proton_script, compat_data, env) for the game's OWN prefix.

    Reuses the existing game prefix (already initialised by the game), so no
    wineboot is run. Picks the Proton version Steam assigns to the game;
    with *allow_runner_fallback* it falls back to the prefix's recorded
    runner / any installed Proton when there is no Steam mapping (Heroic and
    GOG installs — mirrors the Tk downgrade/Morrowind wizards' resolver).
    Returns None on failure (after logging why).
    """
    from Utils.steam_finder import (
        find_proton_for_game, find_steam_root_for_proton_script,
    )
    pfx = game.get_prefix_path() if hasattr(game, "get_prefix_path") else None
    if pfx is None or not Path(pfx).is_dir():
        log_fn("game prefix not found — deploy/launch the game once, or pick "
               "a different prefix option.")
        return None
    steam_id = effective_steam_id(game)
    proton_script = find_proton_for_game(steam_id) if steam_id else None
    if proton_script is None and allow_runner_fallback:
        from Utils.proton_prefix import read_prefix_runner, resolve_compat_data
        from Utils.steam_finder import find_any_installed_proton
        preferred_runner = read_prefix_runner(resolve_compat_data(Path(pfx)))
        proton_script = find_any_installed_proton(preferred_runner)
        if proton_script is not None:
            log_fn(f"using fallback Proton tool {proton_script.parent.name} "
                   "(no per-game Steam mapping found).")
    if proton_script is None:
        log_fn("could not resolve the game's Proton version — pick a "
               "different prefix option.")
        return None
    steam_root = find_steam_root_for_proton_script(proton_script)
    if steam_root is None:
        return None
    compat_data = Path(pfx).parent
    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
    if steam_id:
        env.setdefault("SteamAppId", str(steam_id))
        env.setdefault("SteamGameId", str(steam_id))
    return proton_script, compat_data, env


def resolve_tool_prefix(exe: Path, game, proton_name: str, prefix_mode: str,
                        log_fn=_noop_log, *,
                        isolated_prefix_dir: "Path | None" = None):
    """Resolve (proton_script, compat_data, env) for a wizard tool's prefix.

    Honours the chosen placement mode:
      * isolated — creates/initialises prefix_<ProtonName>/ next to the exe,
                   or *isolated_prefix_dir* when given (tools whose exe sits
                   somewhere a prefix shouldn't go, e.g. Creation Kit in the
                   game root, relocate it)
      * shared   — creates/initialises wine_prefixes/shared_<ProtonName>/
      * game     — reuses the game's own prefix (no init)
    Saved per-exe env-var overrides (launch_env.json) are merged into env.
    First use of an isolated/shared prefix runs a synchronous wineboot —
    only call from a worker thread. Returns None on failure.

    Port of the Tk ProtonPrefixStepMixin._get_tool_env (note the different
    tuple order: compat_data before env, matching get_tool_prefix_env).
    """
    if prefix_mode == PREFIX_MODE_GAME:
        result = get_game_prefix_env(game, log_fn=log_fn)
    else:
        target = isolated_prefix_dir
        if prefix_mode == PREFIX_MODE_SHARED:
            from Utils.steam_finder import find_any_installed_proton
            proton_script = find_any_installed_proton(proton_name)
            if proton_script is None:
                log_fn(f"could not find Proton '{proton_name}'.")
                return None
            target = shared_prefix_dir(proton_script.parent.name)
        result = get_tool_prefix_env(
            exe, proton_name, prefix_dir=target,
            steam_id=effective_steam_id(game),
        )
    if result is None:
        return None
    proton_script, compat_data, env = result
    extra = parse_env_overrides(load_tool_launch_env(exe))
    if extra:
        env.update(extra)
        log_fn("applying saved env vars: "
               + " ".join(f"{k}={v}" for k, v in extra.items()))
    return proton_script, compat_data, env


def run_tool_logged(
    proton_script: Path,
    exe: Path,
    env: dict,
    log_fn=_noop_log,
    *,
    extra_args: "list[str] | None" = None,
    cwd: "Path | None" = None,
    label: str | None = None,
    winedebug: str = "+err,+warn,fixme-all",
) -> int:
    """Launch *exe* through Proton and stream its output to *log_fn*.

    Replaces the old ``Popen(..., stdout=DEVNULL, stderr=DEVNULL)`` pattern the
    wizard tools all used, which silently swallowed crash traces (e.g.
    WitcherScriptMerger dying instantly with no visible cause). We now:

      * force ``WINEDEBUG`` (unless the caller already set one in *env*) so
        Wine-side load failures are emitted, and
      * merge the tool's stdout+stderr and pump it line-by-line to *log_fn*.

    Blocks until the process exits (call from a worker thread) and returns the
    exit code. *proton_script* and *env* come from ``resolve_tool_prefix``;
    *extra_args* are appended after the exe (e.g. xEdit's data-path flag).
    """
    from Utils.steam_finder import proton_run_command

    label = label or exe.name
    # Only set WINEDEBUG when the caller hasn't chosen its own channels
    # (BodySlide sets +wgl,+opengl for its GL trace and must win).
    env.setdefault("WINEDEBUG", winedebug)

    cmd = proton_run_command(proton_script, "run", str(exe), env=env)
    if extra_args:
        cmd = cmd + list(extra_args)

    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(cwd) if cwd is not None else str(exe.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
    except OSError as exc:
        log_fn(f"{label}: failed to launch — {exc}")
        raise

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line:
            log_fn(f"{label}: {line}")
    rc = proc.wait()
    if rc != 0:
        log_fn(f"{label}: exited with code {rc}")
    return rc


def launch_winetricks_in_prefix(wineprefix: Path, log_fn=_noop_log) -> None:
    """Launch the winetricks GUI against *wineprefix* (a .../pfx dir),
    downloading winetricks/cabextract on demand."""
    from Utils.protontricks import (
        _bundled_winetricks,
        _get_proton_bin,
        cabextract_installed,
        install_cabextract,
        install_winetricks,
        winetricks_installed,
    )

    if not wineprefix.is_dir():
        log_fn("Prefix tools: no Wine prefix is available — cannot launch winetricks.")
        return
    if not winetricks_installed():
        log_fn("Prefix tools: winetricks not found — downloading …")
        if not install_winetricks(log_fn=lambda m: log_fn(f"Prefix tools: {m}")):
            return
    if not cabextract_installed():
        log_fn("Prefix tools: cabextract not found — downloading a portable copy …")
        if not install_cabextract(log_fn=lambda m: log_fn(f"Prefix tools: {m}")):
            return

    wt = _bundled_winetricks()
    env = os.environ.copy()
    env["WINEPREFIX"] = str(wineprefix)
    path_prefix = str(wt.parent)
    proton_bin = _get_proton_bin()
    if proton_bin:
        path_prefix = proton_bin + os.pathsep + path_prefix
    env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")

    log_fn(f"Prefix tools: launching winetricks GUI against {wineprefix.parent.name} …")
    try:
        subprocess.Popen(
            [str(wt), "--gui"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log_fn(f"Prefix tools error: {e}")


def launch_wine_tool_in_prefix(proton_script: Path, prefix_dir: Path, env: dict,
                               tool: str, log_fn=_noop_log) -> bool:
    """Launch a bundled wine tool (``winecfg`` / ``regedit``) inside an isolated
    tool prefix. Returns False if the launch failed.

    *proton_script*/*env* come from ``get_tool_prefix_env`` (env already carries
    STEAM_COMPAT_DATA_PATH); *prefix_dir* is that same isolated prefix. Mirrors
    proton_tools.wine_tool_command: uses Proton's ``runinprefix`` verb (running
    the raw ``files/bin/wine`` binary core-dumps on modern GE-Proton) and, inside
    our own Flatpak sandbox, forwards the launch to the host.
    """
    from Utils.proton_tools import _host_forward
    from Utils.steam_finder import proton_run_command

    env["WINEPREFIX"] = str(prefix_dir / "pfx")
    cmd = proton_run_command(proton_script, "runinprefix", tool, env=env)
    cmd = _host_forward(cmd, env, lambda m: log_fn(f"Prefix tools: {m}"))
    log_fn(f"Prefix tools: launching {tool} …")
    try:
        subprocess.Popen(cmd, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        log_fn(f"Prefix tools error: {e}")
        return False


# ---------------------------------------------------------------------------
# Launch entry points
# ---------------------------------------------------------------------------

def launch_game(game, log_fn=_noop_log) -> None:
    """Launch the game itself: native command / Steam / Heroic / Proton,
    honouring the saved launch mode. Call from a worker thread."""
    native_cmd = getattr(game, "get_launch_command", lambda: None)()
    if native_cmd is not None:
        log_fn(f"Play: launching natively: {' '.join(native_cmd)}")
        try:
            subprocess.Popen(
                native_cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log_fn(f"Play error: {e}")
        return

    mode = load_launch_mode(game, game_exe_key(game))
    steam_id = effective_steam_id(game)
    heroic_app_names = heroic_app_names_for_launch(game)

    if mode == "steam":
        if steam_id:
            launch_via_steam(steam_id, log_fn)
        else:
            log_fn("Play: launch mode is Steam but game has no Steam ID.")
        return

    if mode == "heroic":
        if heroic_app_names:
            launch_via_heroic(heroic_app_names, log_fn)
        else:
            log_fn("Play: launch mode is Heroic but game has no Heroic app name.")
        return

    if mode != "none":  # "auto"
        if steam_id and game_is_steam_install(game):
            launch_via_steam(steam_id, log_fn)
            return
        if heroic_app_names and game_is_heroic_install(game):
            if launch_via_heroic(heroic_app_names, log_fn):
                return

    exe_path = resolve_game_exe(game)
    if exe_path is None:
        log_fn("Play: could not find the game's executable on disk.")
        return

    # Native Linux binary (no .exe/.bat suffix): run directly instead of
    # routing through Proton, which would fail on an ELF executable.
    if exe_path.suffix.lower() not in (".exe", ".bat"):
        log_fn(f"Play: launching native binary: {exe_path}")
        try:
            subprocess.Popen(
                [str(exe_path)],
                cwd=str(exe_path.parent),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log_fn(f"Play error: {e}")
        return

    launch_exe_via_proton(exe_path, game, log_fn)


def launch_exe_via_proton(exe_path: Path, game, log_fn=_noop_log) -> None:
    """Standard Proton launch path for .exe files. Call from a worker thread.

    Uses the game's prefix by default; a saved per-exe Proton override runs in
    an isolated prefix_<Proton>/ next to the exe (with Bethesda registry /
    plugins.txt / My Games setup mirrored from the wizard prefixes).
    """
    from Utils.proton_prefix import read_prefix_runner, resolve_compat_data
    from Utils.steam_finder import (
        find_any_installed_proton,
        find_proton_for_game,
        find_steam_root_for_proton_script,
        list_installed_proton,
        proton_run_command,
    )

    proton_override_name = load_proton_override(game, exe_path.name)
    if proton_override_name:
        # Try exact match first, then prefix match ("Proton 10" → "Proton 10.0")
        proton_script = find_any_installed_proton(proton_override_name)
        if proton_script is None:
            override_lower = proton_override_name.lower()
            for candidate in list_installed_proton():
                if candidate.parent.name.lower().startswith(override_lower):
                    proton_script = candidate
                    break
        if proton_script is None:
            log_fn(f"Run EXE: Proton override '{proton_override_name}' not found.")
            return
        # Dedicated prefix next to the exe so it's isolated from the game prefix
        compat_data = exe_path.parent / f"prefix_{proton_script.parent.name}"
        compat_data.mkdir(parents=True, exist_ok=True)
        log_fn(f"Run EXE: using {proton_script.parent.name} with isolated prefix.")
    else:
        prefix_path = (
            game.get_prefix_path()
            if hasattr(game, "get_prefix_path") else None
        )
        if prefix_path is None or not prefix_path.is_dir():
            log_fn("Run EXE: Proton prefix not configured for this game.")
            return

        compat_data = resolve_compat_data(prefix_path)

        steam_id = effective_steam_id(game)
        proton_script = find_proton_for_game(steam_id) if steam_id else None
        if proton_script is None:
            # Use the same Proton version the prefix was built with.
            preferred_runner = read_prefix_runner(compat_data)
            proton_script = find_any_installed_proton(preferred_runner)
            if proton_script is None:
                if steam_id:
                    log_fn(
                        f"Run EXE: could not find Proton version for app {steam_id}, "
                        "and no installed Proton tool was found."
                    )
                else:
                    log_fn("Run EXE: no Steam ID and no installed Proton tool was found.")
                return
            log_fn(
                f"Run EXE: using fallback Proton tool {proton_script.parent.name} "
                "(no per-game Steam mapping found)."
            )

    steam_root = find_steam_root_for_proton_script(proton_script)
    if steam_root is None:
        log_fn("Run EXE: could not determine Steam root for the selected Proton tool.")
        return

    env = os.environ.copy()
    env["STEAM_COMPAT_DATA_PATH"] = str(compat_data)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(steam_root)
    # Proton expects these to locate the game install and per-game shader/
    # compat caches; without them GE-Proton falls back to app ID 0.
    game_path = game.get_game_path() if hasattr(game, "get_game_path") else None
    if game_path and not proton_override_name:
        env["STEAM_COMPAT_INSTALL_PATH"] = str(game_path)
    if not proton_override_name:
        steam_id = effective_steam_id(game)
        if steam_id:
            env.setdefault("SteamAppId", steam_id)
            env.setdefault("SteamGameId", steam_id)

    if proton_override_name:
        # Bethesda games: mirror the wizard-prefix setup so tools in the
        # isolated prefix see the game path (registry), the deployed
        # plugins.txt and the game's My Games INIs. All no-ops otherwise.
        if getattr(game, "synthesis_registry_name", None):
            from Utils.bethesda_registry import maybe_register_for_game
            maybe_register_for_game(
                prefix_dir=compat_data,
                proton_script=proton_script,
                env=env,
                game=game,
                log_fn=log_fn,
            )
        pfx = compat_data / "pfx"
        link_plugins_txt(game, pfx, lambda m: log_fn(f"Run EXE: {m}"))
        link_mygames(game, pfx, lambda m: log_fn(f"Run EXE: {m}"))

    try:
        extra_args = shlex.split(load_exe_args(game, exe_path.name))
    except ValueError as e:
        log_fn(f"Run EXE: invalid arguments — {e}")
        return

    log_fn(f"Run EXE: launching {exe_path.name} via {proton_script.parent.name} ...")

    # Apply launch-option env vars before building the command: when the
    # command gets wrapped in flatpak-spawn --host, proton_run_command
    # forwards the env diff via --env= flags, so env must be final here.
    launch_opts = load_launch_options(game, exe_path.name)
    env_updates, _ = parse_launch_options(launch_opts, [])
    if env_updates:
        env.update(env_updates)

    base_cmd = proton_run_command(proton_script, "run", str(exe_path),
                                  env=env) + extra_args
    if not launch_opts:
        final_cmd = base_cmd
    else:
        _, final_cmd = parse_launch_options(launch_opts, base_cmd)

    log_fn(f"Run EXE:   cmd: {' '.join(final_cmd)}")
    _env_keys = (
        "WINE_D3D_CONFIG", "PROTON_USE_WINED3D", "WINEDLLOVERRIDES",
        "STEAM_COMPAT_DATA_PATH", "WINEDEBUG", "DXVK_HUD", "PROTON_LOG",
    )
    _env_summary = " ".join(
        f"{k}={env.get(k)}" for k in _env_keys if env.get(k) is not None
    )
    if _env_summary:
        log_fn(f"Run EXE:   env: {_env_summary}")

    try:
        subprocess.Popen(
            final_cmd,
            env=env,
            cwd=exe_path.parent,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log_fn(f"Run EXE error: {e}")


def resolve_jar_prefix_env(jar_path: Path, game, log_fn=_noop_log):
    """Resolve (proton_script, compat_data, env) for running a .jar under Proton.

    Follows the same rule as regular exes (launch_exe_via_proton): with no
    Proton override the game's own prefix is used; with an override an isolated
    ``prefix_<Proton>/`` is created next to the jar. Returns None on failure
    (after logging why). First use of an isolated prefix runs wineboot — call
    from a worker thread.
    """
    from Utils.steam_finder import (
        find_any_installed_proton, list_installed_proton,
    )
    override = load_proton_override(game, jar_path.name)
    if not override:
        # Game prefix (no wineboot; already initialised by the game).
        return get_game_prefix_env(
            game, log_fn=lambda m: log_fn(f"Run JAR: {m}"),
            allow_runner_fallback=True)

    # Specific Proton → isolated prefix_<Proton>/ next to the jar.
    proton_script = find_any_installed_proton(override)
    if proton_script is None:
        override_lower = override.lower()
        for candidate in list_installed_proton():
            if candidate.parent.name.lower().startswith(override_lower):
                proton_script = candidate
                break
    if proton_script is None:
        log_fn(f"Run JAR: Proton override '{override}' not found.")
        return None
    prefix_dir = jar_path.parent / f"prefix_{proton_script.parent.name}"
    result = get_tool_prefix_env(
        jar_path, override, prefix_dir=prefix_dir,
        steam_id=effective_steam_id(game))
    return result


def launch_jar(jar_path: Path, game, log_fn=_noop_log) -> None:
    """Launch a .jar via a user-supplied Java command. Call from a worker thread.

    Java runtimes can't be run through ``proton run <jar>``, so the actual
    command comes from the exe's Launch Options / Launch arguments, which the
    user fills in themselves. ``%command%`` is substituted with the jar's path
    so a typical invocation looks like ``java -jar %command%``.

    Two runtime modes (per-exe, saved in exe_launch_mode.json):
      * host   — the jar path is the native Unix path and the command runs
                 directly on the host (its `java`); no Proton.
      * proton — the jar path is the prefix's Z: Wine path and the whole
                 command is wrapped in ``proton run`` so a Windows Java inside
                 a Proton prefix runs it. Which prefix follows the exe's Proton
                 override: none → the game's prefix; a specific version → an
                 isolated ``prefix_<Proton>/`` next to the jar.
    """
    runtime = load_jar_runtime(game, jar_path.name)
    launch_opts = load_launch_options(game, jar_path.name)
    args_str = load_exe_args(game, jar_path.name)

    if runtime == JAR_RUNTIME_PROTON:
        result = resolve_jar_prefix_env(jar_path, game, log_fn=log_fn)
        if result is None:
            return
        from Utils.steam_finder import proton_run_command
        from Utils.wine_paths import to_wine_path
        proton_script, compat_data, env = result
        jar_token = to_wine_path(jar_path)
        # Windows target: keep backslashes in paths and any file arguments the
        # user added.
        extra_args = split_preserving_backslash(args_str)
        # Always launch the bundled Windows Java on the jar; the user doesn't
        # have to type a command. We reference java.exe by its in-prefix C:
        # path (C:\java8\bin\java.exe) rather than a Z: path — a Z: path into
        # the prefix's own drive (and one with spaces, e.g. "Proton -
        # Experimental") is what made the launch fail.
        from Utils.jre_prefix import java_exe_in_prefix, JAVA_EXE_WIN
        java_native = java_exe_in_prefix(compat_data)
        if not java_native.is_file():
            log_fn("Run JAR: no Java in this prefix — click 'Install Java into "
                   "prefix' in the exe settings first (it installs into the "
                   "prefix the Proton version selects).")
            return
        jvm_cmd = [JAVA_EXE_WIN, "-jar", jar_token]
        # Launch Options are appended as extra flags. Steam-style %command% is
        # still honoured (it stands for the whole java command) for power users;
        # otherwise env vars are extracted and the rest appended after the jar.
        if launch_opts:
            env_updates, jvm_cmd = parse_launch_options(
                launch_opts, jvm_cmd, split_fn=split_preserving_backslash)
            if env_updates:
                env.update(env_updates)
        final_cmd = proton_run_command(
            proton_script, "run", *jvm_cmd, env=env) + extra_args
    else:  # host
        env = os.environ.copy()
        jar_token = str(jar_path)
        try:
            extra_args = shlex.split(args_str)
        except ValueError as e:
            log_fn(f"Run JAR: invalid arguments — {e}")
            return
        opts_for_cmd = launch_opts or "java -jar %command%"
        env_updates, host_cmd = parse_launch_options(opts_for_cmd, [jar_token])
        if env_updates:
            env.update(env_updates)
        if not host_cmd:
            log_fn("Run JAR: launch options produced no command — add e.g. "
                   "'java -jar %command%' in Launch Options.")
            return
        # Fail loudly when the launcher (java) isn't installed — otherwise the
        # process errors out invisibly and nothing opens.
        launcher = host_cmd[0]
        if os.sep not in launcher and shutil.which(launcher) is None:
            in_flatpak = Path("/.flatpak-info").exists()
            if in_flatpak and shutil.which("flatpak-spawn"):
                # The host may have java even if the sandbox doesn't — try it.
                host_cmd = ["flatpak-spawn", "--host", *host_cmd]
                log_fn(f"Run JAR: '{launcher}' not in sandbox — forwarding to host.")
            else:
                log_fn(f"Run JAR error: '{launcher}' not found. Install a Java "
                       "runtime (e.g. `sudo pacman -S jre-openjdk` / your "
                       "distro's JRE) or set the full path in Launch Options.")
                return
        final_cmd = host_cmd + extra_args

    log_fn(f"Run JAR: launching {jar_path.name} ({runtime}) ...")
    log_fn(f"Run JAR:   cmd: {' '.join(final_cmd)}")
    try:
        proc = subprocess.Popen(
            final_cmd,
            env=env,
            cwd=str(jar_path.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
    except Exception as e:
        log_fn(f"Run JAR error: {e}")
        return

    # Stream the launcher's output to the log so failures (missing java.exe in
    # the prefix, a jar that crashes on start) are visible instead of silent.
    def _pump():
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                log_fn(f"Run JAR: {line}")
        rc = proc.wait()
        if rc != 0:
            log_fn(f"Run JAR: {jar_path.name} exited with code {rc}")

    import threading
    threading.Thread(target=_pump, daemon=True).start()

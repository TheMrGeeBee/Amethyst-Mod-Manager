"""
Game and profile helpers: _GAMES registry, load_games, profiles, create_profile, etc.
Used by TopBar, ModListPanel, PluginPanel, and App. No dependency on other gui modules.
"""

import json
import shutil
from pathlib import Path

from Games.base_game import BaseGame
from Utils.config_paths import get_config_dir, get_profiles_dir, get_last_game_path, get_loot_game_dir
from Utils.game_loader import discover_games
from Utils.plugin_loader import discover_plugins
from Utils.profile_state import (
    merge_profile_settings,
    read_profile_settings,
    write_profile_settings,
)

# Game handlers — populated by _load_games() when first called
_GAMES: dict[str, BaseGame] = {}


def foreign_deployed_plugin_basenames(game) -> set[str]:
    """Return lowercase plugin filenames currently deployed by a *different*
    profile than the one the UI has active.

    When profile A is deployed and the user switches to profile B, the live
    Data/ folder still contains A's mod plugin files. Callers that scan Data/
    (vanilla detection, orphan detection) use this to exclude those files so
    they don't bleed across profiles. Returns an empty set when the active
    profile is the deployed one, or when no deploy is active.
    """
    try:
        if not getattr(game, "get_deploy_active", lambda: False)():
            return set()
        deployed_name = game.get_last_deployed_profile()
        active_dir = getattr(game, "_active_profile_dir", None)
        if active_dir is not None and active_dir.name == deployed_name:
            return set()
        deployed_dir = game.get_profile_root() / "profiles" / deployed_name
        names: set[str] = set()
        for fname in ("plugins.txt", "loadorder.txt"):
            p = deployed_dir / fname
            if not p.is_file():
                continue
            try:
                for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                    n = line.strip()
                    if not n or n.startswith("#"):
                        continue
                    if n.startswith("*"):
                        n = n[1:]
                    if n:
                        names.add(n.lower())
            except OSError:
                continue
        return names
    except (OSError, AttributeError):
        return set()


def _vanilla_plugins_for_game(game) -> dict[str, str]:
    """Return {lowercase_name: original_name} for vanilla plugins."""
    result: dict[str, str] = {}
    for name in getattr(game, "vanilla_plugins", []) or []:
        result[name.lower()] = name

    dlc_plugins = getattr(game, "vanilla_dlc_plugins", []) or []
    ccc_name = getattr(game, "vanilla_ccc_filename", None)
    if not dlc_plugins and not ccc_name:
        return result

    # DLC and CC entries are only treated as vanilla if their file is present
    # in the Data folder — users may not own every DLC. We scan the live folder
    # without subtracting another profile's deployment: DLC names are a fixed
    # list and CC names come from the .ccc manifest, so cross-profile mod
    # plugins can't accidentally match here.
    data_dir = game.get_vanilla_plugins_path() if hasattr(game, "get_vanilla_plugins_path") else None
    present: set[str] = set()
    if data_dir and data_dir.is_dir():
        try:
            present = {entry.name.lower() for entry in data_dir.iterdir() if entry.is_file()}
        except OSError:
            pass

    for name in dlc_plugins:
        if name.lower() in present:
            result.setdefault(name.lower(), name)

    if not ccc_name:
        return result
    game_path = game.get_game_path()
    if not game_path:
        return result
    ccc = game_path / ccc_name
    if not ccc.is_file():
        return result
    try:
        for line in ccc.read_text(encoding="utf-8", errors="ignore").splitlines():
            n = line.strip()
            if n and not n.startswith("#") and n.lower() in present:
                result.setdefault(n.lower(), n)
    except OSError:
        pass
    return result


def _load_games() -> list[str]:
    """Discover game handlers and return sorted display names (configured games only)."""
    global _GAMES
    new_games = discover_games()
    _GAMES.clear()
    _GAMES.update(new_games)
    discover_plugins()
    for game in _GAMES.values():
        if getattr(game, "loot_sort_enabled", False) and getattr(game, "game_id", None):
            get_loot_game_dir(game.game_id)
    names = sorted(name for name, game in _GAMES.items() if game.is_configured())
    return names if names else ["No games configured"]


def _profiles_for_game(game_name: str) -> list[str]:
    """Return sorted profile folder names for the given game, 'default' first."""
    game = _GAMES.get(game_name)
    if game is not None:
        profiles_dir = game.get_profile_root() / "profiles"
    else:
        profiles_dir = get_profiles_dir() / game_name / "profiles"
    if not profiles_dir.is_dir():
        return ["default"]
    names = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir())
    # Ensure 'default' is always first if present
    if "default" in names:
        names.remove("default")
        names.insert(0, "default")
    return names if names else ["default"]


def profile_uses_specific_mods(profile_dir: Path) -> bool:
    """Return True if this profile stores its own mods folder inside itself."""
    return bool(read_profile_settings(profile_dir, None).get("profile_specific_mods", False))


def get_collection_url_from_profile(profile_dir: Path) -> str | None:
    """Return the collection URL from profile_state profile_settings, or None if not set."""
    url = read_profile_settings(profile_dir, None).get("collection_url")
    return url if url else None


def save_collection_url_to_profile(profile_dir: Path, collection_url: str) -> None:
    """Save collection_url into profile_settings, merging with existing keys."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    merge_profile_settings(profile_dir, {"collection_url": collection_url})


def find_profile_with_collection_url(game_name: str, collection_url: str) -> str | None:
    """Return the profile name whose profile_state contains *collection_url*, or None."""
    game = _GAMES.get(game_name)
    if game is not None:
        profiles_dir = game.get_profile_root() / "profiles"
    else:
        profiles_dir = get_profiles_dir() / game_name / "profiles"
    if not profiles_dir.is_dir():
        return None
    for p in profiles_dir.iterdir():
        if p.is_dir():
            url = get_collection_url_from_profile(p)
            if url and url == collection_url:
                return p.name
    return None


def find_profile_with_collection_slug(game_name: str, slug: str) -> str | None:
    """Return the profile name whose stored collection_url matches *slug*, regardless
    of which /revisions/{N} suffix (if any) is attached. Use this to find a profile
    eligible for a revision swap (update) rather than an exact-URL reinstall."""
    if not slug:
        return None
    game = _GAMES.get(game_name)
    if game is not None:
        profiles_dir = game.get_profile_root() / "profiles"
    else:
        profiles_dir = get_profiles_dir() / game_name / "profiles"
    if not profiles_dir.is_dir():
        return None
    needle = f"/collections/{slug}"
    for p in profiles_dir.iterdir():
        if not p.is_dir():
            continue
        url = get_collection_url_from_profile(p)
        if not url:
            continue
        if needle in url and (url.endswith(needle) or f"{needle}/" in url):
            return p.name
    return None


def _create_profile(
    game_name: str,
    profile_name: str,
    profile_specific_mods: bool = False,
) -> Path:
    """Create a new profile folder, copying modlist.txt from default."""
    game = _GAMES.get(game_name)
    if game is not None:
        profiles_root = game.get_profile_root()
    else:
        profiles_root = get_profiles_dir() / game_name
    profile_dir = profiles_root / "profiles" / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    plugins = profile_dir / "plugins.txt"
    if not plugins.exists():
        plugins.touch()
    modlist = profile_dir / "modlist.txt"
    if not modlist.exists():
        if profile_specific_mods:
            # Profile-specific mods folder starts empty — don't inherit the
            # default modlist which references the shared mods directory.
            modlist.touch()
        else:
            default_modlist = profiles_root / "profiles" / "default" / "modlist.txt"
            if default_modlist.exists():
                shutil.copy2(default_modlist, modlist)
            else:
                modlist.touch()
    if profile_specific_mods:
        write_profile_settings(profile_dir, {"profile_specific_mods": True})
        # Create the profile-specific mods, overwrite, and Root_Folder directories
        # up front so they exist as soon as the profile is selected.
        (profile_dir / "mods").mkdir(exist_ok=True)
        (profile_dir / "overwrite").mkdir(exist_ok=True)
        (profile_dir / "Root_Folder").mkdir(exist_ok=True)
    return profile_dir


def _save_last_game(game_name: str) -> None:
    """Persist the last-selected game name to the config directory."""
    try:
        get_last_game_path().write_text(
            json.dumps({"last_game": game_name}), encoding="utf-8"
        )
    except OSError:
        pass


def _load_last_game() -> str | None:
    """Return the previously saved game name, or None if not set / unreadable."""
    try:
        data = json.loads(get_last_game_path().read_text(encoding="utf-8"))
        return data.get("last_game")
    except (OSError, ValueError, KeyError):
        return None


def _clear_game_config(game_name: str) -> None:
    """Remove this game's config from ~/.config/AmethystModManager/games/<game_name>/.
    Causes the game to show as unconfigured on next use."""
    game_config_dir = get_config_dir() / "games" / game_name
    try:
        if game_config_dir.is_dir():
            shutil.rmtree(game_config_dir)
    except OSError:
        pass
    game = _GAMES.get(game_name)
    if game is not None:
        game.load_paths()


def _handle_missing_profile_root(topbar, game_name: str) -> None:
    """Profile/staging folder was deleted: clear game config, refresh list, switch to another game or clear last_game."""
    _clear_game_config(game_name)
    game_names = _load_games()
    topbar._game_menu.configure(values=game_names)
    if game_names and game_names[0] != "No games configured":
        topbar._game_var.set(game_names[0])
        if hasattr(topbar, "_profile_menu") and topbar._profile_menu is not None:
            profiles = _profiles_for_game(game_names[0])
            topbar._profile_menu.configure(values=profiles)
            topbar._profile_var.set(profiles[0])
        topbar._reload_mod_panel()
    else:
        get_last_game_path().unlink(missing_ok=True)
        topbar._game_var.set("No games configured")
        if hasattr(topbar, "_profile_menu") and topbar._profile_menu is not None:
            topbar._profile_menu.configure(values=["default"])
            topbar._profile_var.set("default")
        topbar._reload_mod_panel()

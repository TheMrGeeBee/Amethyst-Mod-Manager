"""
GUI-neutral core of the Tale of Two Wastelands installer wizard.

Moved out of wizards/ttw.py (which imports customtkinter) so the Qt wizard
view can share it: installer/mod discovery, vanilla-esm validation, the
restore-to-vanilla step, output registration and requirement seeding.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from Games.base_game import BaseGame

GITHUB_API_URL = (
    "https://api.github.com/repos/SulfurNitride/TTW_Linux_Installer/releases/latest"
)
GITHUB_REPO_URL = "https://github.com/SulfurNitride/TTW_Linux_Installer"
MODPUB_URL = "https://mod.pub/ttw/133/files"
EXE_NAME = "mpi_installer"
APP_DIR = "TTW"
OUTPUT_NAME = "Tale of Two Wastelands"

# Nexus (newvegas) mod ids the TTW setup recommends/requires. Seeded into the
# TTW mod's meta.ini missing_requirements so they surface through the standard
# "missing requirements" flag (red marker → install panel).
TTW_REQUIRED_MOD_IDS = [
    57174, 68714, 82540, 70801, 65906, 77415, 58277, 66927,
    72541, 66537, 66347, 80993, 71973, 84823, 80666, 71336,
]

# Fallout 3 Steam app ids (vanilla + GOTY) used to auto-locate the FO3 install.
_FO3_STEAM_IDS = ("22300", "22370")
_FO3_EXE_NAME = "Fallout3.exe"

# Vanilla master + DLC plugins TTW xdelta-patches (FO3 list is fixed; the
# wizard runs under FNV so there's no FO3 game object to query).
FO3_REQUIRED_ESMS = [
    "Fallout3.esm",
    "Anchorage.esm", "ThePitt.esm", "BrokenSteel.esm",
    "PointLookout.esm", "Zeta.esm",
]


def _noop(_msg: str) -> None:
    pass


def applications_dir(game: "BaseGame") -> Path:
    return game.get_mod_staging_path().parent / "Applications" / APP_DIR


def find_ttw_installer(game: "BaseGame") -> Path | None:
    p = applications_dir(game) / EXE_NAME
    return p if p.is_file() else None


def find_fo3_install() -> Path | None:
    """Locate the Fallout 3 install folder via Steam libraries, or None."""
    try:
        from Utils.steam_finder import find_game_by_steam_id, find_steam_libraries
        libs = find_steam_libraries()
        for sid in _FO3_STEAM_IDS:
            hit = find_game_by_steam_id(libs, sid, _FO3_EXE_NAME)
            if hit is not None:
                return hit
    except Exception:
        pass
    return None


def missing_vanilla_esms(game_root: Path, esms: "list[str]") -> list[str]:
    """Return the *esms* not present in ``<game_root>/Data`` (case-insensitive)."""
    data = game_root / "Data"
    try:
        present = {p.name.lower() for p in data.iterdir() if p.is_file()}
    except OSError:
        return list(esms)
    return [e for e in esms if e.lower() not in present]


def fnv_required_esms(game: "BaseGame") -> list[str]:
    """Vanilla master + DLC .esm files TTW patches (from the game's plugin lists)."""
    plugins = list(getattr(game, "vanilla_plugins", []) or []) + \
        list(getattr(game, "vanilla_dlc_plugins", []) or [])
    return [p for p in plugins if p.lower().endswith(".esm")]


def ttw_mod_dir(game: "BaseGame") -> "Path | None":
    """Path to the already-installed TTW mod in staging, or None (only when the
    key merged plugin is present, so a stray empty folder doesn't trip skip)."""
    try:
        staging = game.get_effective_mod_staging_path()
    except Exception:
        staging = None
    if staging is None:
        return None
    mod_dir = staging / OUTPUT_NAME
    if (mod_dir / "TaleOfTwoWastelands.esm").is_file():
        return mod_dir
    return None


def sync_active_profile(game: "BaseGame", profile: str) -> None:
    """Point the game's active profile dir at *profile* so staging/modlist/INI
    paths resolve there (staging can be per-profile)."""
    try:
        game.set_active_profile_dir(
            game.get_profile_root() / "profiles" / profile)
        game.load_paths()
    except Exception:
        pass


def restore_to_vanilla(game: "BaseGame", current_profile: str,
                       log_fn: Callable[[str], None] = _noop) -> "tuple[bool, Path | None]":
    """Restore the game to vanilla, mirroring the top-bar Restore button.

    Returns (success, fnv_game_root).  The game root is re-resolved from the
    last-deployed profile (per-profile paths) and returned so the caller keeps
    the post-restore esm check + installer on the same root.  Always restores
    the active profile to *current_profile* in the finally block.
    """
    if not hasattr(game, "restore"):
        return False, None

    fnv_root: "Path | None" = None
    success = True
    try:
        from Utils.deploy_pipeline import check_paths_mounted
        mount_err = check_paths_mounted(game)
        if mount_err:
            log_fn(f"Restore aborted: {mount_err}")
            return False, None

        last_deployed = game.get_last_deployed_profile()
        if last_deployed:
            game.set_active_profile_dir(
                game.get_profile_root() / "profiles" / last_deployed)
            game.load_paths()
        game_root = game.get_game_path()
        if game_root is not None:
            fnv_root = game_root

        game.restore(log_fn=log_fn)

        from Utils.deploy import restore_root_folder
        root_folder_dir = game.get_effective_root_folder_path()
        if root_folder_dir.is_dir() and game_root:
            restore_root_folder(root_folder_dir, game_root, log_fn=log_fn)
    except Exception as exc:
        success = False
        log_fn(f"restore error: {exc}")
    finally:
        try:
            game.set_active_profile_dir(
                game.get_profile_root() / "profiles" / current_profile)
            game.load_paths()
        except Exception:
            pass
        if success:
            try:
                game.clear_deploy_active()
            except Exception:
                pass
    return success, fnv_root


def register_output(game: "BaseGame", dest: Path,
                    log_fn: Callable[[str], None] = _noop) -> None:
    """Register the installer's Data/-rooted output as the TTW mod (normal
    Data-relative mod, not rootFolder) and index it."""
    from Utils.install_as_mod import index_installed_mod, register_as_mod_neutral
    register_as_mod_neutral(
        game, OUTPUT_NAME, archive=None, log_fn=log_fn, root_folder=False)
    index_installed_mod(game, OUTPUT_NAME, log_fn=log_fn)


def seed_required_mods(game: "BaseGame",
                       log_fn: Callable[[str], None] = _noop) -> None:
    """Write the recommended-mod id list into the TTW mod's meta.ini
    ``missing_requirements`` (filtered live against installed mods)."""
    from Nexus.nexus_meta import read_meta, write_meta

    mod_dir = ttw_mod_dir(game)
    if mod_dir is None:
        return
    meta_path = mod_dir / "meta.ini"
    if not meta_path.is_file():
        log_fn("TTW meta.ini not found — skipping requirement seeding.")
        return
    meta = read_meta(meta_path)
    meta.missing_requirements = ";".join(f"{mid}:" for mid in TTW_REQUIRED_MOD_IDS)
    write_meta(meta_path, meta)
    log_fn(f"seeded {len(TTW_REQUIRED_MOD_IDS)} recommended mod(s) into the "
           "TTW requirements list.")

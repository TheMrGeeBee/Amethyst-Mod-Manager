"""
base_game.py
Abstract base class that all game handlers must subclass.

To add support for a new game:
  1. Create a new .py file in the Games/ directory
  2. Subclass BaseGame and implement all abstract methods/properties
  3. Drop the file in — it will be auto-discovered by Utils/game_loader.py
"""

from __future__ import annotations

import json
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from Utils.config_paths import get_game_config_dir, get_game_config_path
from Utils.deploy import LinkMode
from Utils.steam_finder import find_prefix as _find_steam_prefix

if TYPE_CHECKING:
    from typing import Any


# Shared serialisation tables for load_paths/save_paths. Note that we map both
# "symlink" and "copy" → SYMLINK on read — historically the user-facing "copy"
# option was renamed to symlink years ago and the saved string can be either.
_DEPLOY_MODE_FROM_STR: dict[str, LinkMode] = {
    "symlink": LinkMode.SYMLINK,
    "copy":    LinkMode.SYMLINK,
}
_DEPLOY_MODE_TO_STR: dict[LinkMode, str] = {
    LinkMode.SYMLINK: "symlink",
    LinkMode.COPY:    "copy",
}
_DEFAULT_DEPLOY_MODE = LinkMode.HARDLINK


# ---------------------------------------------------------------------------
# Wizard tool descriptor
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WizardTool:
    """Describes a single wizard tool available for a game.

    Attributes:
        id:          Unique machine-readable key, e.g. ``"downgrade_fo3"``.
        label:       Short human-readable name shown on the button.
        description: One-line explanation shown below the label.
        dialog_class_path:  Dotted import path to the dialog class,
                            e.g. ``"wizards.fallout_downgrade.FalloutDowngradeWizard"``.
                            Resolved lazily at runtime so game modules don't
                            need to import heavy GUI code at load time.
        extra:       Arbitrary keyword arguments forwarded to the dialog
                     constructor.  Lets a single dialog class serve multiple
                     games with different configuration (URLs, keywords, etc.).
    """
    id: str
    label: str
    description: str = ""
    dialog_class_path: str = ""
    extra: dict = field(default_factory=dict)
    category: str = ""  # optional grouping header in the wizard picker; inferred if empty


class BaseGame(ABC):

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Human-readable display name, e.g. 'Skyrim Special Edition'.
        Must match the subfolder name under Games/ and Profiles/.
        """

    @property
    @abstractmethod
    def game_id(self) -> str:
        """
        Filesystem-safe identifier, e.g. 'skyrim_se'.
        Typically matches the handler's .py filename (without extension).
        """

    @property
    @abstractmethod
    def exe_name(self) -> str:
        """
        The game's executable filename used to locate it in Steam libraries.
        e.g. 'SkyrimSELauncher.exe'
        """

    @property
    def exe_name_alts(self) -> list[str]:
        """
        Additional executable paths to try when auto-detecting the game install.
        Used for games that ship different exe paths depending on the store
        (e.g. Steam vs Epic/Heroic).  Checked after ``exe_name``.
        """
        return []

    @property
    def default_deploy_mode(self) -> str:
        """
        The deploy method pre-selected in the configure dialog.
        Returns ``"symlink"`` by default.  Override to ``"hardlink"`` for games
        where symlinks are unsupported (e.g. Cyberpunk 2077 with CET mods).
        The configure dialog will show "(Recommended)" next to whichever option
        this returns.
        """
        return "symlink"

    @property
    def root_folder_deploy_enabled(self) -> bool:
        """
        Whether Root_Folder deployment is supported for this game.
        When True (the default), the GUI will transfer files from
        Profiles/<game>/Root_Folder/ into the game's install root on deploy
        (subject to the per-session checkbox in the mod list).
        Set to False in a game handler to permanently disable this feature
        regardless of the checkbox state (e.g. for games where writing to the
        install root would be unsafe or unsupported).
        """
        return True

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        """
        Lowercase top-level folder names to strip from mod files during filemap
        building.  Useful for games where mod authors sometimes wrap their files
        in a redundant top-level folder that the game already provides.

        Example: Subnautica mods are installed into BepInEx/Plugins/, but some
        authors ship their mod as plugins/MyMod/MyMod.dll.  Declaring {"plugins"}
        here causes that leading "plugins/" segment to be stripped so the file
        lands at MyMod/MyMod.dll inside the target directory.

        Stripping is applied only to the first path segment and only when it
        matches one of these names (case-insensitive).  Mods whose files do not
        start with a listed prefix are unaffected.

        Return an empty set (the default) to disable stripping entirely.
        """
        return set()

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        """
        Lowercase filenames that are excluded from conflict detection.

        Files whose name (not path) matches an entry here are counted in the
        filemap as normal but do not contribute to a mod's conflict status.
        Useful for metadata files that many mods ship (e.g. modinfo.ini) which would otherwise cause spurious conflict markers.

        Return an empty set (the default) to disable.
        """
        return set()

    @property
    def excluded_loose_filenames(self) -> set[str]:
        """
        Lowercase filename glob patterns that are excluded from the filemap
        entirely, but only when the file is *loose* (sits at the top level of
        a mod with no parent folder).

        Unlike conflict_ignore_filenames (which keeps files in the filemap and
        only skips conflict tracking), matched loose files are dropped from the
        filemap and never deployed.  Files of the same name nested inside a
        folder are unaffected.

        Useful for generic loose readme/spawn-command .txt files that can crash
        a game, while preserving identically-extensioned files (e.g. .txt
        localization) that live inside folders.

        Return an empty set (the default) to disable.
        """
        return set()

    @property
    def archive_extensions(self) -> frozenset[str]:
        """
        File extensions of game-specific archive formats (e.g. ``.bsa``,
        ``.ba2``) whose contents should be scanned for archive-level
        conflict detection.

        When non-empty, the filemap rebuild also parses the table-of-contents
        of matching archive files inside each mod's staging folder and computes
        which files inside archives overlap between mods.

        Return an empty frozenset (the default) to disable archive conflict
        detection entirely.  Only Bethesda-family games need this.
        """
        return frozenset()

    @property
    def filemap_exclude_dirs(self) -> frozenset[str]:
        """
        Lowercase top-level directory names inside a mod folder that are
        completely excluded from filemap scanning.  Files inside these
        directories are never indexed, never appear in the filemap, and are
        never deployed to the game's data directory.

        The default includes ``"fomod"`` so that FOMOD installer metadata
        (ModuleConfig.xml, screenshots, etc.) stored in every FOMOD-capable
        mod's staging folder is never surfaced as a deployable game file.

        Override and extend to suppress additional per-game metadata dirs.
        """
        return frozenset({"fomod"})

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        """
        Like mod_folder_strip_prefixes, but applied AFTER mod_required_top_level_folders
        and mod_auto_strip_until_required have run at install time.

        Use this when the folder to strip is the same one declared in
        mod_required_top_level_folders — so auto-strip can first normalise the
        mod structure down to that folder, then this property removes it.

        Example: REFramework mods must contain a top-level reframework/ folder
        (validated by mod_required_top_level_folders), which is then stripped
        here so files are staged as bare content paths.

        Also applied during filemap building (same as mod_folder_strip_prefixes).

        Return an empty set (the default) to disable.
        """
        return set()

    @property
    def mod_install_prefix(self) -> str:
        """
        A forward-slash path segment automatically prepended to every file's
        destination when a mod is installed.  Applied during install before the
        mod_required_top_level_folders check, so the check sees the final path.

        Example: Witcher 3 mods normally live in <game>/mods/<ModName>/…
        Declaring "mods" here means a mod shipped as ModName/content/… lands at
        mods/ModName/content/… without the user having to do anything.

        Return an empty string (the default) to disable prepending entirely.
        """
        return ""

    @property
    def mod_install_extensions(self) -> set[str]:
        """
        When non-empty, only files whose extension (lowercase, including the
        leading dot) appears in this set will be included in the filemap.
        Files with other extensions are silently excluded during filemap build.

        Example: Baldur's Gate 3 mods only need .pak files in the Mods folder;
        declaring {".pak"} here filters out .txt readme files, images, etc.

        Return an empty set (the default) to include all files.
        """
        return set()

    @property
    def mod_root_deploy_folders(self) -> set[str]: # Legacy, Use custom routing rules instead
        """
        Lowercase top-level folder names inside a mod that should be deployed
        to the game's root directory instead of the normal mod data path.

        During filemap building, files whose first path segment (after
        strip-prefix processing) matches one of these names are written to a
        separate ``filemap_root.txt`` and bypass the ``mod_install_extensions``
        filter.  The game's ``deploy()`` method is responsible for deploying
        that file into the game root.

        Example: Baldur's Gate 3 mods normally install ``.pak`` files into the
        Proton-prefix Mods folder, but some mods ship a ``bin/`` folder that
        must land in the game's install root.  Declaring ``{"bin"}`` here
        routes those files accordingly.

        Return an empty set (the default) to disable this behaviour.
        """
        return set()

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        """
        Lowercase top-level folder names that are valid install roots for this
        game's mods.  When non-empty, the installer checks whether a mod
        contains at least one of these as its top-level directory.  If none
        match, the user is prompted to set a prefix path before the mod is
        installed (MO2-style "Set data directory").

        Example: Cyberpunk 2077 mods must live under archive/, bin/, r6/, or
        red4ext/ — declaring these here causes a warning dialog to appear when
        an author ships loose files with no recognised top-level folder.

        Return an empty set (the default) to disable this check entirely.
        """
        return set()

    @property
    def mod_auto_strip_until_required(self) -> bool:
        """
        When True and mod_required_top_level_folders is non-empty, the
        installer will try to strip leading path segments (one or more
        top-level folders) until at least one remaining top-level folder
        is in mod_required_top_level_folders, instead of showing the
        prefix dialog. When False (the default), the dialog is shown
        when no required top-level folder is found.
        """
        return False

    @property
    def filemap_exclude_unknown_top_level(self) -> bool:
        """
        When True (and mod_required_top_level_folders is non-empty), any
        filemap entry whose top-level path segment is not one of the
        mod_required_top_level_folders is dropped from the filemap entirely
        (and therefore from deployment and the Data tab).

        Use for games like Cyberpunk 2077 where authors ship extra top-level
        folders (e.g. screenshots, "aboutMods", source dumps) that must not be
        deployed into the game root.  Loose top-level files (no folder) are
        also excluded under this rule.

        Return False (the default) to keep every entry regardless of its
        top-level folder.
        """
        return False

    @property
    def mod_required_file_types(self) -> set[str]:
        """
        File extensions (e.g. {".esp", ".esm", ".esl"}) that are recognised as
        valid top-level content for this game's mods.

        Can be used in two ways:

        1. Alongside mod_required_top_level_folders (fallback): if the
           top-level folder check fails, the installer checks whether the mod
           contains a file with one of these extensions at its root (or can be
           stripped down to one) before falling through to the prefix dialog.

        2. Standalone (without mod_required_top_level_folders): if no required
           top-level folders are declared, this check runs on its own — the mod
           must have a qualifying file at its top level, or the auto-strip /
           prefix-dialog / install-as-is fallbacks apply as normal.

        mod_auto_strip_until_required and mod_install_as_is_if_no_match both
        apply in either mode.

        Return an empty set (the default) to disable this check entirely.
        """
        return set()

    @property
    def additional_install_logic(self) -> list:
        """
        List of callables for game-specific post-install steps.

        Each callable is invoked as fn(dest_root, mod_name, log_fn) after the
        mod files are copied to staging. Use for game-specific file moves or
        transformations (e.g. moving loose .archive files to archive/pc/mod
        for Cyberpunk 2077).

        Return an empty list (the default) for no additional logic.
        """
        return []

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        """
        When True, if both mod_required_top_level_folders and
        mod_required_file_types checks fail for a mod, the mod is installed
        as-is without stripping any folders and without showing the prefix
        dialog.  Use this for games where some mods have a predictable
        structure and others are completely free-form.

        When False (the default), the prefix dialog is shown instead.
        """
        return False

    @property
    def supports_bain(self) -> bool:
        """
        When True, the installer runs BAIN (Wrye Bash bundled archive)
        detection and shows the sub-package picker for archives that look
        like a complex BAIN package.

        False by default: BAIN packages are only authored for Bethesda
        games, and the Bethesda-centric heuristics misfire on other games'
        loose mods (a stray readme .txt or a coincidentally-named "Textures"
        folder is enough). Bethesda game handlers override this to True.
        """
        return False

    @property
    def collections_disabled(self) -> bool:
        """
        When True, the Collections button is hidden for this game and incoming
        nxm:// collection links are silently ignored.

        Defaults to False.  Set to True in game handlers that do not support
        Nexus Collections (e.g. games without a Nexus Collections catalogue).
        """
        return False

    @property
    def mod_supports_bundles(self) -> bool:
        """
        When True, the installer detects bundle mods — archives whose top-level
        subfolders each contain a ``modinfo.ini`` with the same ``nameasbundle``
        value — and installs every variant as a separate staged mod named
        ``<bundle_name>__<variant_name>``.  The mod list then shows them as a
        radio-button group where enabling one variant auto-disables the others.

        Defaults to False.  Enable in game handlers that use the Fluffy-style
        bundle format (e.g. Resident Evil Village).
        """
        return False

    @property
    def mod_deploy_path_remap(self) -> dict[str, str]:
        """
        Prefix substitutions applied to deployed file paths (destination only).

        Keys are lowercase source prefixes; values are the replacement prefix.
        Applied before the file is written to the game root.

        Example: RE2/RE3 ship mods with ``natives/x64/`` paths but the game
        installs files under ``natives/STM/``.  Declaring
        ``{"natives/x64/": "natives/STM/"}`` here causes the deploy to write
        files to the correct location while the filemap still records the
        original ``natives/x64/`` relative path.

        Defaults to an empty dict (no remapping).
        """
        return {}

    @property
    def pak_hash_extension_remap(self) -> dict[str, str]:
        """
        File extension substitutions applied when computing PAK hashes.

        Some RE Engine games store assets with a different format-version suffix
        in the PAK than what mod authors ship.  For example, RE2/RE3 store
        textures as ``.tex.34`` inside the PAK, but mods are distributed with
        ``.tex.10``.  Declaring ``{".tex.10": ".tex.34"}`` here causes the
        patcher to hash the remapped filename when searching the PAK.

        Defaults to an empty dict (no remapping).
        """
        return {}

    @property
    def filemap_conflict_key_fn(self) -> "Callable[[str], str] | None":
        """Return a function that normalises a filemap rel_key for conflict detection.

        When two staged paths produce the same conflict key they are treated as
        the same file.  This is needed when ``mod_deploy_path_remap`` or
        ``pak_hash_extension_remap`` map different staged paths to the same
        deployed path (e.g. ``natives/x64/foo.tex.10`` and
        ``natives/STM/foo.tex.34`` both land at ``natives/STM/foo.tex.34``).

        Returns None (the default) when no normalisation is needed.
        """
        _path_remap = self.mod_deploy_path_remap
        _ext_remap = self.pak_hash_extension_remap
        if not _path_remap and not _ext_remap:
            return None
        _prefix_pairs = [(k.lower(), v.lower()) for k, v in _path_remap.items()]
        _ext_pairs = [(k.lower(), v.lower()) for k, v in _ext_remap.items()]

        def _normalise(rel_key: str) -> str:
            k = rel_key.lower()
            for old_p, new_p in _prefix_pairs:
                if k.startswith(old_p):
                    k = new_p + k[len(old_p):]
                    break
            for old_e, new_e in _ext_pairs:
                if k.endswith(old_e):
                    k = k[: len(k) - len(old_e)] + new_e
                    break
            return k

        return _normalise

    @property
    def normalize_folder_case(self) -> bool:
        """
        When True (the default), folder segments that differ only in case across
        mods are unified to a single canonical casing (most-uppercase wins).
        This is correct for case-insensitive games (Windows/Bethesda etc.) where
        ``Scripts/`` and ``scripts/`` are the same directory.

        Set to False for games whose mod loader runs on a case-sensitive file
        system and respects the exact folder names provided by mod authors
        (e.g. Stardew Valley on Linux, where ``Music/`` and ``music/`` are
        different directories).  When False, folder names in the filemap are
        left exactly as each mod provides them — no cross-mod unification.
        """
        return True

    @property
    def filemap_casing(self) -> str:
        """
        How to pick the canonical casing for folders/filenames in the filemap
        when ``normalize_folder_case`` is True. Ignored when normalization is
        off. One of:

          "upper"        — prefer the variant with more uppercase letters
                           (default; correct for most Windows-shipped mods).
          "lower"        — prefer the variant with more lowercase letters
                           (Cyberpunk 2077: REDengine reads ``archive/pc/mod``
                           lowercase — using ``Mod`` for one mod and ``mod``
                           for another would split into two real dirs on
                           case-sensitive Linux filesystems).
          "force_lower"  — every folder segment is lowercased regardless of
                           how mod authors shipped it. Filenames untouched.
          "force_upper"  — every folder segment is uppercased. Filenames
                           untouched. Mostly here for symmetry — rarely
                           useful in practice.
        """
        return "upper"

    @property
    def mod_staging_requires_subdir(self) -> bool:
        """
        When True, each mod's staging folder must contain a named subdirectory
        at its top level — loose files must NOT sit at the staging folder root.

        This applies to games like Stardew Valley where the mod loader requires
        mods to live in <game>/Mods/<ModName>/.  The staging structure must
        therefore be mods/<StagingName>/<ModName>/... so the filemap records
        <ModName>/... and deploys to Mods/<ModName>/....

        A common mistake is copying an existing Mods/<ModName>/ folder directly
        into staging (resulting in mods/<ModName>/manifest.json at root).  When
        this flag is True the mod list panel automatically wraps the contents of
        any flat staging folder inside a subdirectory named after the folder
        before building the filemap, preventing silent mis-deployment.

        Return False (the default) to disable this behaviour.
        """
        return False

    @property
    def mod_staging_wrap_signals(self) -> "tuple[set[str], set[str]]":
        """Marker (filenames, extensions) that flag a flat mod as needing wrapping.

        Only consulted when ``mod_staging_requires_subdir`` is True.  Returns a
        tuple of (lowercase filenames, lowercase extensions incl. dot).  A flat
        staging folder is wrapped into ``<ModName>/`` if any root-level file
        matches.  Default matches ``manifest.json`` (Stardew/SMAPI).
        """
        return ({"manifest.json"}, set())

    @property
    def mod_staging_already_structured_markers(self) -> "set[str]":
        """Marker filenames that mark a mod as already correctly structured.

        Only consulted when ``mod_staging_requires_subdir`` is True.  If any
        immediate subdirectory of a mod contains one of these files, the mod is
        left untouched even if a wrap signal is present at the root — the loose
        root file is a sibling (e.g. a JA3 Packs ``.hpk`` next to an existing
        ``<ModName>/metadata.lua`` folder), not a flat mod.  Empty (the default)
        disables the guard.
        """
        return set()

    @property
    def frameworks(self) -> dict[str, str]:
        """
        A mapping of framework display names to their executable filenames.

        The plugin panel checks whether each executable exists in the game's
        root directory **or** in the active profile's Root_Folder staging
        directory, and displays a status banner at the top of the Plugins tab:
          • Green  — "Script Extender Installed"
          • Red    — "Script Extender Not Present"

        Example (Skyrim SE):
            {"Script Extender": "skse64_loader.exe"}

        Return an empty dict (the default) to show no banners.
        """
        return {}

    def add_deploy_warning(self, message: str) -> None:
        """Queue a user-facing warning for the GUI to toast after deploy."""
        if not hasattr(self, "_deploy_user_warnings"):
            self._deploy_user_warnings: list[str] = []
        self._deploy_user_warnings.append(message)

    def pop_deploy_warnings(self) -> list[str]:
        """Return and clear queued deploy warnings."""
        out = getattr(self, "_deploy_user_warnings", [])
        self._deploy_user_warnings = []
        return out

    def get_launch_command(self) -> "list[str] | None":
        """Return a native Linux command used to launch this game, bypassing Proton.

        When non-None, the plugin panel will use this command for the Play
        button instead of the normal exe-via-Proton path.  The command is a
        list suitable for subprocess.Popen, e.g.::

            ["flatpak", "run", "org.openmw.OpenMW"]

        Return None (the default) to use the normal Proton launch path.
        """
        return None

    @property
    def preferred_launch_exe(self) -> str:
        """
        Optional game-root-relative path to an alternative executable that
        should be shown first in the exe dropdown and treated as the launch
        exe (green Play button), but ONLY when the file is present on disk.

        Use this for games where a script extender or mod loader must be
        launched instead of the normal game executable, but where replacing
        the original exe on disk would cause errors (e.g. Oblivion Remastered
        with obse64_loader.exe).

        When the file at this path does not exist, the normal ``exe_name``
        is used as the launch exe instead.

        Return an empty string (the default) to always use ``exe_name``.
        """
        return ""

    @property
    def steam_id(self) -> str:
        """
        Steam App ID for this game, e.g. '377160' for Fallout 4.
        Used to locate the game's Steam compatibility data / Proton prefix.
        Return an empty string for non-Steam games or if not applicable.
        """
        return ""

    @property
    def alt_steam_ids(self) -> list[str]:
        """
        Additional Steam App IDs for alternate editions of this game
        (e.g. GOTY, Complete Edition) that share the same game folder and
        Proton prefix layout.  Checked when the primary steam_id prefix is
        not found.  Return an empty list if there are no alternates.
        """
        return []

    def effective_steam_id(self) -> str:
        """Return the Steam App ID that is actually installed for this game.

        Some games ship under more than one App ID (e.g. localized editions or
        GOTY bundles) that share the same install layout but require launching
        through the App ID the user actually owns.  ``steam_id`` is the primary
        ID; :meth:`alt_steam_ids` lists the alternates.

        The configured Proton prefix lives under
        ``compatdata/<app_id>/pfx``, so when a prefix is set we read the App ID
        straight out of that path and use it if it matches one of this game's
        known IDs.  Falls back to the primary ``steam_id`` otherwise.
        """
        known = [self.steam_id, *self.alt_steam_ids]
        known = [str(s) for s in known if s]
        prefix = self.get_prefix_path()
        if prefix is not None:
            # prefix is .../compatdata/<app_id>/pfx (or the compatdata/<app_id>
            # dir itself); walk up looking for a part that matches a known ID.
            for part in prefix.parts[::-1]:
                if part in known:
                    return part
        return self.steam_id

    @property
    def heroic_app_names(self) -> list[str]: # Legacy, App names are now detected automatically
        """
        Heroic Games Launcher app identifiers for this game.

        Used as a fallback when the game is not found in Steam libraries.
        Heroic uses different identifiers per store:
          - Epic Games: the 'appName' string from the Epic catalogue
            e.g. 'Pewee' for Cyberpunk 2077
          - GOG: the numeric product ID as a string, or the exact game title
            e.g. '1207658924' or 'The Witcher 3: Wild Hunt'

        List multiple values if the game may appear under different IDs or
        across stores, e.g. ['Pewee', '1207658924'].

        Return an empty list (the default) to disable Heroic detection for
        this game.
        """
        return []

    def get_prefix_path(self) -> Path | None:
        """
        Return the saved Proton prefix path (the pfx/ directory) for this game,
        or None if not set.  Subclasses persist this in paths.json.
        """
        return None

    def set_prefix_path(self, path: "Path | str | None") -> None:
        """
        Save the Proton prefix path and persist it to paths.json.
        Subclasses should override this to write it alongside game_path.
        """

    @property
    def plugin_extensions(self) -> list[str]:
        """
        File extensions that this game treats as plugins (loaded by the engine).
        e.g. ['.esp', '.esl', '.esm'] for Bethesda games.
        Return an empty list to disable all plugin tracking for this game.
        Subclasses override this to enable plugin panel functionality.
        """
        return []

    @property
    def plugins_use_star_prefix(self) -> bool:
        """
        Whether plugins.txt uses the MO2-style '*Name' prefix for enabled plugins.
        Most Bethesda games use True. Oblivion Remastered uses False (bare names only).
        """
        return True

    # Whether the engine loads all master-group plugins (.esm/.esl extension
    # or master header flag) before all non-masters. The plugins panel then
    # enforces that partition like MO2/Wrye Bash do.
    plugins_master_block: bool = False

    @property
    def plugins_include_vanilla(self) -> bool:
        """
        Whether vanilla (base-game) plugins should be written into plugins.txt.
        Standard Bethesda games exclude them (the engine handles them separately).
        Oblivion Remastered requires all plugins, including vanilla, to be listed.
        """
        return False

    @property
    def vanilla_plugins(self) -> list[str]:
        """Hardcoded vanilla plugin filenames for this game."""
        return []

    @property
    def vanilla_dlc_plugins(self) -> list[str]:
        """Official DLC plugin filenames (mirrors MO2's DLCPlugins())."""
        return []

    @property
    def vanilla_ccc_filename(self) -> str | None:
        """Filename of the CC manifest in <game_path>, or None."""
        return None

    @property
    def supports_esl_flag(self) -> bool:
        """
        Whether this game supports the ESL (light plugin) flag in TES4 plugin headers.
        ESL plugins occupy their own FormID space and don't count against the 255‑plugin limit.
        Supported by: Fallout 4, Fallout 4 VR, Skyrim SE/AE, Skyrim VR, Enderal SE, Starfield.
        NOT supported by: Fallout 3, Fallout NV, Oblivion, Skyrim LE, Enderal LE, Morrowind.
        """
        return False

    @property
    def loot_sort_enabled(self) -> bool:
        """
        Whether LOOT plugin sorting is supported for this game.
        Return False for games that don't use or need LOOT (e.g. Subnautica).
        When False, the Sort Plugins button will do nothing.
        Subclasses that support LOOT should override this to return True
        and also provide loot_game_type and loot_masterlist_url.
        """
        return False

    @property
    def loot_game_type(self) -> str:
        """
        The libloot GameType attribute name for this game, e.g. 'SkyrimSE'.
        Only used when loot_sort_enabled is True.
        Must match an attribute of loot.GameType in the libloot Python bindings.
        """
        return ""

    @property
    def loot_masterlist_repo(self) -> str:
        """
        Repo slug under github.com/loot/ that hosts this game's masterlist
        (e.g. 'skyrimse', 'fallout4', 'fallout4vr').

        When set, the masterlist URL is built dynamically to match the bundled
        libloot version (loot_sorter.masterlist_url_for_repo), with a
        walk-down fallback to the most recent available branch. This is the
        preferred way to declare masterlist hosting — `loot_masterlist_url`
        is kept as a legacy fallback for repos that don't follow the
        standard branch-per-libloot-version convention.

        Return an empty string to use `loot_masterlist_url` instead.
        """
        return ""

    @property
    def loot_masterlist_url(self) -> str:
        """
        Legacy: full URL to download the LOOT masterlist YAML for this game.
        Only used as a fallback when `loot_masterlist_repo` is not set.
        e.g. 'https://raw.githubusercontent.com/loot/skyrimse/v0.21/masterlist.yaml'
        The masterlist is stored as ~/.config/AmethystModManager/LOOT/data/masterlist_<game_id>.yaml.
        Return an empty string if no masterlist URL is known.
        """
        return ""

    @property
    def nexus_game_domain(self) -> str:
        """
        Nexus Mods game domain name used for API requests.
        e.g. 'skyrimspecialedition', 'cyberpunk2077', 'baldursgate3'
        This is the subdomain used in URLs like nexusmods.com/<domain>/mods/...
        Return an empty string to disable Nexus integration for this game.
        """
        return ""

    @property
    def wine_dll_overrides(self) -> dict[str, str]:
        """
        Wine DLL overrides to apply to the Proton prefix on every deploy.

        Maps DLL name → load order string using Wine's notation:
          ``"native,builtin"``  — try the Windows DLL first, then Wine's
          ``"native"``          — Windows DLL only
          ``"builtin"``         — Wine's built-in only
          ``"disabled"``        — block the DLL entirely

        These are written into ``user.reg`` under
        ``[Software\\\\Wine\\\\DllOverrides]`` each time ``deploy()`` runs,
        so users do not need to configure winecfg manually.

        Example (BepInEx)::

            return {"winhttp": "native,builtin"}

        Return an empty dict (the default) to leave the prefix unchanged.
        """
        return {}

    @property
    def winetricks_components(self) -> list[str]:
        """
        Winetricks components to install automatically when this game is first
        added (i.e. when the user clicks Add/Confirm in the configure dialog).

        Each entry is a winetricks verb, e.g. ``"d3dcompiler_47"`` or
        ``"vcrun2022"``.  Installation is skipped silently when no Proton prefix
        is available for the game.

        Return an empty list (the default) to skip automatic installation.
        """
        return []

    @property
    def auto_install_deps(self) -> list[str]:
        """
        Prefix dependencies to install automatically when this game is first
        added, using the *same installers the Proton Tools menu uses* (not
        winetricks). Supported keys: ``"vcredist"`` (Microsoft's official
        vc_redist.x64.exe, run silently via Proton) and ``"d3dcompiler_47"``
        (the Mozilla fxc2 Win 8.1 DLL drop).

        These run silently in the background and are skipped per-dep when the
        prefix's amethyst_deps.json already records them, or when no Proton
        prefix is available. Prefer this over ``winetricks_components`` — many
        users lack a working winetricks/cabextract setup, and the winetricks
        vcredist/d3dcompiler verbs are also less reliable for DLL mods.

        Return an empty list (the default) to skip automatic installation.
        """
        return []

    @property
    def custom_routing_rules(self) -> list:
        """
        A list of CustomRule objects (from Utils.deploy) that route specific
        file types to a game-root-relative destination directory during deploy.

        Files matching a rule are placed under game_root / rule.dest and are
        excluded from the normal deploy destination. Placement depends on
        rule.flatten: when True the file lands as a bare filename under dest;
        when False (default) the mod-relative path is preserved under dest.

        Example (RE Engine .pak files, flattened to one folder)::

            from Utils.deploy import CustomRule
            return [CustomRule(dest="pak_mods", extensions=[".pak"], flatten=True)]

        Return an empty list (the default) to use normal routing for all files.
        """
        return []

    @property
    def restore_before_deploy(self) -> bool:
        """
        When True (the default), the GUI runs restore() before deploy() when
        the user clicks Deploy, to clean the game state first. Set to False
        for games where deploy() itself restores then applies mods in one
        cycle (e.g. unpack → remove modded → add mods → repack).
        """
        return True

    @property
    def restore_on_close_eligible(self) -> bool:
        """When False, the 'restore on close' setting skips this game.

        Override to False for games where restore is expensive (e.g. gpak
        repack) and should only run when the user explicitly clicks Restore.
        """
        return True

    @property
    def reshade_dll(self) -> str | None:
        """
        Suggested DLL name that ReShade should be installed as.

        This is only a *hint* used to pre-select the right option in the
        ReShade wizard — the user can always change it there.  The correct
        value depends on the graphics API the game uses:
          - DirectX 9          → ``"d3d9.dll"``
          - DirectX 10/11/12   → ``"dxgi.dll"``
          - OpenGL             → ``"opengl32.dll"``

        The ReShade wizard is offered for every game regardless of this
        value; return ``None`` (the default) to leave the wizard's DLL
        choice unset so the user picks it manually (e.g. custom games).
        """
        return None

    @property
    def reshade_arch(self) -> int | None:
        """
        Suggested executable architecture for ReShade: ``32`` or ``64``.

        Like :attr:`reshade_dll`, this only pre-selects the matching option
        in the wizard; the user can override it.  Controls whether
        ``ReShade32.dll`` or ``ReShade64.dll`` is extracted from the
        installer.  Override to ``32`` for legacy 32-bit games (e.g.
        Fallout 3, Fallout New Vegas, Oblivion, Skyrim classic).

        Return ``None`` (the default) to leave the choice unset so the user
        picks it manually.
        """
        return None

    def reshade_install_subdir(self, game_path: "Path") -> "Path | None":
        """Return the game-root-relative folder where ReShade (the proxy DLL,
        ReShade.ini, preset and ``reshade-shaders/``) must be installed.

        ReShade has to sit next to the *rendering* executable so the graphics
        API proxy DLL gets loaded.  For most games that is the folder holding
        :attr:`exe_name`.  Games whose ``exe_name`` is a bootstrap launcher
        sitting above the real binary (e.g. Unreal Engine titles, where the
        shipping exe lives under ``<Project>/Binaries/Win64/``) override this
        to point at the actual binary's directory.

        *game_path* is the resolved install root, so implementations can probe
        the on-disk layout.  Return ``None`` to mean "the game root itself"
        (no subdir).
        """
        exe = self.exe_name or ""
        subdir = Path(exe).parent if exe else Path(".")
        return None if subdir == Path(".") else subdir

    @property
    def wizard_tools(self) -> list[WizardTool]:
        """
        Per-game helper tools shown in the Wizard dialog.

        Override this in a game subclass to register tools that aid with
        game-specific setup tasks (e.g. downgrading, patching, installing
        runtimes).  Each entry is a :class:`WizardTool` whose
        ``dialog_class_path`` points to the CTkToplevel that implements the
        multi-step wizard.

        Subclasses that add their own tools should extend via::

            @property
            def wizard_tools(self):
                return self._base_wizard_tools() + [WizardTool(...)]

        Return an empty list (the default) to hide the Wizard button for
        this game.
        """
        return self._base_wizard_tools()

    def _base_wizard_tools(self) -> list[WizardTool]:
        """Return the framework-level wizard tools (e.g. ReShade).

        Subclasses that override :attr:`wizard_tools` and need to include
        the auto-generated base tools (such as Install ReShade) should call
        this method rather than ``super().wizard_tools``, to avoid
        accidentally inheriting intermediate class tools::

            @property
            def wizard_tools(self):
                return self._base_wizard_tools() + [WizardTool(...)]
        """
        tools: list[WizardTool] = []
        tools.append(WizardTool(
            id="install_reshade",
            label="Install ReShade",
            description="Download and install ReShade into the game folder.",
            dialog_class_path="wizards.reshade.ReShadeWizard",
            extra={"reshade_dll": self.reshade_dll, "reshade_arch": self.reshade_arch},
        ))
        return tools

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    @abstractmethod
    def get_game_path(self) -> Path | None:
        """
        Return the root install directory of the game, or None if not set.
        e.g. /home/deck/.steam/steamapps/common/Skyrim Special Edition
        """

    @abstractmethod
    def get_mod_data_path(self) -> Path | None:
        """
        Return the directory inside the game where mod files are installed.
        e.g. for Skyrim SE: <game_path>/Data
        Returns None if game_path is not configured.
        """

    def get_vanilla_plugins_path(self) -> Path | None:
        """
        Return the directory that contains the game's own (vanilla) plugin files.
        Used to detect which .esp/.esm files belong to the base game so they can
        be shown as non-disableable entries in the plugins panel.

        Defaults to get_mod_data_path(). Override for games whose vanilla plugins
        live in a different subdirectory (e.g. Oblivion Remastered).
        """
        return self.get_mod_data_path()

    @abstractmethod
    def get_mod_staging_path(self) -> Path:
        """
        Return the path where this manager stages installed mods before
        linking them into the game. Always returns a Path regardless of
        whether the directory exists yet.
        e.g. Profiles/Skyrim Special Edition/mods/
        """

    def get_hardlink_deploy_targets(self) -> list[tuple[str, "Path | None"]]:
        """
        Return a list of (label, path) pairs for directories that must be on
        the same filesystem as the staging folder when using hardlinks.

        The default returns just the game directory.  Games that also deploy
        into the Proton prefix (e.g. The Sims 4, BG3) should override this.
        """
        return [("Game directory", self.get_game_path())]

    def get_profile_root(self) -> Path:
        """
        Return the root directory that contains the profiles/ folder.

        - Default (no custom staging): the parent of get_mod_staging_path(),
          i.e. Profiles/<game>/ so that profiles/ sits alongside mods/.
        - Custom staging path: the staging path itself is the root, so
          profiles/ lives inside it (e.g. /mnt/ssd/MySkyrimMods/profiles/).
        """
        if self._staging_path is not None:
            return self._staging_path
        return self.get_mod_staging_path().parent

    # Active profile directory — set by the UI whenever the user switches profiles.
    _active_profile_dir: "Path | None" = None

    def set_active_profile_dir(self, profile_dir: "Path | None") -> None:
        """Record which profile folder is currently active.

        Call this whenever the user selects a profile so that
        :meth:`get_effective_mod_staging_path` can decide whether to route
        mods into the profile-specific folder or the shared folder.
        """
        self._active_profile_dir = profile_dir

    def get_effective_mod_staging_path(self) -> Path:
        """Return the mods staging path that should be used for the active profile.

        If the active profile has the ``profile_specific_mods`` flag set in
        ``profile_state.json`` (profile_settings), returns ``<profile_dir>/mods/`` so that all
        mod files are kept inside the profile folder itself.

        Otherwise falls back to the standard :meth:`get_mod_staging_path`
        (shared ``mods/`` folder next to ``profiles/``), which preserves the
        existing behaviour for all profiles without the flag.
        """
        if self._active_profile_dir is not None:
            try:
                # Import here to avoid a circular import at module level.
                from gui.game_helpers import profile_uses_specific_mods  # type: ignore
                if profile_uses_specific_mods(self._active_profile_dir):
                    return self._active_profile_dir / "mods"
            except Exception:
                pass
        return self.get_mod_staging_path()

    def get_effective_overwrite_path(self) -> Path:
        """Return the overwrite directory for the active profile.

        For profiles with the ``profile_specific_mods`` flag the overwrite
        folder lives inside the profile directory itself (a sibling of the
        profile-specific ``mods/`` folder).  For all other profiles it
        falls back to ``<profile_root>/overwrite/``, which is the original
        shared location that sits alongside the shared ``mods/`` folder.

        This is always consistent with :meth:`get_effective_mod_staging_path`:
        ``overwrite/`` is a sibling of ``mods/`` regardless of which root they
        live under.
        """
        return self.get_effective_mod_staging_path().parent / "overwrite"

    def get_effective_filemap_path(self) -> Path:
        """Return the filemap.txt path for the active profile.

        For profile-specific-mods profiles this is ``<profile_dir>/filemap.txt``
        so that each profile maintains its own independent filemap.
        For normal profiles it falls back to ``<profile_root>/filemap.txt``.
        """
        return self.get_effective_mod_staging_path().parent / "filemap.txt"

    def get_effective_root_folder_path(self) -> Path:
        """Return the Root_Folder staging path for the active profile.

        For profiles with the ``profile_specific_mods`` flag the Root_Folder
        lives inside the profile directory itself (a sibling of the
        profile-specific ``mods/`` folder).  For all other profiles it
        falls back to ``<profile_root>/Root_Folder/``, the original shared
        location that sits alongside the shared ``mods/`` folder.
        """
        if self._active_profile_dir is not None:
            try:
                from gui.game_helpers import profile_uses_specific_mods  # type: ignore
                if profile_uses_specific_mods(self._active_profile_dir):
                    return self._active_profile_dir / "Root_Folder"
            except Exception:
                pass
        return self.get_profile_root() / "Root_Folder"

    # -----------------------------------------------------------------------
    # Configuration persistence
    # -----------------------------------------------------------------------

    @property
    def _deploy_state_file(self) -> Path:
        """Path to deploy_state.json for this game.

        Stores the name of the last profile that was successfully deployed so
        that restore() can direct runtime-generated files (ShaderCache, saves,
        etc.) to the correct overwrite folder even when the user has since
        switched to a different profile.
        """
        return self._paths_file.parent / "deploy_state.json"

    def get_last_deployed_profile(self) -> str:
        """Return the name of the last successfully deployed profile, or 'default'."""
        try:
            data = json.loads(self._deploy_state_file.read_text(encoding="utf-8"))
            return data.get("last_deployed") or "default"
        except (OSError, ValueError):
            return "default"

    def get_deploy_active(self) -> bool:
        """Return True if mods are currently deployed to the game folder."""
        try:
            data = json.loads(self._deploy_state_file.read_text(encoding="utf-8"))
            return bool(data.get("deploy_active", False))
        except (OSError, ValueError):
            return False

    def save_last_deployed_profile(self, profile_name: str) -> None:
        """Persist profile_name as the last successfully deployed profile."""
        try:
            self._deploy_state_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(self._deploy_state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data["last_deployed"] = profile_name
            data["deploy_active"] = True
            self._deploy_state_file.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def clear_deploy_active(self) -> None:
        """Mark mods as no longer deployed to the game folder (e.g. after a restore)."""
        try:
            self._deploy_state_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(self._deploy_state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data["deploy_active"] = False
            self._deploy_state_file.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def get_last_active_profile(self) -> str:
        """Return the name of the last active profile for this game, or 'default'."""
        try:
            data = json.loads(self._deploy_state_file.read_text(encoding="utf-8"))
            return data.get("last_active_profile") or "default"
        except (OSError, ValueError):
            return "default"

    def save_last_active_profile(self, profile_name: str) -> None:
        """Persist profile_name as the last active profile for this game."""
        try:
            self._deploy_state_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                data = json.loads(self._deploy_state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data["last_active_profile"] = profile_name
            self._deploy_state_file.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    @property
    def _paths_file(self) -> Path:
        """Path to this game's paths.json in the user config directory.

        Resolves to: ~/.config/AmethystModManager/games/<game_name>/paths.json

        Stored outside the app bundle so it survives AppImage updates and
        works correctly when the AppImage filesystem is mounted read-only.
        """
        return get_game_config_path(self.name)

    @property
    def _settings_file(self) -> Path:
        """Path to this game's game_settings.json in the user config directory.

        Stores lightweight per-game UI preferences (e.g. auto_deploy) that are
        independent of the path configuration persisted in paths.json.

        Resolves to: ~/.config/AmethystModManager/games/<game_name>/game_settings.json
        """
        return get_game_config_dir(self.name) / "game_settings.json"

    def _load_settings(self) -> dict:
        try:
            if self._settings_file.exists():
                return json.loads(self._settings_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save_settings(self, data: dict) -> None:
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        self._settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @property
    def auto_deploy(self) -> bool:
        """If True, the manager will automatically deploy after every filemap rebuild."""
        return self._load_settings().get("auto_deploy", False)

    @auto_deploy.setter
    def auto_deploy(self, value: bool) -> None:
        data = self._load_settings()
        data["auto_deploy"] = bool(value)
        self._save_settings(data)

    @property
    def archive_invalidation(self) -> bool:
        """If True, archive invalidation is applied on deploy (Bethesda games)."""
        return self._load_settings().get("archive_invalidation", True)

    @archive_invalidation.setter
    def archive_invalidation(self, value: bool) -> None:
        data = self._load_settings()
        data["archive_invalidation"] = bool(value)
        self._save_settings(data)

    def _migrate_old_config(self) -> None:
        """One-time migration: copy paths.json from the old in-tree location.

        Previous versions stored config at Games/<name>/paths.json inside the
        project directory.  On first run after upgrading, this copies any
        existing file to the new ~/.config location so settings are preserved.
        The original file is left in place (harmless, and safe for rollback).
        """
        old_path = Path(__file__).parent / self.name / "paths.json"
        new_path = self._paths_file
        if old_path.is_file() and not new_path.is_file():
            shutil.copy2(old_path, new_path)

    # Subclass hooks for the shared load_paths/save_paths implementation.
    # Override _save_paths_extra() / _load_paths_extra() to persist additional
    # JSON keys (e.g. heroic_app_name, script_extender_swap) without rewriting the
    # whole boilerplate.
    def _save_paths_extra(self) -> dict:
        """Extra JSON fields to merge into save_paths() output. Override in
        subclasses that persist additional config beyond the standard set."""
        return {}

    def _load_paths_extra(self, data: dict) -> None:
        """Apply extra JSON fields read by load_paths(). Pair with
        :meth:`_save_paths_extra`."""
        return None

    def _find_prefix_for_load(self) -> "Path | None":
        """Locate a Proton prefix for this game during load_paths().

        Tries ``steam_id`` first, then any ``alt_steam_ids``. Subclasses with
        non-Steam prefix sources (Heroic-only games, etc.) can override.
        """
        for sid in [self.steam_id, *self.alt_steam_ids]:
            if not sid:
                continue
            found = _find_steam_prefix(sid)
            if found:
                return found
        return None

    def load_paths(self) -> bool:
        """Load path configuration from the user config directory.

        Returns True if a valid ``game_path`` was loaded, False otherwise.
        Subclasses customise this via :meth:`_load_paths_extra` and
        :meth:`_find_prefix_for_load` rather than overriding this method.
        """
        self._migrate_old_config()
        if not self._paths_file.exists():
            self._game_path = None
            self._prefix_path = None
            self._staging_path = None
            return False
        try:
            data = json.loads(self._paths_file.read_text(encoding="utf-8"))
            raw = data.get("game_path", "")
            if raw:
                self._game_path = Path(raw)
            raw_pfx = data.get("prefix_path", "")
            if raw_pfx:
                self._prefix_path = Path(raw_pfx)
            raw_mode = data.get("deploy_mode", "hardlink")
            self._deploy_mode = _DEPLOY_MODE_FROM_STR.get(raw_mode, _DEFAULT_DEPLOY_MODE)
            raw_staging = data.get("staging_path", "")
            if raw_staging:
                self._staging_path = Path(raw_staging)
            self._load_paths_extra(data)
            self._validate_staging()
            if not self._prefix_path or not self._prefix_path.is_dir():
                found = self._find_prefix_for_load()
                if found:
                    self._prefix_path = found
                    self.save_paths()
            return bool(self._game_path)
        except (json.JSONDecodeError, OSError):
            pass
        self._game_path = None
        self._prefix_path = None
        return False

    def save_paths(self) -> None:
        """Write current path configuration to the user config directory."""
        self._paths_file.parent.mkdir(parents=True, exist_ok=True)
        mode_str = _DEPLOY_MODE_TO_STR.get(self._deploy_mode, "hardlink")
        data = {
            "game_path":    str(self._game_path)    if self._game_path    else "",
            "prefix_path":  str(self._prefix_path)  if self._prefix_path  else "",
            "deploy_mode":  mode_str,
            "staging_path": str(self._staging_path) if self._staging_path else "",
        }
        data.update(self._save_paths_extra())
        self._paths_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def set_game_path(self, path: Path | str | None) -> None:
        """Convenience: set game_path and immediately persist it. Pass None to clear."""
        self._game_path = Path(path) if path else None
        self.save_paths()

    def set_heroic_app_name(self, app_name: str | None) -> None:
        """Persist a discovered Heroic app name into paths.json.

        Used by the Add Game dialog so GOG/Epic titles keep a record of
        which Heroic library entry they resolved to. Launch code still
        prefers live detection via installed.json, so this field is an
        informational fallback rather than the source of truth.
        """
        try:
            self.save_paths()
        except Exception:
            pass
        try:
            data: dict = {}
            if self._paths_file.is_file():
                data = json.loads(self._paths_file.read_text(encoding="utf-8")) or {}
            data["heroic_app_name"] = app_name or ""
            self._paths_file.parent.mkdir(parents=True, exist_ok=True)
            self._paths_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass

    def _validate_staging(self) -> None:
        """Check that a custom staging path still exists on disk.

        Called during load_paths().  If the user set a custom staging
        directory and that directory has since been deleted, the game
        config is stale — clear paths.json so the user must re-add the
        game through the Add Game dialog.
        """
        if self._staging_path is not None and not self._staging_path.is_dir():
            self._game_path = None
            self._prefix_path = None
            self._staging_path = None
            # Wipe the persisted config so the game shows as unconfigured.
            try:
                self._paths_file.unlink(missing_ok=True)
            except OSError:
                pass

    # -----------------------------------------------------------------------
    # Validation (concrete — subclasses may override)
    # -----------------------------------------------------------------------

    def is_configured(self) -> bool:
        """Returns True if game_path is set and the directory exists on disk."""
        p = self.get_game_path()
        return p is not None and p.exists()

    def validate_install(self) -> list[str]:
        """
        Check that the game is ready to receive mod installs.
        Returns a list of human-readable error strings; empty list = all good.
        """
        errors: list[str] = []
        if not self.is_configured():
            errors.append(
                f"Game path not set or does not exist for '{self.name}'."
            )
        data_path = self.get_mod_data_path()
        if data_path is not None and not data_path.exists():
            errors.append(f"Mod data directory does not exist: {data_path}")
        return errors

    def post_build_filemap(self, filemap_path: "Path", staging_path: "Path") -> None:
        """Called after build_filemap() writes filemap.txt.

        Override in game handlers that need to rewrite the filemap paths to
        reflect the actual deployed layout rather than the staging layout.
        For example, Witcher 3 transforms staging paths such as
        ``TrueFires_v1.01/modTrueFires/content/x.xml`` into the routed path
        ``mods/modTrueFires/content/x.xml`` so the treeview and filemap both
        match the real game-root structure.

        The default implementation is a no-op.
        """

    def post_clean_game_folder(self, log_fn=None) -> None:
        """Called after Clean Game Folder removes deployed files.
        Override in game handlers that need extra cleanup (e.g. resetting
        modsettings.lsx to vanilla for BG3).
        """

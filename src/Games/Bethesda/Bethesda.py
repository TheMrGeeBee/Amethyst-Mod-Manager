"""
Bethesda.py
Game handler for Various Bethesda games using the same deployment method.

Mod structure:
  Mods install into <game_path>/Data/
  Staged mods live in Profiles/Fallout 3/mods/
"""

import json
import re
import shutil
from pathlib import Path

from Games.base_game import BaseGame, WizardTool
from Utils.atomic_write import write_atomic_text
from Utils.deploy import LinkMode, deploy_core, deploy_custom_rules, deploy_filemap, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, expand_separator_link_modes, expand_separator_raw_deploy, cleanup_custom_deploy_dirs, restore_custom_rules, move_to_core, restore_data_core
from Utils.modlist import read_modlist
from Utils.config_paths import get_profiles_dir

_PROFILES_DIR = get_profiles_dir()

# Prefix deps auto-installed on first add for the modern 64-bit Creation Engine
# games (Skyrim SE/VR, Fallout 4/4VR, Starfield, EnderalSE): their *SE DLL
# plugins silently fail to load without the VC++ x64 runtime, and Community
# Shaders / ENB need the fxc2 d3dcompiler_47. Installed via the Proton-menu
# installers, not winetricks — see base_game.auto_install_deps. The older 32-bit
# Gamebryo titles (Oblivion/FO3/FNV/Skyrim LE) don't need these.
_MODERN_CREATION_ENGINE_DEPS = ["vcredist", "d3dcompiler_47"]


def _read_ini_key(ini_path: Path, section: str, key: str) -> "str | None":
    """Return the current value for [section] key, or None if not present."""
    try:
        text = ini_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        text = ini_path.read_text(encoding="utf-8", errors="replace")

    section_re = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=(?P<value>.*)$")

    in_section = False
    for line in text.splitlines():
        m = section_re.match(line)
        if m:
            in_section = m.group("name").strip() == section
            continue
        if in_section:
            km = key_re.match(line)
            if km:
                return km.group("value").rstrip("\r")
    return None


def _set_ini_key(ini_path: Path, section: str, key: str, value: "str | None") -> None:
    """Set or remove a single INI key without disturbing the rest of the file.

    Bethesda game INIs sometimes contain multi-line values (e.g. Fallout.ini's
    [GeneralWarnings] section) that configparser refuses to parse. This helper
    does a line-based edit so the rest of the file is preserved byte-for-byte.
    value=None removes the key; empty [section] blocks are pruned on removal.
    """
    try:
        text = ini_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    except UnicodeDecodeError:
        text = ini_path.read_text(encoding="utf-8", errors="replace")

    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline) if text else []

    section_header = f"[{section}]"
    section_re = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")

    section_start = -1
    section_end = len(lines)
    for i, line in enumerate(lines):
        m = section_re.match(line)
        if not m:
            continue
        if section_start == -1 and m.group("name").strip() == section:
            section_start = i
        elif section_start != -1:
            section_end = i
            break

    if section_start == -1:
        if value is None:
            return
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(section_header)
        lines.append(f"{key}={value}")
        lines.append("")
    else:
        key_line = -1
        for i in range(section_start + 1, section_end):
            if key_re.match(lines[i]):
                key_line = i
                break

        if value is None:
            if key_line != -1:
                del lines[key_line]
                section_end -= 1
            has_content = any(
                ln.strip() and not ln.strip().startswith((";", "#"))
                for ln in lines[section_start + 1:section_end]
            )
            if not has_content:
                trailing = section_end
                while trailing < len(lines) and lines[trailing] == "":
                    trailing += 1
                del lines[section_start:trailing]
        else:
            new_line = f"{key}={value}"
            if key_line != -1:
                lines[key_line] = new_line
            else:
                lines.insert(section_end, new_line)

    out = newline.join(lines)
    if text.endswith(newline) and not out.endswith(newline):
        out += newline
    # If the INI is a symlink (profile-specific INI files routed into My Games),
    # write *through* the link to its real target so the edit persists back to the
    # profile's "ini files" folder and the symlink itself survives. An atomic
    # write-temp→rename would clobber the link, turning it into a regular file.
    if ini_path.is_symlink():
        real = ini_path.resolve()
        write_atomic_text(real, out)
    else:
        write_atomic_text(ini_path, out)


class Fallout_3(BaseGame):

    plugins_use_star_prefix = False
    plugins_include_vanilla = True
    vanilla_plugins = ["Fallout3.esm"]
    vanilla_dlc_plugins = [
        "Anchorage.esm", "ThePitt.esm", "BrokenSteel.esm",
        "PointLookout.esm", "Zeta.esm",
    ]
    synthesis_registry_name = "Fallout3"

    # BAIN packages are authored for Bethesda games, so re-enable the
    # sub-package picker that BaseGame disables by default.
    @property
    def supports_bain(self) -> bool:
        return True

    def __init__(self):
        self._game_path: Path | None = None
        self._prefix_path: Path | None = None
        self._deploy_mode: LinkMode = LinkMode.HARDLINK
        self._staging_path: Path | None = None
        self._script_extender_swap: bool = True
        self._profile_ini_files: bool = False
        self._profile_saves: bool = False
        self.load_paths()

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Fallout 3"

    @property
    def game_id(self) -> str:
        return "Fallout3"

    @property
    def exe_name(self) -> str:
        return "Fallout3Launcher.exe"

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esl", ".esm"]

    @property
    def steam_id(self) -> str:
        return "22300"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout3"
    
    @property
    def mods_dir(self) -> str:
        return "Data"

    @property
    def mod_folder_strip_prefixes(self) -> set[str]:
        return {"Data","oblivion"}
    
    @property
    def mod_required_top_level_folders(self) -> set[str]:
        return {"skse",
                "sfse",
                "f4se",
                "nvse",
                "fose",
                "obse",
                "textures",
                "sound",
                "meshes",
                "mcm",
                "scripts",
                "interface",
                "lightplacer",
                "mapmarkers",
                "music",
                "nemesis_engine",
                "seq",
                "shadercache",
                "shaders",
                "shadersfx",
                "grass",
                "video",
                "source",
                "calientetools",
                "data",
                "materials",
                "tools",
                "config",
                "menus",
                "distantlod",
                "fonts",
                "facegen",
                "lodsettings",
                "lsdata",
                "strings",
                "trees",
                "asi",
                "geometries",
                "bashtags",
                "dialogueviews",
                "terrain",
                "vis",
                "programs",
                "misc",
                "particles",
                "planetdata",
                "dyndolod",
                "netscriptframework",
                "skyproc patchers",
                }

    @property
    def mod_auto_strip_until_required(self) -> bool:
        return True

    @property
    def mod_required_file_types(self) -> set[str]:
        return {".esp", ".esl", ".esm", ".ini", ".bsa", ".ba2"}

    @property
    def mod_install_as_is_if_no_match(self) -> bool:
        return True

    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.xml","*read*.txt","*.jpg"}
    
    @property
    def excluded_loose_filenames(self) -> set[str]:
        return {"*.txt"}

    @property
    def archive_extensions(self) -> frozenset[str]:
        # Older Bethesda games use BSA archives. Fallout 4 / Fallout 4 VR /
        # Starfield / Fallout 76 use BA2 and override this further.
        return frozenset({".bsa"})

    @property
    def loot_sort_enabled(self) -> bool:
        return True

    @property
    def loot_game_type(self) -> str:
        return "Fallout3"

    @property
    def loot_masterlist_repo(self) -> str:
        return "fallout3"

    @property
    def reshade_dll(self) -> str:
        return "d3d9.dll"

    @property
    def reshade_arch(self) -> int:
        return 32
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["fose_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["fose*.dll"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
                ]

    def _saves_routing_rule(self, extensions: list[str]):
        """Route loose save files into the prefix's My Games Saves folder, mirrored to the GOG variant if that folder exists."""
        from Utils.deploy import CustomRule
        gog_sub = self._MYGAMES_SUBPATH_GOG or Path(f"{self._MYGAMES_SUBPATH} GOG")
        mirrors: list[str] = []
        if self._prefix_path is not None and (self._prefix_path / self._MYGAMES_DOCS / gog_sub).is_dir():
            mirrors.append(str(self._MYGAMES_DOCS / gog_sub / "Saves"))
        return CustomRule(
            dest=str(self._MYGAMES_DOCS / self._MYGAMES_SUBPATH / "Saves"),
            extensions=extensions, flatten=True, to_prefix=True,
            mirror_dests=mirrors,
        )

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="downgrade_fo3",
                label="Downgrade Fallout 3",
                description=(
                    "Downgrade to pre-Anniversary Edition so that "
                    "the script extender (FOSE) works correctly."
                ),
                dialog_class_path="wizards.fallout_downgrade.FalloutDowngradeWizard",
            ),
            WizardTool(
                id="install_se_fo3",
                label="Install Script Extender (FOSE)",
                description="Download and install FOSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://fose.silverlock.org/download/fose_v1_2_beta2.7z",
                    "archive_keywords": ["fose"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_fo3",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="FO3Edit", id_suffix="fo3",
                nexus_url="https://www.nexusmods.com/fallout3/mods/637?tab=files",
            ),
        ]

    @staticmethod
    def _xedit_wizard_tools(
        build: str, id_suffix: str, nexus_url: str, qac: bool = True
    ) -> list[WizardTool]:
        """Build the 'Run <xEdit>' (+ optional QAC) wizard entries for a game.

        All Bethesda games share one parametrised xEdit wizard
        (``wizards.sseedit``); only the exe name + Nexus page differ, supplied
        via ``extra``.  Plugins xEdit creates/cleans are rescued generically by
        the game's ``restore()`` (``restore_data_core`` with overwrite/staging),
        so no per-game restore code is needed.
        """
        exe = f"{build}.exe"
        tools = [
            WizardTool(
                id=f"run_{build.lower()}_{id_suffix}",
                label=f"Run {build}",
                description=f"Install {build}, deploy mods, and run {exe}.",
                dialog_class_path="wizards.sseedit.SSEEditWizard",
                extra={"xedit_exe": exe, "nexus_url": nexus_url, "display_name": build},
            ),
        ]
        if qac:
            tools.append(
                WizardTool(
                    id=f"run_{build.lower()}_qac_{id_suffix}",
                    label=f"Run {build} QAC",
                    description=f"Deploy mods and run {build}QuickAutoClean.exe.",
                    dialog_class_path="wizards.sseedit.SSEEditQACWizard",
                    extra={"xedit_exe": exe, "nexus_url": nexus_url, "display_name": build},
                )
            )
        return tools

    # -----------------------------------------------------------------------
    # Paths
    # -----------------------------------------------------------------------

    def get_game_path(self) -> Path | None:
        return self._game_path

    def get_mod_data_path(self) -> Path | None:
        """Mods go into the Data/ subfolder of the game root directory."""
        if self._game_path is None:
            return None
        return self._game_path / "Data"

    def get_mod_staging_path(self) -> Path:
        if self._staging_path is not None:
            return self._staging_path / "mods"
        return _PROFILES_DIR / self.name / "mods"

    def _load_paths_extra(self, data: dict) -> None:
        self._script_extender_swap = data.get("script_extender_swap", True)
        self._profile_ini_files = data.get("profile_ini_files", False)
        self._profile_saves = data.get("profile_saves", False)

    def _save_paths_extra(self) -> dict:
        return {
            "script_extender_swap": self._script_extender_swap,
            "profile_ini_files":    self._profile_ini_files,
            "profile_saves":        self._profile_saves,
        }

    def set_staging_path(self, path: "Path | str | None") -> None:
        self._staging_path = Path(path) if path else None
        self.save_paths()

    def get_prefix_path(self) -> Path | None:
        return self._prefix_path

    def get_deploy_mode(self) -> LinkMode:
        return self._deploy_mode

    def set_deploy_mode(self, mode: LinkMode) -> None:
        self._deploy_mode = mode
        self.save_paths()

    @property
    def script_extender_swap(self) -> bool:
        return self._script_extender_swap

    def set_script_extender_swap(self, value: bool) -> None:
        self._script_extender_swap = value
        self.save_paths()

    @property
    def profile_ini_files(self) -> bool:
        return self._profile_ini_files

    def set_profile_ini_files(self, value: bool) -> None:
        self._profile_ini_files = value
        self.save_paths()
        if value:
            # Create the (empty) "ini files" folder in every profile so the
            # user has an obvious place to drop their per-profile INIs.
            self._ensure_profile_ini_dirs()

    # Name of the subfolder inside each profile that holds the user's INIs.
    _PROFILE_INI_SUBDIR = "ini files"

    def _profile_ini_dir(self, profile: str) -> Path:
        """Folder inside a profile that the user drops per-profile INIs into."""
        return self.get_profile_root() / "profiles" / profile / self._PROFILE_INI_SUBDIR

    def _ensure_profile_ini_dirs(self) -> None:
        """Create the empty 'ini files' folder for every existing profile."""
        profiles_root = self.get_profile_root() / "profiles"
        if not profiles_root.is_dir():
            return
        for profile_dir in profiles_root.iterdir():
            if profile_dir.is_dir():
                (profile_dir / self._PROFILE_INI_SUBDIR).mkdir(parents=True, exist_ok=True)

    @property
    def profile_saves(self) -> bool:
        return self._profile_saves and self.supports_profile_saves

    def set_profile_saves(self, value: bool) -> None:
        self._profile_saves = value and self.supports_profile_saves
        self.save_paths()
        if self._profile_saves:
            # Create the empty Saves folder up-front so the user knows where to
            # drop their saves, without waiting for a deploy. Seed every
            # existing profile (and the active one) since any of them may be
            # deployed next.
            self._ensure_profile_saves_dirs()

    def _ensure_profile_saves_dirs(self) -> None:
        """Create an empty ``Saves`` folder in each existing profile folder."""
        try:
            profiles_root = self.get_profile_root() / "profiles"
        except Exception:
            return
        names: set[str] = set()
        if self._active_profile_dir is not None:
            names.add(self._active_profile_dir.name)
        if profiles_root.is_dir():
            names.update(p.name for p in profiles_root.iterdir() if p.is_dir())
        for name in names:
            try:
                (profiles_root / name / self._SAVES_FOLDER_NAME).mkdir(
                    parents=True, exist_ok=True)
            except OSError:
                pass

    def set_prefix_path(self, path: Path | str | None) -> None:
        self._prefix_path = Path(path) if path else None
        self.save_paths()

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout3")
    # GOG subpaths are None on subclasses without a GOG release.
    _APPDATA_SUBPATH_GOG: "Path | None" = Path("drive_c/users/steamuser/AppData/Local/Fallout3 GOG")
    _MYGAMES_SUBPATH = Path("Fallout3")
    _MYGAMES_SUBPATH_GOG: "Path | None" = Path("Fallout3 GOG")
    _ARCHIVE_INI_FILENAME = "FALLOUT.INI"
    # Per-game Prefs INI. When set, archive invalidation writes the same keys
    # to both the primary INI and the Prefs INI so the Prefs file can't silently
    # override what we wrote — the engine reads both and the Prefs value wins
    # when present in both. Set to None on subclasses without a Prefs INI.
    _ARCHIVE_PREFS_INI_FILENAME: "str | None" = "FalloutPrefs.ini"

    # Whether the SArchiveList / SInvalidationFile edits go to the Prefs INI too.
    # FO3/FNV: yes — FalloutPrefs.ini legitimately carries Archive keys that
    # override Fallout.ini. Oblivion: NO — OblivionPrefs.ini does not manage
    # SArchiveList, and writing a partial list there (dummy + mod BSAs but no
    # vanilla archives, since the vanilla list only lives in Oblivion.ini)
    # shadows the good list and breaks BSA loading for ALL mods. bInvalidate-
    # OlderFiles still goes to both regardless.
    _archive_list_in_prefs_ini: bool = True
    archive_invalidation_enabled = True
    _archive_invalidation_extra_keys: tuple[tuple[str, str], ...] = ()

    # MO2-style dummy-BSA invalidation. When _invalidation_bsa_name is set, the
    # apply step writes an empty BSA into the game's Data folder, prepends it to
    # SArchiveList, and empties SInvalidationFile (disabling the legacy .txt
    # codepath). When None, only the bInvalidateOlderFiles INI key is touched.
    # BA2-based games (Fallout 4, Starfield) must override with None.
    _invalidation_bsa_name: "str | None" = "Fallout - Invalidation.bsa"
    _invalidation_bsa_version: "int | None" = 0x68
    _invalidation_archive_list_key: str = "SArchiveList"

    # FO3/FNV only: these engines read files only from BSAs listed in
    # SArchiveList — a mod BSA named to match its plugin is NOT reliably
    # auto-loaded. When True, the invalidation step appends every deployed
    # mod-provided BSA to SArchiveList so its assets load. Fallout_3/
    # Fallout3_GOTY/Fallout_NV set it True; later engines override it False.
    # Oblivion does NOT use this — it auto-loads a mod's BSA via plugin-name
    # association, and forcing entries here both fights SkyBSA's load-order
    # reversal and blows the 256-char SArchiveList limit. See
    # geckwiki.com/index.php/BSA_Files.
    _archive_list_needs_mod_bsas: bool = True

    # Engine-fix plugin whose FalloutCustom.ini support bypasses the vanilla
    # 255-char SArchiveList read limit (settings applied in-memory, 16 KB
    # buffer). FO3: Command Extender; FNV overrides with JIP LN NVSE (which
    # additionally patches the vanilla Fallout.ini read).
    _archive_list_fix_name: "str | None" = "Command Extender"
    _archive_list_fix_path: "str | None" = "Data/FOSE/Plugins/CommandExtender.dll"
    _CUSTOM_INI_FILENAME = "FalloutCustom.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "fose_loader.exe"

    @property
    def frameworks(self) -> dict[str, str]:
        fw = {"Script Extender": self._script_extender_exe}
        # The SArchiveList fix plugin is only relevant on games where we
        # append mod BSAs (FO3/GOTY/FNV) — later engines inherit the attrs
        # but never hit that codepath.
        if (self._archive_list_needs_mod_bsas
                and self._archive_list_fix_name and self._archive_list_fix_path):
            fw[self._archive_list_fix_name] = self._archive_list_fix_path
        return fw

    _PLUGINS_TXT_FILENAME = "plugins.txt"

    def _plugins_txt_targets(self, prefix_root: "Path | None" = None) -> list[Path]:
        """Return every in-prefix path where the game might expect plugins.txt.

        Steam and GOG builds use separate AppData folders. If both exist, we
        write to both so either build picks up the load order.

        prefix_root overrides the game's own pfx/ dir — used for per-tool
        Proton prefixes (PGPatcher etc.) that need the same layout.
        """
        root = prefix_root if prefix_root is not None else self._prefix_path
        if root is None:
            return []
        fname = self._PLUGINS_TXT_FILENAME
        steam_dir = root / self._APPDATA_SUBPATH
        targets: list[Path] = []
        if steam_dir.is_dir():
            targets.append(steam_dir / fname)
        if self._APPDATA_SUBPATH_GOG is not None:
            gog_dir = root / self._APPDATA_SUBPATH_GOG
            if gog_dir.is_dir():
                targets.append(gog_dir / fname)
        if not targets:
            targets.append(steam_dir / fname)
        return targets

    def _plugins_txt_target(self) -> Path | None:
        """Return the primary in-prefix plugins.txt path (back-compat shim)."""
        targets = self._plugins_txt_targets()
        return targets[0] if targets else None

    def _symlink_plugins_txt(self, profile: str, log_fn, prefix_root: "Path | None" = None) -> None:
        """Symlink the active profile's plugins.txt into the Proton prefix."""
        _log = log_fn
        targets = self._plugins_txt_targets(prefix_root)
        if not targets:
            _log("  WARN: Prefix path not set — skipping plugins.txt symlink.")
            return

        source = self.get_profile_root() / "profiles" / profile / "plugins.txt"
        if not source.is_file():
            _log(f"  WARN: plugins.txt not found at {source} — skipping symlink.")
            return

        for target in targets:
            if target.exists() or target.is_symlink():
                target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(source)
            _log(f"  Linked plugins.txt → {target}")

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        """Remove the plugins.txt symlink from the Proton prefix on restore."""
        _log = log_fn
        for target in self._plugins_txt_targets():
            if target.is_symlink():
                target.unlink()
                _log(f"  Removed plugins.txt symlink: {target}")

    # -----------------------------------------------------------------------
    # Timestamp load order (Oblivion/FO3/FNV)
    # -----------------------------------------------------------------------

    # The legacy engine orders plugins by Data/ file mtime — plugins.txt only
    # selects the active set. Skyrim-era subclasses (plugins.txt-ordered)
    # override this back to False.
    _plugin_load_order_by_mtime: bool = True

    # Every Bethesda engine loads master-flagged plugins before non-masters.
    plugins_master_block = True

    def _orders_plugins_by_mtime(self) -> bool:
        return self._plugin_load_order_by_mtime and not self.plugins_use_star_prefix

    def stamp_plugin_load_order(self, profile: str, log_fn=None) -> None:
        """Set ascending mtimes on deployed plugins to match the profile's load order."""
        _log = log_fn or (lambda _: None)
        if self._game_path is None or not self._orders_plugins_by_mtime():
            return
        from Utils.plugins import read_loadorder, read_plugins
        profile_dir = self.get_profile_root() / "profiles" / profile
        ordered = read_loadorder(profile_dir / "loadorder.txt")
        if not ordered:
            ordered = [
                e.name for e in read_plugins(
                    profile_dir / "plugins.txt",
                    star_prefix=self.plugins_use_star_prefix,
                )
            ]
        if not ordered:
            return
        from Utils.plugin_mtimes import stamp_plugin_load_order
        stamped = stamp_plugin_load_order(
            ordered,
            self._game_path / "Data",
            staging_root=self.get_effective_mod_staging_path(),
            overwrite_dir=self.get_effective_overwrite_path(),
            log_fn=_log,
        )
        if stamped:
            _log(f"  Set mtimes on {stamped} plugin(s) to enforce load order.")

    # -----------------------------------------------------------------------
    # Archive invalidation
    # -----------------------------------------------------------------------

    _MYGAMES_DOCS = Path("drive_c/users/steamuser/Documents/My Games")

    def _get_archive_ini_path(self) -> "Path | None":
        """Return the primary INI used for archive invalidation (back-compat)."""
        mygames = self._mygames_path()
        if mygames is None:
            return None
        return mygames / self._ARCHIVE_INI_FILENAME

    def _get_archive_ini_paths(self) -> list[Path]:
        """Return every INI that needs the invalidation keys written.

        Includes the primary Fallout.ini-style INI and, when set, the Prefs INI
        in the same directory. Empty when the prefix is unconfigured.
        """
        mygames = self._mygames_path()
        if mygames is None:
            return []
        paths = [mygames / self._ARCHIVE_INI_FILENAME]
        if self._ARCHIVE_PREFS_INI_FILENAME:
            paths.append(mygames / self._ARCHIVE_PREFS_INI_FILENAME)
        return paths

    def _mygames_paths(self) -> list[Path]:
        """Return every My Games folder for this game inside the prefix.

        Steam and GOG builds use separate folders. If both exist, we manage
        both so either build sees the active profile's INIs.
        """
        if self._prefix_path is None:
            return []
        steam_dir = self._prefix_path / self._MYGAMES_DOCS / self._MYGAMES_SUBPATH
        paths: list[Path] = []
        if steam_dir.is_dir():
            paths.append(steam_dir)
        if self._MYGAMES_SUBPATH_GOG is not None:
            gog_dir = self._prefix_path / self._MYGAMES_DOCS / self._MYGAMES_SUBPATH_GOG
            if gog_dir.is_dir():
                paths.append(gog_dir)
        if not paths:
            paths.append(steam_dir)
        return paths

    def _mygames_path(self) -> "Path | None":
        """Return the primary My Games folder (back-compat shim)."""
        paths = self._mygames_paths()
        return paths[0] if paths else None

    def _symlink_profile_ini_files(self, profile: str, log_fn) -> None:
        """Symlink *.ini files from the profile folder into the My Games directory.

        Any existing file at the target is backed up as <name>.bak before being
        replaced.  Existing symlinks pointing to our profile dir are silently
        replaced without a backup (they are already managed by us).
        """
        _log = log_fn
        if not self._profile_ini_files:
            return
        mygames_dirs = self._mygames_paths()
        if not mygames_dirs:
            _log("  WARN: Prefix path not set — skipping profile INI symlinks.")
            return
        ini_dir = self._profile_ini_dir(profile)
        ini_dir.mkdir(parents=True, exist_ok=True)
        ini_files = list(ini_dir.glob("*.ini"))
        if not ini_files:
            _log(f"  No *.ini files found in '{ini_dir.name}' folder — skipping.")
            return
        for mygames in mygames_dirs:
            mygames.mkdir(parents=True, exist_ok=True)
            for src in ini_files:
                target = mygames / src.name
                if target.is_symlink():
                    target.unlink()
                elif target.exists():
                    backup = target.with_suffix(".bak")
                    target.rename(backup)
                    _log(f"  Backed up {target.name} → {backup.name}")
                target.symlink_to(src)
                _log(f"  Linked {src.name} → {target}")

    def _remove_profile_ini_symlinks(self, profile: str, log_fn) -> None:
        """Remove profile INI symlinks from My Games and restore any backups."""
        _log = log_fn
        if not self._profile_ini_files:
            return
        mygames_dirs = [p for p in self._mygames_paths() if p.is_dir()]
        if not mygames_dirs:
            return
        ini_dir = self._profile_ini_dir(profile)
        if not ini_dir.is_dir():
            return
        try:
            ini_dir_resolved = ini_dir.resolve()
        except OSError:
            ini_dir_resolved = ini_dir
        for mygames in mygames_dirs:
            # Scan the actual My Games folder so orphaned symlinks (whose source
            # .ini was deleted from the profile) are still removed.
            for target in mygames.glob("*.ini"):
                if not target.is_symlink():
                    continue
                # Compare the symlink's *target* directory against our ini dir,
                # resolving both sides so a symlinked prefix/staging path on the
                # way to ini_dir doesn't break the match.
                try:
                    link_target = target.readlink()
                    if not link_target.is_absolute():
                        link_target = target.parent / link_target
                    link_parent = link_target.resolve().parent
                except OSError:
                    continue
                if link_parent != ini_dir_resolved:
                    continue
                target.unlink()
                _log(f"  Removed profile INI symlink: {target.name}")
                backup = target.with_suffix(".bak")
                if backup.exists():
                    backup.rename(target)
                    _log(f"  Restored {target.name} from .bak")

    # -----------------------------------------------------------------------
    # Profile-specific saves
    # -----------------------------------------------------------------------
    #
    # Whether this game exposes the profile-specific saves option at all. Off
    # for games with server-side/cloud saves (e.g. Fallout 76) where there is
    # no local Saves folder worth redirecting.
    supports_profile_saves = True
    # Folder name the engine reads saves from, inside each save-link target.
    # Override on a subclass whose engine uses a different name.
    _SAVES_FOLDER_NAME = "Saves"
    # Suffix used to hide a pre-existing real Saves folder so the game can't
    # see it while our profile symlink is active.
    _SAVES_BACKUP_SUFFIX = "_backup_amm"

    def _saves_link_targets(self) -> list[Path]:
        """Return every directory that should receive a ``Saves`` symlink.

        Defaults to the game's My Games folder(s). Games whose saves live
        somewhere else can override this to point at their own location while
        reusing the deploy/restore logic below.
        """
        return self._mygames_paths()

    def _profile_saves_dir(self, profile: str) -> Path:
        """Path to the profile-specific saves folder (created on demand)."""
        return self.get_profile_root() / "profiles" / profile / self._SAVES_FOLDER_NAME

    def _symlink_profile_saves(self, profile: str, log_fn) -> None:
        """Symlink each target's ``Saves`` folder to the profile saves folder.

        Creates the profile saves folder if it does not yet exist. Any real
        (non-symlink) ``Saves`` folder already present at a target is renamed
        to ``Saves<backup-suffix>`` so the game stops seeing it; restore puts
        it back. Symlinks we already manage are replaced without a backup.
        """
        _log = log_fn
        if not self.profile_saves:
            return
        targets = self._saves_link_targets()
        if not targets:
            _log("  WARN: No save-link target — skipping profile saves.")
            return
        profile_saves = self._profile_saves_dir(profile)
        profile_saves.mkdir(parents=True, exist_ok=True)
        for target_dir in targets:
            target_dir.mkdir(parents=True, exist_ok=True)
            link = target_dir / self._SAVES_FOLDER_NAME
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                backup = target_dir / (self._SAVES_FOLDER_NAME + self._SAVES_BACKUP_SUFFIX)
                if backup.exists():
                    _log(f"  WARN: {backup.name} already exists — leaving "
                         f"{link.name} in place, skipping.")
                    continue
                link.rename(backup)
                _log(f"  Backed up existing {link.name} → {backup.name}")
            link.symlink_to(profile_saves)
            _log(f"  Linked {link} → {profile_saves}")

    def _remove_profile_saves_symlinks(self, profile: str, log_fn) -> None:
        """Remove profile saves symlinks and restore any backed-up Saves folder."""
        _log = log_fn
        if not self.profile_saves:
            return
        profile_saves = self._profile_saves_dir(profile)
        for target_dir in self._saves_link_targets():
            if not target_dir.is_dir():
                continue
            link = target_dir / self._SAVES_FOLDER_NAME
            if link.is_symlink() and Path(link.resolve()) == profile_saves.resolve():
                link.unlink()
                _log(f"  Removed profile saves symlink: {link}")
            elif link.exists():
                # Not our symlink — leave it alone and skip restoring the backup
                # so we don't clobber whatever is there now.
                continue
            backup = target_dir / (self._SAVES_FOLDER_NAME + self._SAVES_BACKUP_SUFFIX)
            if backup.exists() and not link.exists():
                backup.rename(link)
                _log(f"  Restored {link.name} from {backup.name}")

    def apply_archive_invalidation(self, log_fn) -> None:
        """Set bInvalidateOlderFiles=1 in every managed game INI so loose files win.

        When ``_invalidation_bsa_name`` is set (MO2-style), also write a dummy
        BSA into the game's Data folder, prepend it to ``SArchiveList``, and
        empty ``SInvalidationFile`` to disable the legacy .txt codepath.

        Writes to both Fallout.ini and FalloutPrefs.ini (or the per-game
        equivalents) because the engine reads both at launch and the Prefs
        value wins when a key appears in both — leaving Prefs unmanaged would
        silently override what we wrote to the primary INI.
        """
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        # AI toggled off in the GUI: ensure on-disk state matches by running
        # the revert path. Idempotent — if nothing was previously applied the
        # helpers no-op. Without this, turning AI off and re-deploying would
        # leave the dummy BSA and INI keys in place.
        if not self.archive_invalidation:
            self.revert_archive_invalidation(_log)
            return
        ini_paths = self._get_archive_ini_paths()
        if not ini_paths:
            _log("  WARN: Prefix path not set — skipping archive invalidation.")
            return

        # FO3/FNV: resolve the mod-BSA delta once so every INI gets the same
        # update and the tracking sidecar is written exactly once afterwards.
        prev_mod_bsas: list[str] = []
        new_mod_bsas: list[str] = []
        if self._archive_list_needs_mod_bsas:
            prev_mod_bsas = self._tracked_mod_bsas()
            new_mod_bsas = self._deployed_mod_bsas()

        self._write_dummy_bsa_file(_log)
        primary_ini = ini_paths[0]
        longest_list = ""
        for ini_path in ini_paths:
            ini_path.parent.mkdir(parents=True, exist_ok=True)
            _set_ini_key(ini_path, "Archive", "bInvalidateOlderFiles", "1")
            for key, value in self._archive_invalidation_extra_keys:
                if _read_ini_key(ini_path, "Archive", key) is not None:
                    continue
                _set_ini_key(ini_path, "Archive", key, value)
            # SArchiveList / SInvalidationFile only go to the Prefs INI when the
            # engine treats it as an Archive-key override (FO3/FNV). On Oblivion
            # they must stay in Oblivion.ini only — see _archive_list_in_prefs_ini.
            # Also strip any partial SArchiveList a prior version wrote to Prefs,
            # since it shadows the good list and breaks BSA loading.
            if ini_path != primary_ini and not self._archive_list_in_prefs_ini:
                self._strip_archive_list_keys(ini_path)
                continue
            written = self._apply_dummy_bsa_invalidation_ini(
                ini_path, prev_mod_bsas, new_mod_bsas)
            if len(written) > len(longest_list):
                longest_list = written

        if self._archive_list_needs_mod_bsas:
            self._save_tracked_mod_bsas(new_mod_bsas)
            if new_mod_bsas:
                _log(f"  Registered {len(new_mod_bsas)} mod BSA(s) in "
                     f"{self._invalidation_archive_list_key}.")
            self._sync_archive_list_custom_ini(ini_paths, longest_list, _log)

        names = ", ".join(p.name for p in ini_paths)
        _log(f"  Archive invalidation enabled in {names}.")

    def _sync_archive_list_custom_ini(
        self, ini_paths: "list[Path]", list_str: str, _log,
    ) -> None:
        """Route an over-limit SArchiveList through FalloutCustom.ini, or warn.

        Vanilla FO3/FNV read the key into a 255-char buffer; anything past
        that is silently truncated mid-name. JIP LN NVSE (FNV) / Command
        Extender (FO3) apply FalloutCustom.ini settings directly in memory
        with a 16 KB buffer, bypassing the limit — so when the list is over
        and the plugin is installed, mirror it there. Otherwise remove our
        key so a stale FalloutCustom.ini value can't shadow the managed INIs
        (those plugins apply it *after* the vanilla INIs load).
        """
        key = self._invalidation_archive_list_key
        ini_dirs = {p.parent for p in ini_paths}
        over = len(list_str) > 255
        if over and self._archive_list_fix_installed():
            for d in ini_dirs:
                _set_ini_key(d / self._CUSTOM_INI_FILENAME, "Archive",
                             key, list_str)
            _log(f"  {key} is {len(list_str)} chars (engine limit 255) — "
                 f"wrote full list to {self._CUSTOM_INI_FILENAME} "
                 f"({self._archive_list_fix_name} installed).")
            return
        for d in ini_dirs:
            custom_ini = d / self._CUSTOM_INI_FILENAME
            if custom_ini.is_file():
                _set_ini_key(custom_ini, "Archive", key, None)
        if over:
            fix = (f" Install {self._archive_list_fix_name} to fix this."
                   if self._archive_list_fix_name else "")
            _log(f"  WARN: {key} is {len(list_str)} characters — the engine "
                 "reads only the first 255 and some mod BSAs will not load."
                 f"{fix}")
            self.add_deploy_warning(
                f"{key} exceeds the engine's 255-character limit — some mod "
                f"BSAs will not load.{fix}")

    def revert_archive_invalidation(self, log_fn) -> None:
        """Remove the invalidation keys from every managed game INI.

        Also undoes the MO2-style dummy-BSA setup when ``_invalidation_bsa_name``
        is set: removes the BSA from ``SArchiveList`` in each INI, restores
        ``SInvalidationFile`` to its default, and deletes the dummy file.

        Not gated on the current ``archive_invalidation`` setting — revert cleans
        whatever artifacts are present so toggling the setting and re-deploying
        leaves a consistent on-disk state.
        """
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        ini_paths = [p for p in self._get_archive_ini_paths() if p.is_file()]
        if not ini_paths:
            return

        for ini_path in ini_paths:
            self._revert_dummy_bsa_invalidation_ini(ini_path)
            _set_ini_key(ini_path, "Archive", "bInvalidateOlderFiles", None)
            for key, value in self._archive_invalidation_extra_keys:
                current = _read_ini_key(ini_path, "Archive", key)
                if current is None or current != value:
                    continue
                _set_ini_key(ini_path, "Archive", key, None)

        self._delete_dummy_bsa_file(_log)
        if self._archive_list_needs_mod_bsas:
            self._save_tracked_mod_bsas([])
            for d in {p.parent for p in ini_paths}:
                custom_ini = d / self._CUSTOM_INI_FILENAME
                if custom_ini.is_file():
                    _set_ini_key(custom_ini, "Archive",
                                 self._invalidation_archive_list_key, None)
        names = ", ".join(p.name for p in ini_paths)
        _log(f"  Archive invalidation reverted in {names}.")

    def _write_dummy_bsa_file(self, _log) -> None:
        """Write the dummy BSA into the game's Data folder, if configured."""
        bsa_name = self._invalidation_bsa_name
        bsa_version = self._invalidation_bsa_version
        if bsa_name is None or bsa_version is None:
            return
        if self._game_path is None:
            _log("  WARN: Game path not set — skipping dummy BSA write.")
            return
        from Utils.bsa_invalidation import write_dummy_bsa
        try:
            write_dummy_bsa(self._game_path / "Data" / bsa_name, bsa_version)
        except OSError as exc:
            _log(f"  WARN: Could not write {bsa_name}: {exc}")

    def _delete_dummy_bsa_file(self, _log) -> None:
        """Remove the dummy BSA from the game's Data folder, if present."""
        bsa_name = self._invalidation_bsa_name
        if bsa_name is None or self._game_path is None:
            return
        bsa_path = self._game_path / "Data" / bsa_name
        if not bsa_path.is_file():
            return
        try:
            bsa_path.unlink()
            _log(f"  Removed dummy {bsa_name}.")
        except OSError as exc:
            _log(f"  WARN: Could not remove {bsa_name}: {exc}")

    def _apply_dummy_bsa_invalidation_ini(
        self, ini_path: Path,
        prev_mod_bsas: "list[str] | None" = None,
        new_mod_bsas: "list[str] | None" = None,
    ) -> str:
        """MO2-style INI edits for one INI: SArchiveList[0] + SInvalidationFile=''.
        Returns the archive list as written (for length checks)."""
        bsa_name = self._invalidation_bsa_name
        if bsa_name is None:
            return ""
        from Utils.bsa_invalidation import (
            ensure_in_archive_list, append_to_archive_list,
            remove_many_from_archive_list,
        )
        key = self._invalidation_archive_list_key
        current = _read_ini_key(ini_path, "Archive", key) or ""
        updated = ensure_in_archive_list(current, bsa_name)
        if self._archive_list_needs_mod_bsas:
            # FO3/FNV: only BSAs listed here have their assets read. Drop the
            # mod BSAs we previously appended, then re-append what's currently
            # deployed — so removed mods don't leave stale entries. Lists are
            # precomputed by the caller and the sidecar is written once there.
            prev = self._tracked_mod_bsas() if prev_mod_bsas is None else prev_mod_bsas
            mod_bsas = self._deployed_mod_bsas() if new_mod_bsas is None else new_mod_bsas
            updated = remove_many_from_archive_list(updated, prev)
            updated = append_to_archive_list(updated, mod_bsas)
        if updated != current:
            _set_ini_key(ini_path, "Archive", key, updated)
        _set_ini_key(ini_path, "Archive", "SInvalidationFile", "")
        return updated

    def _strip_archive_list_keys(self, ini_path: Path) -> None:
        """Remove SArchiveList / SInvalidationFile from an INI we no longer want
        to manage (the Oblivion Prefs INI). Leaves other Archive keys alone."""
        if _read_ini_key(ini_path, "Archive",
                         self._invalidation_archive_list_key) is not None:
            _set_ini_key(ini_path, "Archive",
                         self._invalidation_archive_list_key, None)
        if _read_ini_key(ini_path, "Archive", "SInvalidationFile") == "":
            _set_ini_key(ini_path, "Archive", "SInvalidationFile", None)

    def _revert_dummy_bsa_invalidation_ini(self, ini_path: Path) -> None:
        """Undo dummy-BSA INI edits for one INI. The dummy file itself is removed
        once per game dir by :meth:`_delete_dummy_bsa_file`."""
        bsa_name = self._invalidation_bsa_name
        if bsa_name is None:
            return
        from Utils.bsa_invalidation import (
            remove_from_archive_list, remove_many_from_archive_list,
        )
        key = self._invalidation_archive_list_key
        current = _read_ini_key(ini_path, "Archive", key)
        if current is not None:
            updated = remove_from_archive_list(current, bsa_name)
            if self._archive_list_needs_mod_bsas:
                updated = remove_many_from_archive_list(
                    updated, self._tracked_mod_bsas())
            if updated != current:
                _set_ini_key(ini_path, "Archive", key, updated or None)
        # Restore the engine default so a future deactivation doesn't leave
        # SInvalidationFile permanently empty.
        if _read_ini_key(ini_path, "Archive", "SInvalidationFile") == "":
            _set_ini_key(ini_path, "Archive", "SInvalidationFile",
                         "ArchiveInvalidation.txt")

    # --- FO3/FNV mod-BSA registration -------------------------------------
    # These engines read assets only from BSAs listed in SArchiveList, so every
    # deployed mod BSA must be appended. We track what we added in a sidecar so
    # revert/refresh can drop entries for mods that were since removed.

    def _archive_list_fix_installed(self) -> bool:
        """True if the engine-fix plugin from `_archive_list_fix_path` is on
        disk (case-insensitive walk from the game root)."""
        if self._archive_list_fix_path is None or self._game_path is None:
            return False
        current = self._game_path
        for part in Path(self._archive_list_fix_path).parts:
            try:
                entries = {e.name.lower(): e for e in current.iterdir()}
            except OSError:
                return False
            match = entries.get(part.lower())
            if match is None:
                return False
            current = match
        return current.is_file()

    def _mod_bsa_tracking_path(self) -> "Path | None":
        try:
            return self.get_effective_filemap_path().parent / "managed_archives.txt"
        except Exception:
            return None

    def _tracked_mod_bsas(self) -> list[str]:
        """Mod BSA names we previously appended to SArchiveList, if any."""
        path = self._mod_bsa_tracking_path()
        if path is None or not path.is_file():
            return []
        try:
            return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
        except OSError:
            return []

    def _save_tracked_mod_bsas(self, names: list[str]) -> None:
        path = self._mod_bsa_tracking_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(names) + ("\n" if names else ""),
                            encoding="utf-8")
        except OSError:
            pass

    def _deployed_mod_bsas(self) -> list[str]:
        """Top-level .bsa/.ba2 files deployed by mods, from the active filemap.

        Vanilla archives are already in the engine's default SArchiveList; we
        only append archives that a mod actually deploys into Data/.
        """
        try:
            filemap = self.get_effective_filemap_path()
        except Exception:
            return []
        if not filemap.is_file():
            return []
        names: list[str] = []
        seen: set[str] = set()
        try:
            for line in filemap.read_text(encoding="utf-8").splitlines():
                rel = line.split("\t", 1)[0].strip()
                if not rel or "/" in rel or "\\" in rel:
                    continue  # only top-level Data/ entries are loadable archives
                low = rel.lower()
                if not (low.endswith(".bsa") or low.endswith(".ba2")):
                    continue
                if low in seen:
                    continue
                seen.add(low)
                names.append(rel)
        except OSError:
            return []
        return self._order_mod_bsas_by_plugins(names)

    def _plugin_load_order(self) -> list[str]:
        """Lowercased plugin filenames in load order, from the active profile's
        plugins.txt (top = loads first, bottom = loads last / wins). Strips the
        star/asterisk activation prefix used by later games."""
        if self._active_profile_dir is None:
            return []
        path = self._active_profile_dir / self._PLUGINS_TXT_FILENAME
        if not path.is_file():
            return []
        order: list[str] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                name = line.strip()
                if not name or name.startswith("#"):
                    continue
                if name.startswith("*"):
                    name = name[1:].strip()
                if name:
                    order.append(name.lower())
        except OSError:
            return []
        return order

    def _order_mod_bsas_by_plugins(self, bsa_names: list[str]) -> list[str]:
        """Order mod BSAs so the conflict winner follows plugin load order.

        FO3/FNV resolve SArchiveList conflicts as *first listed wins*, while a
        plugin lower in plugins.txt (loaded later) is meant to override earlier
        ones. So the later-loading plugin's BSA must come first → SArchiveList
        order is the reverse of plugin load order.

        Each BSA maps to a plugin by name prefix: ``<plugin-stem>[ suffix].bsa``
        hooks to ``<plugin-stem>.es[pml]``. BSAs with no matching enabled plugin
        keep their relative order and sort after all matched ones.
        """
        order = self._plugin_load_order()
        if not order:
            return bsa_names
        # plugin stem (no extension) -> load index
        stem_rank: dict[str, int] = {}
        for i, plugin in enumerate(order):
            stem = plugin.rsplit(".", 1)[0]
            stem_rank[stem] = i

        def rank(bsa: str) -> int:
            stem = bsa.rsplit(".", 1)[0].lower()
            # longest matching plugin-stem prefix (handles "Name - Textures.bsa")
            best = -1
            for pstem, idx in stem_rank.items():
                if stem == pstem or stem.startswith(pstem + " "):
                    if idx > best:
                        best = idx
            return best

        ranked = [(rank(b), i, b) for i, b in enumerate(bsa_names)]
        # Matched BSAs first, by descending plugin index (later plugin wins →
        # earlier in list); then unmatched (rank -1) in original order.
        matched = sorted((t for t in ranked if t[0] >= 0),
                         key=lambda t: (-t[0], t[1]))
        unmatched = [t for t in ranked if t[0] < 0]
        return [b for _, _, b in matched] + [b for _, _, b in unmatched]

    def swap_launcher(self, log_fn) -> None:
        """Replace the game launcher with the script extender if present."""
        _log = log_fn
        if self._game_path is None:
            return
        if not self._script_extender_swap:
            _log("  Script extender / launcher swap disabled — skipping.")
            return
        se = self._game_path / self._script_extender_exe
        if not se.is_file():
            _log(f"  {self._script_extender_exe} not found — skipping launcher swap.")
            return
        launcher = self._game_path / self.exe_name
        backup   = self._game_path / (Path(self.exe_name).stem + ".bak")
        if launcher.is_file():
            launcher.rename(backup)
            _log(f"  Renamed {self.exe_name} → {backup.name}.")
        shutil.copy2(se, launcher)
        _log(f"  Copied {self._script_extender_exe} → {self.exe_name}.")

    def _restore_launcher(self, log_fn) -> None:
        """Reverse the script extender launcher swap if a backup exists."""
        _log = log_fn
        if self._game_path is None:
            return
        backup   = self._game_path / (Path(self.exe_name).stem + ".bak")
        launcher = self._game_path / self.exe_name
        if not backup.is_file():
            return
        if launcher.is_file():
            launcher.unlink()
        backup.rename(launcher)
        _log(f"  Restored {self.exe_name} from {backup.name}.")

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into the game's Data directory.

        Workflow:
          1. Move everything currently in Data/ → Data_Core/
          2. Hard-link every file listed in filemap.txt into Data/
          3. Hard-link vanilla files from Data_Core/ into Data/ for anything
             not provided by a mod
          4. Symlink the active profile's plugins.txt into the Proton prefix
          5. Swap launcher for FOSE
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data"
        filemap  = self.get_effective_filemap_path()
        staging  = self.get_effective_mod_staging_path()

        if not data_dir.is_dir():
            raise RuntimeError(f"Data directory not found: {data_dir}")
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        # Per-separator deploy overrides. Loaded here (from the real profile_dir,
        # which is where modlist.txt / profile_state.json live — the filemap may
        # sit at the shared-staging profile root instead) and passed explicitly
        # to both Step 0 and Step 2 so the self-load fallbacks in those functions
        # don't have to guess the profile dir from filemap_path.parent.
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        per_mod_modes = expand_separator_link_modes(_sep_deploy, _sep_entries) or None
        per_mod_raw = expand_separator_raw_deploy(_sep_deploy, _sep_entries) or None

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 0: Routing files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                per_mod_link_modes=per_mod_modes,
                raw_mods=per_mod_raw,
                log_fn=_log,
                progress_fn=progress_fn,
                prefix_root=self.get_prefix_path(),
            )

        _log("Step 1: Moving Data/ → Data_Core/ ...")
        move_to_core(data_dir, log_fn=_log)
        _log("  Backed up existing files → Data_Core/.")

        _log(f"Step 2: Transferring mod files into Data/ ({mode.name}) ...")
        linked_mod, placed = deploy_filemap(filemap, data_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            per_mod_link_modes=per_mod_modes,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            exclude=custom_exclude or None,
                                            core_dir=data_dir.parent / (data_dir.name + "_Core"))
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Data_Core/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log,
                                  manifest_dir=filemap.parent)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Symlinking plugins.txt into Proton prefix ...")
        self._symlink_plugins_txt(profile, _log)

        _log("Step 5: Symlinking profile INI files ...")
        self._symlink_profile_ini_files(profile, _log)

        _log("Step 6: Symlinking profile saves ...")
        self._symlink_profile_saves(profile, _log)

        _log("Step 7: Applying archive invalidation ...")
        self.apply_archive_invalidation(_log)

        if self._orders_plugins_by_mtime():
            _log("Step 8: Setting plugin mtimes to match load order ...")
            self.stamp_plugin_load_order(profile, _log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Data/."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore Data/ to its vanilla state by moving Data_Core/ back."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir = self._game_path / "Data"

        _log("Restore: reverting archive invalidation ...")
        self.revert_archive_invalidation(_log)

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(
            _profile_dir, _entries, log_fn=_log,
            filemap_path=self.get_effective_filemap_path(),
        )

        custom_rules = self.custom_routing_rules
        if custom_rules and self._game_path:
            _log("Restore: removing custom-routed files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path,
                rules=custom_rules,
                log_fn=_log,
                prefix_root=self.get_prefix_path(),
            )

        _log("Restore: clearing Data/ and moving Data_Core/ back ...")
        restored = restore_data_core(
            data_dir,
            overwrite_dir=self.get_effective_overwrite_path(),
            staging_root=self.get_effective_mod_staging_path(),
            strip_prefixes=self.mod_folder_strip_prefixes,
            log_fn=_log,
        )
        _log(f"  Restored {restored} file(s). Data_Core/ removed.")

        self._remove_plugins_txt_symlink(_log)
        self._restore_launcher(_log)

        _active = self._active_profile_dir
        if _active is not None:
            _log("Restore: removing profile INI symlinks ...")
            self._remove_profile_ini_symlinks(_active.name, _log)
            _log("Restore: removing profile saves symlinks ...")
            self._remove_profile_saves_symlinks(_active.name, _log)

        _log("Restore complete.")


class Fallout3_GOTY(Fallout_3):
    """Fallout 3 Game of the Year Edition — identical deployment to the base
    game, only the name, game_id, and steam_id differ."""

    @property
    def name(self) -> str:
        return "Fallout 3 GOTY"

    @property
    def game_id(self) -> str:
        return "Fallout3GOTY"

    @property
    def steam_id(self) -> str:
        return "22370"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout3"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="downgrade_fo3goty",
                label="Downgrade Fallout 3 GOTY",
                description=(
                    "Downgrade to pre-Anniversary Edition so that "
                    "the script extender (FOSE) works correctly."
                ),
                dialog_class_path="wizards.fallout_downgrade.FalloutDowngradeWizard",
            ),
            WizardTool(
                id="install_se_fo3goty",
                label="Install Script Extender (FOSE)",
                description="Download and install FOSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://fose.silverlock.org/download/fose_v1_2_beta2.7z",
                    "archive_keywords": ["fose"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_fo3goty",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="FO3Edit", id_suffix="fo3goty",
                nexus_url="https://www.nexusmods.com/fallout3/mods/637?tab=files",
            ),
        ]


class Fallout_NV(Fallout_3):

    synthesis_registry_name = "FalloutNV"

    _archive_list_fix_name = "JIP LN NVSE"
    _archive_list_fix_path = "Data/NVSE/Plugins/jip_nvse.dll"

    vanilla_plugins = ["FalloutNV.esm"]
    vanilla_dlc_plugins = [
        "DeadMoney.esm", "HonestHearts.esm", "OldWorldBlues.esm",
        "LonesomeRoad.esm", "GunRunnersArsenal.esm",
        "CaravanPack.esm", "ClassicPack.esm",
        "MercenaryPack.esm", "TribalPack.esm", "FalloutNV_lang.esp",
    ]

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_fonv",
                label="Install Script Extender (xNVSE)",
                description="Download and install xNVSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "github_api_url": "https://api.github.com/repos/xNVSE/NVSE/releases/latest",
                    "archive_keywords": ["nvse"],
                },
            ),
            WizardTool(
                id="fnv_4gb_patch",
                label="Apply 4GB Patch",
                description="Patch FalloutNV.exe to use 4 GB of memory (keeps a backup that can be restored).",
                dialog_class_path="wizards.fnv_4gb_patch.Fnv4GbPatchWizard",
            ),
            WizardTool(
                id="run_bethini_fonv",
                label="Run BethINI Pie",
                description="Install BethINI Pie and configure Fallout New Vegas INI settings.",
                dialog_class_path="wizards.bethini.BethINIWizard",
            ),
            WizardTool(
                id="run_wrye_bash_fonv",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="FNVEdit", id_suffix="fonv",
                nexus_url="https://www.nexusmods.com/newvegas/mods/34703?tab=files",
            ),
        ]

    @property
    def name(self) -> str:
        return "Fallout New Vegas"

    @property
    def game_id(self) -> str:
        return "FalloutNV"

    @property
    def exe_name(self) -> str:
        return "FalloutNVLauncher.exe"

    @property
    def steam_id(self) -> str:
        return "22380"

    @property
    def alt_steam_ids(self) -> list[str]:
        # 22490 is the Polish/Czech/Russian localized edition of FNV, which is
        # a separate Steam app sharing the same install/prefix layout. Owners of
        # that edition must launch through 22490, not 22380.
        return ["22490"]

    @property
    def nexus_game_domain(self) -> str:
        return "newvegas"

    @property
    def loot_game_type(self) -> str:
        return "FalloutNV"
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["nvse*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["nvse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["nvse*.pdb"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["FNVpatch.exe"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
                ]

    @property
    def loot_masterlist_repo(self) -> str:
        return "falloutnv"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/FalloutNV")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/FalloutNV GOG")
    _MYGAMES_SUBPATH = Path("FalloutNV")
    _MYGAMES_SUBPATH_GOG = Path("FalloutNV GOG")
    _ARCHIVE_INI_FILENAME = "Fallout.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "FalloutPrefs.ini"

    # MO2-style dummy-BSA invalidation (matches FalloutNVBSAInvalidation).
    _invalidation_bsa_name = "Fallout - Invalidation.bsa"
    _invalidation_bsa_version = 0x68

    @property
    def _script_extender_exe(self) -> str:
        return "nvse_loader.exe"


class Fallout_4(Fallout_3):

    _archive_list_needs_mod_bsas = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = [
        "Fallout4.esm",
        "DLCRobot.esm", "DLCworkshop01.esm", "DLCCoast.esm",
        "DLCworkshop02.esm", "DLCworkshop03.esm", "DLCNukaWorld.esm",
        "DLCUltraHighResolution.esm",
    ]
    vanilla_dlc_plugins: list[str] = []
    vanilla_ccc_filename = "Fallout4.ccc"
    synthesis_registry_name = "Fallout4"
    auto_install_deps = _MODERN_CREATION_ENGINE_DEPS

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def reshade_arch(self) -> int:
        return 64

    @property
    def wizard_tools(self) -> list[WizardTool]:
        from wizards.bodyslide import find_mod_exe
        bodyslide_tools = []
        if find_mod_exe(self, ("BodySlide.exe", "BodySlide x64.exe")) is not None:
            bodyslide_tools.append(WizardTool(
                id="run_bodyslide_fo4",
                label="Run BodySlide",
                description="Deploy mods and run BodySlide from the Data folder.",
                dialog_class_path="wizards.bodyslide.BodySlideWizard",
            ))
        if find_mod_exe(self, ("OutfitStudio.exe", "OutfitStudio x64.exe")) is not None:
            bodyslide_tools.append(WizardTool(
                id="run_outfitstudio_fo4",
                label="Run Outfit Studio",
                description="Deploy mods and run Outfit Studio from the Data folder.",
                dialog_class_path="wizards.bodyslide.OutfitStudioWizard",
            ))
        return self._base_wizard_tools() + bodyslide_tools + [
            WizardTool(
                id="install_se_fo4",
                label="Install Script Extender (F4SE)",
                description="Download and install F4SE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/fallout4/mods/42147",
                    "archive_keywords": ["Fallout 4 Script Extender"],
                },
            ),
            WizardTool(
                id="run_bethini_fo4",
                label="Run BethINI Pie",
                description="Install BethINI Pie and configure Fallout 4 INI settings.",
                dialog_class_path="wizards.bethini.BethINIWizard",
            ),
            WizardTool(
                id="run_wrye_bash_fo4",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="FO4Edit", id_suffix="fo4",
                nexus_url="https://www.nexusmods.com/fallout4/mods/2737?tab=files",
            ),
        ]

    @property
    def name(self) -> str:
        return "Fallout 4"

    @property
    def game_id(self) -> str:
        return "Fallout4"

    @property
    def exe_name(self) -> str:
        return "Fallout4Launcher.exe"

    @property
    def steam_id(self) -> str:
        return "377160"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout4"

    @property
    def loot_game_type(self) -> str:
        return "Fallout4"

    @property
    def archive_extensions(self) -> frozenset[str]:
        return frozenset({".ba2"})

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["f4se_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["f4se*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["CustomControlMap.txt"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
                ]

    @property
    def loot_masterlist_repo(self) -> str:
        return "fallout4"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout4")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Fallout4 GOG")
    _MYGAMES_SUBPATH = Path("Fallout4")
    _MYGAMES_SUBPATH_GOG = Path("Fallout4 GOG")
    _ARCHIVE_INI_FILENAME = "Fallout4.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "Fallout4Prefs.ini"
    _archive_invalidation_extra_keys = (("sResourceDataDirsFinal", ""),)
    # BA2-based — no dummy BSA, only the bInvalidateOlderFiles INI key.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None

    @property
    def _script_extender_exe(self) -> str:
        return "f4se_loader.exe"


class Fallout_4VR(Fallout_3):

    _archive_list_needs_mod_bsas = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = ["Fallout4.esm", "Fallout4_VR.esm"]
    vanilla_dlc_plugins: list[str] = []
    synthesis_registry_name = "Fallout 4 VR"
    auto_install_deps = _MODERN_CREATION_ENGINE_DEPS

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def reshade_arch(self) -> int:
        return 64

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_fo4vr",
                label="Install Script Extender (F4SEVR)",
                description="Download and install F4SEVR into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/fallout4/mods/42159",
                    "archive_keywords": ["Fallout 4 Script Extender VR"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_fo4vr",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="FO4VREdit", id_suffix="fo4vr", qac=False,
                nexus_url="https://www.nexusmods.com/fallout4/mods/2737?tab=files",
            ),
        ]

    @property
    def name(self) -> str:
        return "Fallout 4 VR"

    @property
    def game_id(self) -> str:
        return "Fallout4VR"

    @property
    def exe_name(self) -> str:
        return "Fallout4VR.exe"

    @property
    def steam_id(self) -> str:
        return "611660"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout4"
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["f4sevr_steam_loader.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["f4sevr_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["f4sevr*.dll"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
                ]

    @property
    def loot_game_type(self) -> str:
        return "Fallout4VR"

    @property
    def archive_extensions(self) -> frozenset[str]:
        return frozenset({".ba2"})

    @property
    def loot_masterlist_repo(self) -> str:
        return "fallout4vr"

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout4VR")
    _APPDATA_SUBPATH_GOG = None
    _MYGAMES_SUBPATH = Path("Fallout4VR")
    _MYGAMES_SUBPATH_GOG = None
    _ARCHIVE_INI_FILENAME = "Fallout4.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "Fallout4Prefs.ini"
    _archive_invalidation_extra_keys = (("sResourceDataDirsFinal", ""),)
    # BA2-based — no dummy BSA.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None

    @property
    def _script_extender_exe(self) -> str:
        return "f4sevr_loader.exe"


class Oblivion(Fallout_3):

    # Don't force/reorder mod BSAs in SArchiveList (inherits False). Oblivion
    # auto-loads a mod's BSA via plugin-name association (the ESP loads it),
    # AFTER the vanilla archives. Reliable BSA-over-vanilla override needs the
    # SkyBSA OBSE plugin (reverses the in-memory list so the latest-loaded BSA
    # wins); forcing the mod BSA early would invert that, and the 256-char
    # SArchiveList limit makes registration impractical anyway.
    _archive_list_needs_mod_bsas = False
    # OblivionPrefs.ini does NOT manage SArchiveList; writing a partial list
    # there shadowed Oblivion.ini's full list and broke BSA loading for every
    # mod. Keep the archive list out of the Prefs INI.
    _archive_list_in_prefs_ini = False
    vanilla_plugins = ["Oblivion.esm", "Update.esm"]
    vanilla_dlc_plugins = [
        "DLCShiveringIsles.esp", "Knights.esp",
        "DLCBattlehornCastle.esp", "DLCFrostcrag.esp",
        "DLCSpellTomes.esp", "DLCMehrunesRazor.esp",
        "DLCOrrery.esp", "DLCThievesDen.esp",
        "DLCVileLair.esp", "DLCHorseArmor.esp",
    ]
    synthesis_registry_name = "Oblivion"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_oblivion",
                label="Install Script Extender (OBSE)",
                description="Download and install OBSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/oblivion/mods/37952",
                    "archive_keywords": ["xobse"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_oblivion",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="TES4Edit", id_suffix="oblivion",
                nexus_url="https://www.nexusmods.com/oblivion/mods/11536?tab=files",
            ),
        ]

    @property
    def name(self) -> str:
        return "Oblivion"

    @property
    def game_id(self) -> str:
        return "Oblivion"

    @property
    def exe_name(self) -> str:
        return "OblivionLauncher.exe"

    @property
    def plugin_extensions(self) -> list[str]:
        return [".esp", ".esm"]

    @property
    def steam_id(self) -> str:
        return "22330"

    @property
    def nexus_game_domain(self) -> str:
        return "oblivion"

    @property
    def loot_game_type(self) -> str:
        return "Oblivion"

    @property
    def loot_masterlist_repo(self) -> str:
        return "oblivion"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["obse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["obse*.dll"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Oblivion")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Oblivion GOG")
    _MYGAMES_SUBPATH = Path("Oblivion")
    _MYGAMES_SUBPATH_GOG = Path("Oblivion GOG")
    _PLUGINS_TXT_FILENAME = "Plugins.txt"
    _ARCHIVE_INI_FILENAME = "Oblivion.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "OblivionPrefs.ini"
    # MO2-style dummy-BSA invalidation (Oblivion engine: bsa version 0x67).
    _invalidation_bsa_name = "Oblivion - Invalidation.bsa"
    _invalidation_bsa_version = 0x67

    @property
    def _script_extender_exe(self) -> str:
        return "obse_loader.exe"

    def _delete_dummy_bsa_file(self, _log) -> None:
        """Also clean up any legacy ArchiveInvalidation.txt left from the
        pre-migration codepath."""
        super()._delete_dummy_bsa_file(_log)
        if self._game_path is None:
            return
        legacy = self._game_path / "ArchiveInvalidation.txt"
        if legacy.is_file():
            try:
                legacy.unlink()
                _log("  Removed legacy ArchiveInvalidation.txt.")
            except OSError:
                pass


class Skyrim(Fallout_3):

    _archive_list_needs_mod_bsas = False
    # Skyrim 1.4.26+ orders plugins by plugins.txt, not file mtimes.
    _plugin_load_order_by_mtime = False
    vanilla_plugins = ["Skyrim.esm", "Update.esm"]
    vanilla_dlc_plugins = [
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
        "HighResTexturePack01.esp", "HighResTexturePack02.esp",
        "HighResTexturePack03.esp",
    ]
    synthesis_registry_name = "Skyrim"

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_skyrim",
                label="Install Script Extender (SKSE)",
                description="Download and install SKSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://skse.silverlock.org/beta/skse_1_07_03.7z",
                    "archive_keywords": ["skse"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_skyrim",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="TES5Edit", id_suffix="skyrim",
                nexus_url="https://www.nexusmods.com/skyrim/mods/25859?tab=files",
            ),
            WizardTool(
                id="run_skygen_skyrim",
                label="SkyGen — Patch Generator",
                description="Scan your load order for BOS / SkyPatcher patch coverage and generate new patches.",
                dialog_class_path="wizards.skygen.SkyGenWizard",
                extra={"_full_width_overlay": True},
            ),
            WizardTool(
                id="run_plugin_audit_skyrim",
                label="Plugin Audit & Cleanup",
                description=(
                    "Scan load order for safe-to-disable plugins, then clean up orphaned "
                    "SkyGen BOS/SkyPatcher INIs for plugins that must stay enabled."
                ),
                dialog_class_path="wizards.plugin_audit.PluginAuditWizard",
                extra={"_full_width_overlay": True},
            ),
        ]

    @property
    def name(self) -> str:
        return "Skyrim"

    @property
    def game_id(self) -> str:
        return "skyrim"

    @property
    def exe_name(self) -> str:
        return "SkyrimLauncher.exe"

    @property
    def steam_id(self) -> str:
        return "72850"

    @property
    def nexus_game_domain(self) -> str:
        return "skyrim"

    @property
    def loot_game_type(self) -> str:
        return "Skyrim"

    @property
    def loot_masterlist_repo(self) -> str:
        return "skyrim"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["skse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["skse*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Skyrim GOG")
    _MYGAMES_SUBPATH = Path("Skyrim")
    _MYGAMES_SUBPATH_GOG = Path("Skyrim GOG")
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "SkyrimPrefs.ini"
    _invalidation_bsa_name = "Skyrim - Invalidation.bsa"
    _invalidation_bsa_version = 0x68

    @property
    def _script_extender_exe(self) -> str:
        return "skse_loader.exe"


class SkyrimVR(Fallout_3):

    _archive_list_needs_mod_bsas = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = [
        "Skyrim.esm", "Update.esm",
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
    ]
    vanilla_dlc_plugins: list[str] = []
    vanilla_ccc_filename = "Skyrim.ccc"
    synthesis_registry_name = "Skyrim VR"
    auto_install_deps = _MODERN_CREATION_ENGINE_DEPS

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def reshade_arch(self) -> int:
        return 64

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_skyrimvr",
                label="Install Script Extender (SKSEVR)",
                description="Download and install SKSEVR into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://skse.silverlock.org/beta/sksevr_2_00_12.7z",
                    "archive_keywords": ["sksevr"],
                },
            ),
            WizardTool(
                id="run_wrye_bash_skyrimvr",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="TES5VREdit", id_suffix="skyrimvr", qac=False,
                nexus_url="https://www.nexusmods.com/skyrimspecialedition/mods/164?tab=files",
            ),
            WizardTool(
                id="run_skygen_skyrimvr",
                label="SkyGen — Patch Generator",
                description="Scan your load order for BOS / SkyPatcher patch coverage and generate new patches.",
                dialog_class_path="wizards.skygen.SkyGenWizard",
                extra={"_full_width_overlay": True},
            ),
            WizardTool(
                id="run_plugin_audit_skyrimvr",
                label="Plugin Audit & Cleanup",
                description=(
                    "Scan load order for safe-to-disable plugins, then clean up orphaned "
                    "SkyGen BOS/SkyPatcher INIs for plugins that must stay enabled."
                ),
                dialog_class_path="wizards.plugin_audit.PluginAuditWizard",
                extra={"_full_width_overlay": True},
            ),
        ]

    @property
    def name(self) -> str:
        return "Skyrim VR"

    @property
    def game_id(self) -> str:
        return "skyrimvr"

    @property
    def exe_name(self) -> str:
        return "SkyrimVR.exe"

    @property
    def steam_id(self) -> str:
        return "611670"

    @property
    def nexus_game_domain(self) -> str:
        return "skyrimspecialedition"

    @property
    def loot_game_type(self) -> str:
        return "SkyrimVR"

    @property
    def loot_masterlist_repo(self) -> str:
        return "skyrimvr"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["sksevr_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["sksevr*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim VR")
    _APPDATA_SUBPATH_GOG = None
    _MYGAMES_SUBPATH = Path("Skyrim VR")
    _MYGAMES_SUBPATH_GOG = None
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "SkyrimPrefs.ini"
    # Runs on the SSE engine fork — same reasoning as SkyrimSE (no dummy BSA).
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None

    @property
    def _script_extender_exe(self) -> str:
        return "sksevr_loader.exe"


class Starfield(Fallout_3):

    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = [
        "Starfield.esm", "Constellation.esm", "ShatteredSpace.esm",
        "OldMars.esm", "SFBGS003.esm", "SFBGS004.esm", "SFBGS006.esm",
        "SFBGS007.esm", "SFBGS008.esm", "BlueprintShips-Starfield.esm",
        "SFBGS00D.esm", "SFBGS047.esm", "SFBGS050.esm", "BlueprintShips-SFBGS050.esm",
    ]
    vanilla_dlc_plugins: list[str] = []
    vanilla_ccc_filename = "Starfield.ccc"
    synthesis_registry_name = "Starfield"
    auto_install_deps = _MODERN_CREATION_ENGINE_DEPS

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def reshade_arch(self) -> int:
        return 64

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="install_se_starfield",
                label="Install Script Extender (SFSE)",
                description="Download and install SFSE into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "download_url": "https://www.nexusmods.com/starfield/mods/106",
                    "archive_keywords": ["sfse"],
                },
            ),
            WizardTool(
                id="run_bethini_starfield",
                label="Run BethINI Pie",
                description="Install BethINI Pie and configure Starfield INI settings.",
                dialog_class_path="wizards.bethini.BethINIWizard",
            ),
            WizardTool(
                id="run_wrye_bash_starfield",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="SF1Edit", id_suffix="starfield",
                nexus_url="https://www.nexusmods.com/starfield/mods/121?tab=files",
            ),
        ]

    @property
    def name(self) -> str:
        return "Starfield"

    @property
    def game_id(self) -> str:
        return "Starfield"

    @property
    def exe_name(self) -> str:
        # Starfield has no separate launcher; the main executable is the launch target.
        return "Starfield.exe"

    @property
    def steam_id(self) -> str:
        return "1716740"

    @property
    def nexus_game_domain(self) -> str:
        return "starfield"
    
    @property
    def loot_game_type(self) -> str:
        return "Starfield"

    @property
    def archive_extensions(self) -> frozenset[str]:
        return frozenset({".ba2"})

    @property
    def loot_masterlist_repo(self) -> str:
        return "starfield"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["sfse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["sfse*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".sfs"]),
        ]

    # plugins.txt lives at AppData/Local/Starfield/plugins.txt — same pattern as other Bethesda titles.
    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Starfield")
    _APPDATA_SUBPATH_GOG = None
    _MYGAMES_SUBPATH = Path("Starfield")
    _MYGAMES_SUBPATH_GOG = None
    _ARCHIVE_INI_FILENAME = "Starfield.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "StarfieldPrefs.ini"
    # BA2-based — no dummy BSA.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None
    _archive_list_needs_mod_bsas = False

    @property
    def _script_extender_exe(self) -> str:
        return "sfse_loader.exe"

    def _plugins_txt_target(self) -> Path | None:
        """Return the in-prefix path where Starfield expects Plugins.txt (capital P)."""
        if self._prefix_path is None:
            return None
        return self._prefix_path / self._APPDATA_SUBPATH / "Plugins.txt"

    def _symlink_plugins_txt(self, profile: str, log_fn) -> None:
        """Write a Blueprint-stripped copy of Plugins.txt into the prefix.

        Starfield silently drops every plugin appearing after a Blueprint
        (or BlueprintShips) plugin in Plugins.txt, so the prefix-side file
        must omit them entirely — matching libloadorder's behavior. The
        profile's plugins.txt is left untouched so blueprints stay visible
        in the load-order UI.
        """
        from Utils.plugin_parser import is_blueprint_flagged
        from Utils.plugins import read_plugins

        _log = log_fn
        target = self._plugins_txt_target()
        if target is None:
            _log("  WARN: Prefix path not set — skipping Plugins.txt write.")
            return

        source = self.get_profile_root() / "profiles" / profile / "plugins.txt"
        if not source.is_file():
            _log(f"  WARN: plugins.txt not found at {source} — skipping write.")
            return

        if self._game_path is None:
            _log("  WARN: Game path not set — skipping Plugins.txt write.")
            return
        data_dir = self._game_path / "Data"

        entries = read_plugins(source, star_prefix=True)
        kept: list = []
        stripped: list[str] = []
        for e in entries:
            plugin_file = data_dir / e.name
            if plugin_file.is_file() and is_blueprint_flagged(plugin_file):
                stripped.append(e.name)
                continue
            kept.append(e)

        if target.exists() or target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [(f"*{e.name}" if e.enabled else e.name) for e in kept]
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        if stripped:
            _log(f"  Stripped {len(stripped)} Blueprint plugin(s) from Plugins.txt: "
                 + ", ".join(stripped))
        _log(f"  Wrote Plugins.txt → {target}")

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        """Remove the Plugins.txt copy from the prefix on restore."""
        _log = log_fn
        target = self._plugins_txt_target()
        if target is None:
            return
        if target.exists() or target.is_symlink():
            target.unlink()
            _log("  Removed Plugins.txt from prefix.")

    def swap_launcher(self, log_fn) -> None:
        """Replace Starfield.exe with sfse_loader.exe and write Data/SFSE/sfse.ini.

        SFSE reads its RuntimeName setting from Data/SFSE/sfse.ini when the
        loader has been renamed away from sfse_loader.exe.
        """
        super().swap_launcher(log_fn)
        _log = log_fn
        if self._game_path is None:
            return
        backup_name = Path(self.exe_name).stem + ".bak"
        backup = self._game_path / backup_name
        if not backup.is_file():
            return
        sfse_ini = self._game_path / "Data" / "SFSE" / "sfse.ini"
        sfse_ini.parent.mkdir(parents=True, exist_ok=True)
        sfse_ini.write_text(f"[Loader]\nRuntimeName={backup_name}\n", encoding="utf-8")
        _log(f"  Wrote Data/SFSE/sfse.ini (RuntimeName={backup_name}).")

    def _restore_launcher(self, log_fn) -> None:
        """Reverse the launcher swap and remove Data/SFSE/sfse.ini."""
        super()._restore_launcher(log_fn)
        _log = log_fn
        if self._game_path is None:
            return
        sfse_ini = self._game_path / "Data" / "SFSE" / "sfse.ini"
        if sfse_ini.is_file():
            sfse_ini.unlink()
            _log("  Removed Data/SFSE/sfse.ini.")

class Enderal(Fallout_3):

    _archive_list_needs_mod_bsas = False
    # Skyrim LE engine — plugins.txt-ordered, not file mtimes.
    _plugin_load_order_by_mtime = False
    vanilla_plugins = ["Skyrim.esm", "Update.esm", "Enderal - Forgotten Stories.esm"]
    vanilla_dlc_plugins = [
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
        "HighResTexturePack01.esp", "HighResTexturePack02.esp",
        "HighResTexturePack03.esp",
    ]
    synthesis_registry_name = "Enderal"

    @property
    def name(self) -> str:
        return "Enderal"

    @property
    def game_id(self) -> str:
        return "enderal"

    @property
    def exe_name(self) -> str:
        return "Enderal Launcher.exe"

    @property
    def steam_id(self) -> str:
        return "933480"

    @property
    def nexus_game_domain(self) -> str:
        return "enderal"

    @property
    def loot_game_type(self) -> str:
        return "Skyrim"

    @property
    def loot_masterlist_repo(self) -> str:
        return "enderal"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["skse_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["skse*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/enderal")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/enderal GOG")
    _MYGAMES_SUBPATH = Path("Enderal")
    _MYGAMES_SUBPATH_GOG = Path("Enderal GOG")
    _ARCHIVE_INI_FILENAME = "Enderal.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "EnderalPrefs.ini"
    _invalidation_bsa_name = "Enderal - Invalidation.bsa"
    _invalidation_bsa_version = 0x68

    @property
    def _script_extender_exe(self) -> str:
        return "skse_loader.exe"

    def swap_launcher(self, log_fn) -> None:
        # Enderal Launcher.exe already bootstraps SKSE; swapping breaks it.
        log_fn("  Enderal Launcher invokes SKSE internally — skipping launcher swap.")

    def _restore_launcher(self, log_fn) -> None:
        # Migration path: undo any prior swap left over from earlier versions.
        super()._restore_launcher(log_fn)

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="run_wrye_bash_enderal",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="TES5Edit", id_suffix="enderal",
                nexus_url="https://www.nexusmods.com/skyrim/mods/25859?tab=files",
            ),
        ]

class EnderalSE(Fallout_3):

    _archive_list_needs_mod_bsas = False
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = [
        "Skyrim.esm", "Update.esm",
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
        "Enderal - Forgotten Stories.esm",
    ]
    vanilla_dlc_plugins: list[str] = []
    synthesis_registry_name = "Enderal Special Edition"
    auto_install_deps = _MODERN_CREATION_ENGINE_DEPS

    @property
    def name(self) -> str:
        return "Enderal SE"

    @property
    def game_id(self) -> str:
        return "enderalse"

    @property
    def exe_name(self) -> str:
        return "Enderal Launcher.exe"

    @property
    def steam_id(self) -> str:
        return "976620"

    @property
    def nexus_game_domain(self) -> str:
        return "enderalspecialedition"

    @property
    def loot_game_type(self) -> str:
        return "SkyrimSE"

    @property
    def loot_masterlist_repo(self) -> str:
        return "enderal"

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["skse64_loader.exe"], flatten=True, loose_only=True),
            CustomRule(dest="", filenames=["skse64*.dll"], flatten=True, loose_only=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".ess"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Enderal Special Edition")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Enderal Special Edition GOG")
    _MYGAMES_SUBPATH = Path("Enderal Special Edition")
    _MYGAMES_SUBPATH_GOG = Path("Enderal Special Edition GOG")
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "SkyrimPrefs.ini"
    # SSE-engine: see SkyrimSE — no dummy-BSA needed.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None

    @property
    def _script_extender_exe(self) -> str:
        return "skse64_loader.exe"

    def swap_launcher(self, log_fn) -> None:
        # Enderal Launcher.exe already bootstraps SKSE64; swapping breaks it.
        log_fn("  Enderal Launcher invokes SKSE64 internally — skipping launcher swap.")

    def _restore_launcher(self, log_fn) -> None:
        # Migration path: undo any prior swap left over from earlier versions.
        super()._restore_launcher(log_fn)

    @property
    def wizard_tools(self) -> list[WizardTool]:
        return self._base_wizard_tools() + [
            WizardTool(
                id="run_wrye_bash_enderalse",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            *self._xedit_wizard_tools(
                build="SSEEdit", id_suffix="enderalse",
                nexus_url="https://www.nexusmods.com/skyrimspecialedition/mods/164?tab=files",
            ),
        ]


class Fallout_76(Fallout_3):
    """Fallout 76 — a BA2-based Bethesda game with NO plugin system.

    The live game blocks .esp/.esm plugins, so there is no plugins.txt, no load
    order, and no LOOT/Synthesis. Mods load exclusively via the comma-separated
    ``sResourceArchive2List`` key in ``Fallout76Custom.ini`` (My Games/Fallout 76).
    We auto-sync that key from the deployed mod .ba2 files on every deploy/restore,
    mirroring the Vortex FO76 extension. The Archive tab (gated on archive_extensions)
    surfaces the deployed BA2s.
    """

    # No plugin system at all — empty plugin_extensions disables the Plugins tab,
    # load-order tracking, master logic, ESL flags, and orphan-plugin scanning.
    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = False
    vanilla_plugins: list[str] = []
    vanilla_dlc_plugins: list[str] = []
    # Saves are server-side (character files live on Bethesda's servers); the
    # local My Games\Fallout 76 folder holds only config/screenshots, so there
    # is no Saves folder to redirect.
    supports_profile_saves = False

    @property
    def name(self) -> str:
        return "Fallout 76"

    @property
    def game_id(self) -> str:
        return "Fallout76"

    @property
    def exe_name(self) -> str:
        return "Fallout76.exe"

    @property
    def steam_id(self) -> str:
        return "1151340"

    @property
    def nexus_game_domain(self) -> str:
        return "fallout76"

    @property
    def plugin_extensions(self) -> list[str]:
        # FO76 has no plugin system — disable all plugin tracking.
        return []

    @property
    def loot_sort_enabled(self) -> bool:
        return False

    @property
    def loot_game_type(self) -> str:
        return ""
    
    @property
    def conflict_ignore_filenames(self) -> set[str]:
        return {"info.xml","*read*.txt","*.jpg","*.png","Fallout76Custom.ini"}

    @property
    def archive_extensions(self) -> frozenset[str]:
        return frozenset({".ba2"})

    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["dxgi.dll"], flatten=True),
            CustomRule(dest="", folders=["Data"], flatten=True, loose_only=True),
            self._saves_routing_rule([".fos"]),
        ]

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Fallout76")
    _APPDATA_SUBPATH_GOG = None
    _MYGAMES_SUBPATH = Path("Fallout 76")
    _MYGAMES_SUBPATH_GOG = None
    _ARCHIVE_INI_FILENAME = "Fallout76.ini"
    _ARCHIVE_PREFS_INI_FILENAME = "Fallout76Prefs.ini"
    _CUSTOM_INI_FILENAME = "Fallout76Custom.ini"
    # BA2-based — no dummy BSA, only the sResourceArchive2List sync below.
    _invalidation_bsa_name = None
    _invalidation_bsa_version = None
    # We manage the archive list ourselves (see apply/revert below), so leave the
    # inherited FO3/FNV mod-BSA append path off.
    _archive_list_needs_mod_bsas = False
    _archive_list_fix_name = None
    _archive_list_fix_path = None
    _invalidation_archive_list_key = "sResourceArchive2List"

    # No plugins.txt — FO76 doesn't read one.
    def _plugins_txt_targets(self, prefix_root: "Path | None" = None) -> list[Path]:
        return []

    def _symlink_plugins_txt(self, profile: str, log_fn, prefix_root: "Path | None" = None) -> None:
        return

    def _remove_plugins_txt_symlink(self, log_fn) -> None:
        return

    @property
    def wizard_tools(self) -> list[WizardTool]:
        # No SE / Wrye Bash / BethINI — none apply to FO76.
        return self._base_wizard_tools()

    @property
    def frameworks(self) -> dict[str, str]:
        # FO76 has no script extender — skip framework detection entirely.
        return {}
    
    @property
    def reshade_dll(self) -> str:
        return ""

    @property
    def reshade_arch(self) -> int:
        return 64

    # -- Non-whitelisted DLL handling --------------------------------------
    # FO76's anti-cheat refuses to launch if unexpected *.dll files sit in the
    # game root. A mod that ships a stray DLL there would brick the game, so on
    # deploy we rename any non-whitelisted root DLL to <name>.dll.nwmode and on
    # restore we rename it back. Mirrors Fo76ini's RenameAddedDLLs/RestoreAddedDLLs.
    # Whitelist = the DLLs the vanilla game ships with (lower-cased for matching).
    _FO76_DLL_WHITELIST = frozenset({
        "bink2w64.dll", "chrome_elf.dll", "concrt140.dll", "d3dcompiler_43.dll",
        "d3dcompiler_46.dll", "d3dcompiler_47.dll", "libcef.dll", "libegl.dll",
        "libglesv2.dll", "msvcp140.dll", "ortp_x64.dll", "steam_api64.dll",
        "vccorlib140.dll", "vcruntime140.dll", "vivoxsdk_x64.dll", "dxgi.dll", 
        "vivoxsdk.dll", "xaudio2_9redist.dll"
    })

    def _rename_non_whitelisted_dlls(self, log_fn) -> None:
        """Rename non-whitelisted root *.dll → *.dll.nwmode so FO76 will launch."""
        if self._game_path is None:
            return
        try:
            entries = list(self._game_path.iterdir())
        except OSError:
            return
        for dll in entries:
            # Case-insensitive .dll match — the prefix FS is case-preserving and
            # mods may ship MyMod.DLL etc.
            if not dll.is_file() or not dll.name.lower().endswith(".dll"):
                continue
            if dll.name.lower() in self._FO76_DLL_WHITELIST:
                continue
            target = dll.with_name(dll.name + ".nwmode")
            try:
                if target.exists():
                    dll.unlink()  # a prior .nwmode already holds the original
                    log_fn(f"  Removed duplicate non-whitelisted DLL: {dll.name}")
                else:
                    dll.rename(target)
                    log_fn(f"  Renamed non-whitelisted DLL: {dll.name} → {target.name}")
            except OSError as exc:
                log_fn(f"  WARN: could not rename {dll.name}: {exc}")

    def _restore_non_whitelisted_dlls(self, log_fn) -> None:
        """Rename *.dll.nwmode back to *.dll on restore."""
        if self._game_path is None:
            return
        try:
            entries = list(self._game_path.iterdir())
        except OSError:
            return
        for nw in entries:
            if not nw.is_file() or not nw.name.lower().endswith(".nwmode"):
                continue
            original = nw.with_name(nw.name[: -len(".nwmode")])
            try:
                if original.exists():
                    nw.unlink()  # original was re-added during deploy — drop the stash
                    log_fn(f"  Removed stale {nw.name} ({original.name} present)")
                else:
                    nw.rename(original)
                    log_fn(f"  Restored DLL: {nw.name} → {original.name}")
            except OSError as exc:
                log_fn(f"  WARN: could not restore {nw.name}: {exc}")

    def swap_launcher(self, log_fn) -> None:
        # FO76 has no SE launcher to swap — repurpose this post-deploy hook to
        # quarantine non-whitelisted DLLs (game files are all in place by now).
        self._rename_non_whitelisted_dlls(log_fn)

    def _restore_launcher(self, log_fn) -> None:
        # Undo the DLL quarantine on restore (mirrors swap_launcher above).
        self._restore_non_whitelisted_dlls(log_fn)

    # -- sResourceArchive2List sync ----------------------------------------
    # FO76's only load mechanism. We keep the enabled mods' .ba2 filenames in
    # Fallout76Custom.ini's sResourceArchive2List, preserving any user-added
    # entries, and remove them again on restore. The set of "ours" is tracked in
    # managed_archives.txt so removed mods don't leave stale entries.

    _FO76_CUSTOM_INI_DEFAULTS = (
        ("sResourceDataDirsFinal", "STRINGS\\"),
        ("bInvalidateOlderFiles", "1"),
    )

    def _fo76_custom_ini_paths(self) -> list[Path]:
        return [d / self._CUSTOM_INI_FILENAME
                for d in {p.parent for p in self._get_archive_ini_paths()}]

    def apply_archive_invalidation(self, log_fn) -> None:
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        if not self.archive_invalidation:
            self.revert_archive_invalidation(_log)
            return
        custom_inis = self._fo76_custom_ini_paths()
        if not custom_inis:
            _log("  WARN: Prefix path not set — skipping FO76 archive sync.")
            return

        from Utils.bsa_invalidation import (
            append_to_archive_list, remove_many_from_archive_list,
        )
        key = self._invalidation_archive_list_key
        prev = self._tracked_mod_bsas()
        new = self._deployed_mod_bsas()
        for ini in custom_inis:
            ini.parent.mkdir(parents=True, exist_ok=True)
            # Seed the Vortex-style defaults only when absent (don't clobber
            # user edits to these keys).
            for k, v in self._FO76_CUSTOM_INI_DEFAULTS:
                if _read_ini_key(ini, "Archive", k) is None:
                    _set_ini_key(ini, "Archive", k, v)
            current = _read_ini_key(ini, "Archive", key) or ""
            # Drop the .ba2 entries we previously added, then re-add what's
            # deployed now — user-added entries (never in the sidecar) survive.
            updated = remove_many_from_archive_list(current, prev)
            updated = append_to_archive_list(updated, new)
            if updated != current:
                _set_ini_key(ini, "Archive", key, updated)
        self._save_tracked_mod_bsas(new)
        names = ", ".join(i.name for i in custom_inis)
        _log(f"  Synced {len(new)} mod BA2(s) into {key} ({names}).")

    def revert_archive_invalidation(self, log_fn) -> None:
        _log = log_fn
        if not self.archive_invalidation_enabled:
            return
        custom_inis = [i for i in self._fo76_custom_ini_paths() if i.is_file()]
        if not custom_inis:
            return
        from Utils.bsa_invalidation import remove_many_from_archive_list
        key = self._invalidation_archive_list_key
        tracked = self._tracked_mod_bsas()
        for ini in custom_inis:
            current = _read_ini_key(ini, "Archive", key)
            if current is None:
                continue
            updated = remove_many_from_archive_list(current, tracked)
            if updated != current:
                _set_ini_key(ini, "Archive", key, updated or None)
        self._save_tracked_mod_bsas([])
        names = ", ".join(i.name for i in custom_inis)
        _log(f"  Removed managed BA2 entries from {key} ({names}).")
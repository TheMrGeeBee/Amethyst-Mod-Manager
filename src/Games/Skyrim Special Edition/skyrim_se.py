"""
skyrim_se.py
Game handler for Skyrim Special Edition.

Mod structure:
  Mods install into <game_path>/Data/
  Staged mods live in Profiles/Skyrim Special Edition/mods/
"""

import shutil
from pathlib import Path

from Games.Bethesda.Bethesda import Fallout_3
from Games.base_game import WizardTool
from Utils.deploy import LinkMode, deploy_core, deploy_custom_rules, deploy_filemap, load_per_mod_strip_prefixes, load_separator_deploy_paths, expand_separator_deploy_paths, cleanup_custom_deploy_dirs, restore_custom_rules, restore_data_core, move_to_core
from Utils.modlist import read_modlist


class SkyrimSE(Fallout_3):

    plugins_use_star_prefix = True
    plugins_include_vanilla = False
    supports_esl_flag = True
    vanilla_plugins = [
        "Skyrim.esm", "Update.esm",
        "Dawnguard.esm", "HearthFires.esm", "Dragonborn.esm",
    ]
    vanilla_dlc_plugins: list[str] = []
    vanilla_ccc_filename = "Skyrim.ccc"
    synthesis_registry_name = "Skyrim Special Edition"

    # -----------------------------------------------------------------------
    # Identity
    # -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "Skyrim Special Edition"

    @property
    def game_id(self) -> str:
        return "skyrim_se"

    @property
    def exe_name(self) -> str:
        return "SkyrimSELauncher.exe"

    @property
    def steam_id(self) -> str:
        return "489830"

    @property
    def nexus_game_domain(self) -> str:
        return "skyrimspecialedition"

    @property
    def mod_required_top_level_folders(self) -> set[str]:
        # Skyrim SE subset — excludes Fallout-specific folders (f4se, nvse,
        # fose, config) that Fallout_3 includes.
        return {
            "skse",
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
            "grass",
            "video",
            "source",
            "calientetools",
            "data",
            "PBRNifPatcher",
            "PBRTextureSets",
            "distantlod",
            "fonts",
            "facegen",
            "menus",
            "lodsettings",
            "lsdata",
            "strings",
            "trees",
            "asi",
            "tools",
        }

    @property
    def mod_folder_strip_prefixes_post(self) -> set[str]:
        return {"data"}

    @property
    def loot_game_type(self) -> str:
        return "SkyrimSE"

    @property
    def loot_masterlist_repo(self) -> str:
        return "skyrimse"

    @property
    def frameworks(self) -> dict[str, str]:
        return {"Script Extender": "skse64_loader.exe"}

    @property
    def reshade_dll(self) -> str:
        return "dxgi.dll"

    @property
    def reshade_arch(self) -> int:
        return 64
    
    @property
    def custom_routing_rules(self) -> list:
        from Utils.deploy import CustomRule
        return [
            CustomRule(dest="", filenames=["d3dx9_42.dll"], flatten=True),
            CustomRule(dest="", filenames=["skse64_1*.dll"], flatten=True),
            CustomRule(dest="", filenames=["skse64_loader.exe"], flatten=True),
            CustomRule(dest="", filenames=["d3dcompiler_47.dll"], flatten=True),
            CustomRule(dest="Data/SKSE/Plugins/CharGen/Presets", extensions=[".jslot"], flatten=True),
            # ENB Series files → game root
            CustomRule(dest="", filenames=[
                "d3d11.dll",
                "d3dcompiler_46e.dll",
                "enbadaptation.fx",
                "enbbloom.fx",
                "enbdepthoffield.fx",
                "enbeffect.fx",
                "enbeffectpostpass.fx",
                "enbeffectprepass.fx",
                "enblens.fx",
                "enblocal.ini",
                "enbpalette.bmp",
                "enbraindrops.dds",
                "enbseries.ini",
                "enbsunsprite.bmp",
                "enbsunsprite.fx",
                "enbunderwater.fx",
                "enbunderwaternoise.bmp",
            ], flatten=True),
            CustomRule(dest="", folders=["enbseries"], flatten=True),
        ]

    @property
    def wizard_tools(self) -> list[WizardTool]:
        from wizards.pandora import find_pandora_exe
        from wizards.bodyslide import find_mod_exe
        pandora_tools = []
        if find_pandora_exe(self) is not None:
            pandora_tools.append(WizardTool(
                id="run_pandora_skyrimse",
                label="Run Pandora",
                description="Deploy mods and run Pandora Behaviour Engine+.",
                dialog_class_path="wizards.pandora.PandoraWizard",
            ))
        if find_mod_exe(self, "BodySlide x64.exe") is not None:
            pandora_tools.append(WizardTool(
                id="run_bodyslide_skyrimse",
                label="Run BodySlide",
                description="Deploy mods and run BodySlide x64.exe from the Data folder.",
                dialog_class_path="wizards.bodyslide.BodySlideWizard",
            ))
        if find_mod_exe(self, "OutfitStudio x64.exe") is not None:
            pandora_tools.append(WizardTool(
                id="run_outfitstudio_skyrimse",
                label="Run Outfit Studio",
                description="Deploy mods and run OutfitStudio x64.exe from the Data folder.",
                dialog_class_path="wizards.bodyslide.OutfitStudioWizard",
            ))
        return self._base_wizard_tools() + pandora_tools + [
            WizardTool(
                id="install_se_skyrimse",
                label="Install Script Extender (SKSE64)",
                description="Download and install SKSE64 into the game folder.",
                dialog_class_path="wizards.script_extender.ScriptExtenderWizard",
                extra={
                    "versions": [
                        {
                            "label": "Skyrim SE 1.6.1170 (Steam, current)",
                            "description": "Latest SKSE64 release from GitHub. Use this for up-to-date Steam installs.",
                            "github_api_url": "https://api.github.com/repos/ianpatt/skse64/releases/latest",
                            "archive_keywords": ["skse64"],
                        },
                        {
                            "label": "Skyrim SE GOG 1.6.1179",
                            "description": "GOG build of SKSE64 (skse64_2_02_06_gog.7z). Not available on GitHub.",
                            "direct_download_url": "https://skse.silverlock.org/beta/skse64_2_02_06_gog.7z",
                        },
                        {
                            "label": "Skyrim SE 1.5.97 (legacy)",
                            "description": "SKSE64 2.0.20 for older 1.5.97 installs (Special Edition pre-AE).",
                            "github_api_url": "https://api.github.com/repos/ianpatt/skse64/releases/tags/v2.0.20",
                            "archive_keywords": ["skse64"],
                        },
                    ],
                },
            ),
            WizardTool(
                id="run_pgpatcher_skyrimse",
                label="Run PGPatcher",
                description="Install PGPatcher, deploy mods, and run PGPatcher.exe.",
                dialog_class_path="wizards.pgpatcher.PGPatcherWizard",
            ),
            WizardTool(
                id="run_sseedit_skyrimse",
                label="Run SSEEdit",
                description="Install SSEEdit, deploy mods, and run SSEEdit.exe.",
                dialog_class_path="wizards.sseedit.SSEEditWizard",
            ),
            WizardTool(
                id="run_sseedit_qac_skyrimse",
                label="Run SSEEdit QAC",
                description="Deploy mods and run SSEEditQuickAutoClean.exe.",
                dialog_class_path="wizards.sseedit.SSEEditQACWizard",
            ),
            WizardTool(
                id="run_texgen_skyrimse",
                label="Run TexGen",
                description="Install DynDOLOD tools, deploy mods, and run TexGenx64.exe.",
                dialog_class_path="wizards.dyndolod.TexGenWizard",
            ),
            WizardTool(
                id="run_dyndolod_skyrimse",
                label="Run DynDOLOD",
                description="Install DynDOLOD tools, deploy mods, and run DynDOLODx64.exe.",
                dialog_class_path="wizards.dyndolod.DynDOLODWizard",
            ),
            WizardTool(
                id="run_xlodgen_skyrimse",
                label="Run xLODGen",
                description="Install xLODGen, deploy mods, and run xLODGenx64.exe.",
                dialog_class_path="wizards.dyndolod.xLODGenWizard",
            ),
            WizardTool(
                id="run_bethini_skyrimse",
                label="Run BethINI Pie",
                description="Install BethINI Pie and configure Skyrim SE INI settings.",
                dialog_class_path="wizards.bethini.BethINIWizard",
            ),
            WizardTool(
                id="run_wrye_bash_skyrimse",
                label="Run Wrye Bash",
                description="Download and run Wrye Bash.",
                dialog_class_path="wizards.wrye_bash.WryeBashWizard",
            ),
            WizardTool(
                id="run_vramr_skyrimse",
                label="Run VRAMr",
                description="Download VRAMr from Nexus, deploy mods, and run texture optimisation.",
                dialog_class_path="wizards.vramr.VRAMrWizard",
            ),
            WizardTool(
                id="run_bendr_skyrimse",
                label="Run BENDr",
                description="Download BENDr from Nexus, deploy mods, and process normal maps.",
                dialog_class_path="wizards.bendr_parallaxr.BENDrWizard",
            ),
            WizardTool(
                id="run_parallaxr_skyrimse",
                label="Run ParallaxR",
                description="Download ParallaxR from Nexus, deploy mods, and process parallax textures.",
                dialog_class_path="wizards.bendr_parallaxr.ParallaxRWizard",
            ),
        ]

    # -----------------------------------------------------------------------
    # Deployment
    # -----------------------------------------------------------------------

    _APPDATA_SUBPATH = Path("drive_c/users/steamuser/AppData/Local/Skyrim Special Edition")
    _APPDATA_SUBPATH_GOG = Path("drive_c/users/steamuser/AppData/Local/Skyrim Special Edition GOG")
    _MYGAMES_SUBPATH = Path("Skyrim Special Edition")
    _MYGAMES_SUBPATH_GOG = Path("Skyrim Special Edition GOG")
    _ARCHIVE_INI_FILENAME = "Skyrim.ini"

    @property
    def _script_extender_exe(self) -> str:
        return "skse64_loader.exe"

    def swap_launcher(self, log_fn) -> None:
        """Replace SkyrimSELauncher.exe with skse64_loader.exe if present."""
        _log = log_fn
        if self._game_path is None:
            return
        if not self._script_extender_swap:
            _log("  Script extender / launcher swap disabled — skipping.")
            return
        skse = self._game_path / "skse64_loader.exe"
        if not skse.is_file():
            _log("  SKSE loader not found — skipping launcher swap.")
            return
        launcher = self._game_path / "SkyrimSELauncher.exe"
        backup   = self._game_path / "SkyrimSELauncher.bak"
        if launcher.is_file():
            launcher.rename(backup)
            _log("  Renamed SkyrimSELauncher.exe → SkyrimSELauncher.bak.")
        shutil.copy2(skse, launcher)
        _log("  Copied skse64_loader.exe → SkyrimSELauncher.exe.")

    def _restore_launcher(self, log_fn) -> None:
        """Reverse the SKSE launcher swap if a backup exists."""
        _log = log_fn
        if self._game_path is None:
            return
        backup   = self._game_path / "SkyrimSELauncher.bak"
        launcher = self._game_path / "SkyrimSELauncher.exe"
        if not backup.is_file():
            return
        if launcher.is_file():
            launcher.unlink()
        backup.rename(launcher)
        _log("  Restored SkyrimSELauncher.exe from .bak.")

    def deploy(self, log_fn=None, mode: LinkMode = LinkMode.HARDLINK,
               profile: str = "default", progress_fn=None) -> None:
        """Deploy staged mods into the game's Data directory.

        Workflow:
          1. Move Data/ → Data_Core/
          2. Transfer mod files listed in filemap.txt into Data/
          3. Fill gaps with vanilla files from Data_Core/
        (Root Folder deployment is handled by the GUI after this returns.)
        """
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir      = self._game_path / "Data"
        filemap       = self.get_effective_filemap_path()
        staging       = self.get_effective_mod_staging_path()

        if not data_dir.is_dir():
            raise RuntimeError(f"Data directory not found: {data_dir}")
        if not filemap.is_file():
            raise RuntimeError(
                f"filemap.txt not found: {filemap}\n"
                "Run 'Build Filemap' before deploying."
            )

        _log("Step 1: Moving Data/ → Data_Core/ ...")
        moved = move_to_core(data_dir, log_fn=_log)
        _log(f"  Moved {moved} file(s) to Data_Core/.")

        profile_dir = self.get_profile_root() / "profiles" / profile
        per_mod_strip = load_per_mod_strip_prefixes(profile_dir)

        custom_rules = self.custom_routing_rules
        custom_exclude: set[str] = set()
        if custom_rules:
            _log("Step 1b: Routing files via custom rules ...")
            custom_exclude = deploy_custom_rules(
                filemap, self._game_path, staging,
                rules=custom_rules,
                mode=mode,
                strip_prefixes=self.mod_folder_strip_prefixes,
                per_mod_strip_prefixes=per_mod_strip,
                log_fn=_log,
                progress_fn=progress_fn,
            )

        _log(f"Step 2: Transferring mod files into Data/ ({mode.name}) ...")
        _sep_deploy = load_separator_deploy_paths(profile_dir)
        _sep_entries = read_modlist(profile_dir / "modlist.txt") if _sep_deploy else []
        per_mod_deploy = expand_separator_deploy_paths(_sep_deploy, _sep_entries) or None
        linked_mod, placed = deploy_filemap(filemap, data_dir, staging,
                                            mode=mode,
                                            strip_prefixes=self.mod_folder_strip_prefixes,
                                            per_mod_strip_prefixes=per_mod_strip,
                                            per_mod_deploy_dirs=per_mod_deploy,
                                            log_fn=_log,
                                            progress_fn=progress_fn,
                                            exclude=custom_exclude or None,
                                            core_dir=data_dir.parent / (data_dir.name + "_Core"))
        _log(f"  Transferred {linked_mod} mod file(s).")

        _log("Step 3: Filling gaps with vanilla files from Data_Core/ ...")
        linked_core = deploy_core(data_dir, placed, mode=mode, log_fn=_log)
        _log(f"  Transferred {linked_core} vanilla file(s).")

        _log("Step 4: Symlinking plugins.txt into Proton prefix ...")
        self._symlink_plugins_txt(profile, _log)

        _log("Step 5: Symlinking profile INI files ...")
        self._symlink_profile_ini_files(profile, _log)

        _log("Step 6: Applying archive invalidation ...")
        self.apply_archive_invalidation(_log)

        _log(
            f"Deploy complete. "
            f"{linked_mod} mod + {linked_core} vanilla "
            f"= {linked_mod + linked_core} total file(s) in Data/."
        )

    def restore(self, log_fn=None, progress_fn=None) -> None:
        """Restore Data/ to its vanilla state."""
        _log = log_fn or (lambda _: None)

        if self._game_path is None:
            raise RuntimeError("Game path is not configured.")

        data_dir      = self._game_path / "Data"
        staging       = self.get_effective_mod_staging_path()
        overwrite_dir = self.get_effective_overwrite_path()

        _log("Restore: removing plugins.txt symlink ...")
        self._remove_plugins_txt_symlink(_log)

        _log("Restore: reverting archive invalidation ...")
        self.revert_archive_invalidation(_log)

        _log("Restore: restoring launcher ...")
        self._restore_launcher(_log)

        _log("Restore: removing profile INI symlinks ...")
        _profile_dir = self._active_profile_dir
        if _profile_dir is not None:
            self._remove_profile_ini_symlinks(_profile_dir.name, _log)

        _profile_dir = self._active_profile_dir
        _entries = read_modlist(_profile_dir / "modlist.txt") if _profile_dir else []
        cleanup_custom_deploy_dirs(_profile_dir, _entries, log_fn=_log)

        custom_rules = self.custom_routing_rules
        if custom_rules and self._game_path:
            _log("Restore: removing custom-routed files ...")
            restore_custom_rules(
                self.get_effective_filemap_path(),
                self._game_path,
                rules=custom_rules,
                log_fn=_log,
            )

        _log("Restore: clearing Data/ and moving Data_Core/ back ...")
        try:
            restored = restore_data_core(
                data_dir,
                overwrite_dir=overwrite_dir,
                staging_root=staging,
                strip_prefixes=self.mod_folder_strip_prefixes,
                log_fn=_log,
            )
            _log(f"  Restored {restored} file(s). Data_Core/ removed.")
        except RuntimeError as e:
            _log(f"  Skipping data restore: {e}")

        _log("Restore complete.")

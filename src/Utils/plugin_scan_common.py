"""
Shared plugin-scan surface used by both the SkyGen and Plugin Audit wizards.

Both wizards resolve the active profile, read its load order, find plugin
files across the staging tree and build the cross-mod BOS/SkyPatcher patch
index.  The concrete implementations live in Utils.skygen_core (the richer
parser) and are re-exported here as the shared surface; Utils.plugin_audit_core
keeps its own copies of the header/new-record scanners it needs on top.
"""

from __future__ import annotations

from Utils.skygen_core import (  # noqa: F401
    BASE_GAME_PLUGINS,
    GLOBAL_IGNORE_PLUGINS,
    _build_patch_index as build_patch_index,
    _find_mod_folder as find_mod_folder,
    _find_plugin_file as find_plugin_file,
    _profile_dir as profile_dir,
    _read_active_profile as read_active_profile,
    _read_loadorder as read_loadorder,
)

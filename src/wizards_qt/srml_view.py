"""Qt view: Install SRML (Slime Rancher Mod Loader).

Port of the Tk ``sr_srml`` plugin.  Download → locate → extract into the game
folder → run ``SRMLInstaller.exe`` via Proton → clean up.
"""

from __future__ import annotations

from wizards_qt._mod_loader_installer_view import ModLoaderInstallerView


class SRMLView(ModLoaderInstallerView):
    TOOL_LABEL = "SRML"
    NEXUS_URL = ("https://www.nexusmods.com/slimerancher/mods/2"
                 "?tab=files&file_id=724")
    ARCHIVE_KEYWORDS = ["srmlinstaller"]
    INSTALLER_EXE = "SRMLInstaller.exe"
    PICK_TITLE = "Select the SRML archive"

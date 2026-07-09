"""Shared startup hardening for cli.py and run_qt.py (AppImage/flatpak aware).

Importing this module before any Utils/Games/gui_qt import works because
Python puts the launched script's directory at sys.path[0].
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def setup_environment() -> None:
    """Ensure src/ is on sys.path so Utils/Games/etc can be imported."""
    # Drop dead /tmp/.mount_* entries from sys.path. Older AppImage builds
    # exported PYTHONPATH globally; a shell launched from the GUI inherits a
    # path pointing at a mount that vanishes the moment the AppImage exits.
    sys.path[:] = [p for p in sys.path if not (p.startswith("/tmp/.mount_") and not Path(p).is_dir())]

    src = Path(__file__).resolve().parent
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    # When running inside the current AppImage, add the bundled _vendor dir
    # so we can find Pillow / other deps. The launcher used to do this via
    # PYTHONPATH, which leaked into child shells.
    if os.environ.get("APPDIR"):
        vendor = Path(os.environ["APPDIR"]) / "share" / "amethyst-mod-manager" / "_vendor"
        if vendor.is_dir() and str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))

    # Drop a stale MOD_MANAGER_GAMES pointing at /tmp/.mount_* (same leak path).
    mmg = os.environ.get("MOD_MANAGER_GAMES", "")
    if mmg.startswith("/tmp/.mount_") and not Path(mmg).is_dir():
        os.environ.pop("MOD_MANAGER_GAMES", None)
    # Set MOD_MANAGER_GAMES so game_loader can find the Games/ directory.
    if not os.environ.get("MOD_MANAGER_GAMES"):
        games_dir = src / "Games"
        if games_dir.is_dir():
            os.environ["MOD_MANAGER_GAMES"] = str(games_dir)

"""
Utils/flatpak_i386.py
Detect and repair missing 32-bit support inside our own Flatpak sandbox.

The Flatpak manifest declares the org.freedesktop.Platform.Compat.i386 (+GL32)
extensions, which provide /lib/ld-linux.so.2 — the 32-bit ELF interpreter
Proton's wine needs when it boots a prefix (syswow64 processes are 32-bit even
for 64-bit games/tools). Flathub installs pull those related refs in
automatically, but installs from a .flatpak *bundle* (our release zip,
Warehouse, `flatpak install file.flatpak`) do NOT — the extension point stays
empty and the runtime's /lib/ld-linux.so.2 symlink dangles. Every in-sandbox
Proton/wine run then dies with "/lib/ld-linux.so.2: could not open".

Two repair surfaces:
  * ``preflight_i386_error`` — called before an in-sandbox Proton run so the
    user gets an actionable message instead of the cryptic loader error.
  * ``install_i386_extensions`` — installs the refs on the host via
    flatpak-spawn (the app startup self-heal in gui_qt.app uses this).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

from Utils.app_log import safe_log as _safe_log

# Extension refs the sandbox needs for 32-bit support. The branch MUST match
# the freedesktop base of the runtime in flatpak/io.github.Amethyst.ModManager.yml
# (KDE 6.9 → freedesktop 24.08) — keep in sync with the manifest's
# add-extensions block.
I386_EXTENSION_REFS = (
    "org.freedesktop.Platform.Compat.i386//24.08",
    "org.freedesktop.Platform.GL32.default//24.08",
)

# One-line manual fix, used in error messages and docs.
MANUAL_INSTALL_CMD = "flatpak install --user flathub " + " ".join(I386_EXTENSION_REFS)


def _in_flatpak_sandbox() -> bool:
    return os.path.exists("/.flatpak-info")


def i386_support_missing() -> bool:
    """True when we run inside our Flatpak sandbox and the 32-bit loader is
    absent (the Compat.i386 extension isn't installed/mounted).

    /lib/ld-linux.so.2 is a runtime symlink into the extension mount point
    (/app/lib/i386-linux-gnu); ``os.path.exists`` follows it, so a dangling
    link correctly reads as missing. Outside Flatpak this always returns
    False — native/AppImage hosts manage their own 32-bit userland.
    """
    return _in_flatpak_sandbox() and not os.path.exists("/lib/ld-linux.so.2")


def i386_error_message() -> str:
    """Actionable message for a missing-32-bit-support failure."""
    return (
        "32-bit support is missing from the Flatpak sandbox, so wine cannot "
        "start (/lib/ld-linux.so.2 is not available). Install it with: "
        + MANUAL_INSTALL_CMD + " — then restart the app."
    )


def preflight_i386_error(proton_script) -> "str | None":
    """Return an actionable error when running *proton_script* would exec wine
    inside our Flatpak sandbox without 32-bit support, else None.

    Mirrors proton_run_command's dispatch: bare wine binaries (Lutris) and
    Steam-Flatpak Protons are forwarded to the host / Steam's own sandbox,
    which carry their own 32-bit runtimes — only the in-sandbox exec path
    needs the Compat.i386 extension.
    """
    if not i386_support_missing():
        return None
    from pathlib import Path
    from Utils.steam_finder import (
        _proton_script_in_steam_flatpak, _own_process_in_steam_flatpak,
    )
    script = Path(proton_script)
    if script.name in ("wine", "wine64"):
        return None  # runs on the host via flatpak-spawn
    if _proton_script_in_steam_flatpak(script) and not _own_process_in_steam_flatpak():
        return None  # runs inside Steam's own sandbox
    return i386_error_message()


def install_i386_extensions(log_fn: "Callable[[str], None] | None" = None) -> bool:
    """Install the 32-bit compat extension refs from Flathub (user install).

    Runs ``flatpak install`` on the host via flatpak-spawn (the sandbox has no
    flatpak CLI; the manifest grants --talk-name=org.freedesktop.Flatpak).
    A --user install satisfies the extension point regardless of whether the
    app itself is a user or system install. Returns True when the install
    succeeds — the extensions only MOUNT on the next app launch, so callers
    should tell the user to restart.
    """
    _log = _safe_log(log_fn)
    if not _in_flatpak_sandbox():
        _log("i386 extensions: not running inside a Flatpak sandbox — nothing to do.")
        return False
    if shutil.which("flatpak-spawn") is None:
        _log("i386 extensions: flatpak-spawn is unavailable — install manually: "
             + MANUAL_INSTALL_CMD)
        return False
    cmd = [
        "flatpak-spawn", "--host", "--directory=/",
        "flatpak", "install", "--user", "--noninteractive", "flathub",
        *I386_EXTENSION_REFS,
    ]
    _log("i386 extensions: installing " + " ".join(I386_EXTENSION_REFS) + " …")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as exc:
        _log(f"i386 extensions: install failed to run: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        _log("i386 extensions: install failed"
             + (f" — {detail[-1]}" if detail else "")
             + f". Install manually: {MANUAL_INSTALL_CMD}")
        return False
    _log("i386 extensions: installed — they mount on the next app launch.")
    return True

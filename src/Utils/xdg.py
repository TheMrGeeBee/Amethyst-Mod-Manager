"""
Utils/xdg.py
Helpers for launching host-system programs (xdg-open etc.) safely from
a polluted shell environment.

Inside an AppImage, anylinux.so (LD_PRELOAD-injected by quick-sharun) hooks
execve and scrubs AppDir-pointing env vars from child processes — so we
don't need to do anything special there. sharun also doesn't use
LD_LIBRARY_PATH; it invokes the dynamic linker with --library-path.

host_env() therefore only protects against pollution from *outside* the
AppImage: conda/pyenv/Steam-runtime can leave LD_LIBRARY_PATH pointing at
incompatible libraries, which would break xdg-open or Dolphin.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

from Utils.app_log import app_log


# Env vars the AppImage runtime / sharun / our own launcher inject. These all
# either point at /tmp/.mount_* (which disappears the moment the AppImage
# exits) or are otherwise meaningful only inside the AppImage's own python.
# Carrying them into a child process — especially a long-lived terminal the
# user might later run `python3` from — turns into "ImportError: cannot
# import name '_imaging' from 'PIL'" after the mount is gone.
_APPIMAGE_LEAK_VARS = (
    "APPDIR", "APPIMAGE", "ARGV0", "ARG0", "OWD", "URUNTIME",
    "APPIMAGE_ARCH", "APPIMAGE_UUID",
    "SHARUN_DIR", "SHARUN_WORKING_DIR", "SHARUN_ALLOW_SYS_VKICD",
    "PYTHONPATH", "PYTHONHOME", "PYTHONDONTWRITEBYTECODE",
    "MOD_MANAGER_GAMES",  # app_bootstrap auto-points this at $APPDIR/.../Games
    "GIO_LAUNCH_DESKTOP",
    "GDK_PIXBUF_MODULEDIR", "GDK_PIXBUF_MODULE_FILE",
    "GIO_MODULE_DIR",
    "GSETTINGS_SCHEMA_DIR",
    "GTK_PATH", "GTK_IM_MODULE_FILE",
    "QT_PLUGIN_PATH",
    "TERMINFO", "LIBTHAI_DICTDIR",
    "PERLLIB", "PERL5LIB",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE",
    "LD_LIBRARY_PATH", "LD_PRELOAD",
)


def _strip_appimage_path_entries(value: str) -> str:
    """Drop colon-separated entries that point at /tmp/.mount_*."""
    if not value:
        return value
    parts = [p for p in value.split(":") if p and not p.startswith("/tmp/.mount_")]
    return ":".join(parts)


def host_env() -> dict[str, str]:
    """Return os.environ scrubbed of AppImage-injected pollution.

    Inside an AppImage, anylinux.so (LD_PRELOAD'd by quick-sharun) already
    drops some AppDir-pointing vars on execve, but it doesn't know about our
    custom ones (MOD_MANAGER_GAMES) or about /tmp/.mount_* fragments inside
    PATH / XDG_DATA_DIRS. So we strip them here too.

    Outside an AppImage this also defends against stale env in shells the
    user opened *from* a previous AppImage launch — `$PATH` still has
    `/tmp/.mount_<dead>/bin` in it, etc.
    """
    env = os.environ.copy()
    for k in _APPIMAGE_LEAK_VARS:
        env.pop(k, None)
    # Strip /tmp/.mount_* entries from list-style vars rather than unsetting
    # them outright — they may still hold useful host paths.
    for k in ("PATH", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS"):
        if k in env:
            cleaned = _strip_appimage_path_entries(env[k])
            if cleaned:
                env[k] = cleaned
            else:
                env.pop(k, None)
    return env


def _in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def xdg_download_dir() -> Path:
    """Return the user's Downloads directory.

    Desktops record localised user dirs ("Téléchargements", "Descargas", …)
    in ~/.config/user-dirs.dirs but rarely export XDG_DOWNLOAD_DIR into the
    environment, so check the env var first, then parse the file, then fall
    back to ~/Downloads.
    """
    env = os.environ.get("XDG_DOWNLOAD_DIR")
    if env:
        return Path(env)
    home = Path.home()
    cfg_base = os.environ.get("XDG_CONFIG_HOME") or (home / ".config")
    try:
        for line in (Path(cfg_base) / "user-dirs.dirs").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("XDG_DOWNLOAD_DIR="):
                continue
            raw = line.split("=", 1)[1].strip().strip('"')
            raw = raw.replace("$HOME", str(home))
            # A value of just "$HOME/" means the dir is disabled per the
            # xdg-user-dirs spec — use the fallback instead.
            if raw and Path(raw) != home:
                return Path(raw)
            break
    except (OSError, UnicodeDecodeError):
        pass
    return home / "Downloads"


def spawn_watched(
    cmd: list[str],
    label: str,
    log_fn: Callable[[str], None] | None,
    on_fail: Callable[[], None] | None = None,
) -> None:
    """Run *cmd* in the background, log non-zero exits, optionally chain a fallback.

    Public so other launchers (Utils/exe_launch.launch_via_steam) can reuse the
    Flatpak-safe CWD handling and exit-code watching instead of calling
    ``subprocess.Popen`` directly — a bare Popen of ``flatpak-spawn --host …``
    succeeds even when the *host* command it forwards to is missing or fails,
    which silently swallows launch errors.
    """
    # Use a CWD the host definitely has. Inside Flatpak the sandbox CWD
    # (e.g. /app/share/amethyst-mod-manager) doesn't exist on the host, so
    # `flatpak-spawn --host` inherits it and the spawned host process fails
    # to start with "Failed to change to directory".
    cwd = os.path.expanduser("~") if os.path.isdir(os.path.expanduser("~")) else "/"
    try:
        proc = subprocess.Popen(
            cmd,
            env=host_env(),
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        msg = f"{label}: {cmd[0]} not found ({exc})"
        app_log(msg)
        if log_fn:
            log_fn(msg)
        if on_fail:
            on_fail()
        return

    def _watch() -> None:
        _, err = proc.communicate()
        rc = proc.returncode
        if rc != 0:
            text = err.decode(errors="replace").strip() or "(no output)"
            msg = f"{label}: rc={rc} {text}"
            app_log(msg)
            if log_fn:
                log_fn(msg)
            if on_fail:
                on_fail()

    threading.Thread(target=_watch, daemon=True).start()


def xdg_open(path: str | Path, log_fn: Callable[[str], None] | None = None) -> None:
    """Open *path* with the user's default application via xdg-open.

    Uses host_env() so that the launched application (e.g. Dolphin) loads
    its own system libraries. Failures are logged to app_log (always) and
    log_fn (if provided), so they don't disappear silently.

    Inside a Flatpak sandbox the runtime's xdg-open usually can't resolve
    host MIME associations (or lacks the target app entirely), so we route
    through ``flatpak-spawn --host`` when available. Fall back to bare
    xdg-open if flatpak-spawn isn't usable.
    """
    target = str(path)
    if _in_flatpak() and shutil.which("flatpak-spawn"):
        cmd = ["flatpak-spawn", "--host", "xdg-open", target]
    else:
        cmd = ["xdg-open", target]
    spawn_watched(cmd, f"xdg-open {target!r}", log_fn)


def open_url(url: str, log_fn: Callable[[str], None] | None = None) -> None:
    """Open *url* in the user's default browser.

    Inside a Flatpak sandbox `xdg-open` from the runtime usually can't reach
    the host's browser. Try, in order:
      1. `flatpak-spawn --host xdg-open <url>` — runs xdg-open on the host.
      2. `gio open <url>` — uses the OpenURI portal from inside the sandbox.
      3. bare `xdg-open <url>` — last resort.
    Each step's failure is logged and triggers the next.
    """
    if not _in_flatpak():
        spawn_watched(["xdg-open", url], f"xdg-open {url!r}", log_fn)
        return

    def try_gio() -> None:
        if shutil.which("gio"):
            spawn_watched(["gio", "open", url], f"gio open {url!r}", log_fn,
                           on_fail=try_xdg)
        else:
            try_xdg()

    def try_xdg() -> None:
        if shutil.which("xdg-open"):
            spawn_watched(["xdg-open", url], f"xdg-open {url!r}", log_fn)
        else:
            msg = f"open_url: no working launcher for {url!r}"
            app_log(msg)
            if log_fn:
                log_fn(msg)

    if shutil.which("flatpak-spawn"):
        spawn_watched(
            ["flatpak-spawn", "--host", "xdg-open", url],
            f"flatpak-spawn xdg-open {url!r}",
            log_fn,
            on_fail=try_gio,
        )
    else:
        try_gio()

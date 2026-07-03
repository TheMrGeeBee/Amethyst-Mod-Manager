"""Neutral (GUI-free) helpers for the SMAPI install wizard.

Extracted from the Tk ``sdv_smapi`` plugin so the download / extraction /
terminal-launch logic can be reused by the Qt view and unit-tested without a
GUI toolkit.  No tkinter or Qt imports here.

Pipeline:
  * ``fetch_latest_smapi_asset()`` — latest installer-zip URL from GitHub.
  * ``download_smapi(url, dest, reporthook)`` — HTTPS download via the app CA
    bundle.
  * ``run_smapi_installer(archive, log_fn)`` — extract under ~/.cache, mark the
    installer executable, build a bash wrapper, detect a terminal emulator
    (host / flatpak-spawn / generic), run it, then clean up the temp dir.
"""

from __future__ import annotations

import json as _json
import os
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

LogFn = Callable[[str], None]

_GITHUB_API_URL = "https://api.github.com/repos/Pathoschild/SMAPI/releases/latest"


def _noop(_msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Fetch + download
# ---------------------------------------------------------------------------

def fetch_latest_smapi_asset() -> tuple[str, str]:
    """Return (version_tag, download_url) for the latest SMAPI installer zip."""
    req = urllib.request.Request(
        _GITHUB_API_URL,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "ModManager/1.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = _json.loads(resp.read().decode())
    tag = data.get("tag_name", "unknown")
    assets = data.get("assets", [])
    for asset in assets:
        nl = asset.get("name", "").lower()
        if (nl.endswith(".zip") and "smapi" in nl and "installer" in nl
                and "double" not in nl):
            return tag, asset["browser_download_url"]
    for asset in assets:
        nl = asset.get("name", "").lower()
        if nl.endswith(".zip") and "smapi" in nl and "double" not in nl:
            return tag, asset["browser_download_url"]
    raise RuntimeError("No SMAPI installer zip found in the latest GitHub release.")


def download_smapi(url: str, dest: Path, reporthook=None) -> None:
    """Download *url* to *dest* over HTTPS using the app's resolved CA bundle."""
    from Utils.ca_bundle import download_file
    download_file(url, dest, reporthook=reporthook)


# ---------------------------------------------------------------------------
# Extraction + wrapper
# ---------------------------------------------------------------------------

def _extract_zip(archive: Path, dest: Path) -> None:
    if archive.name.lower().endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(dest)
    else:
        raise RuntimeError(f"Unsupported archive format: {archive.name}")


def _chmod_exec(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode
                   | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def build_wrapper(script: Path, wrapper_dir: Path) -> Path:
    """Create a bash wrapper that cd's to the installer's folder, runs it, then
    pauses so the user can read its output."""
    wrapper = wrapper_dir / "run_smapi_install.sh"
    # Escape single quotes for bash single-quoted strings.
    script_dir = str(script.parent).replace("'", "'\\''")
    script_name = script.name.replace("'", "'\\''")
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f"cd '{script_dir}' || {{ echo 'Failed to cd into installer folder'; "
        "read -n 1; exit 1; }\n"
        f"./'{script_name}'\n"
        "rc=$?\n"
        "echo\n"
        "echo '---- SMAPI installer finished (exit code '$rc') ----'\n"
        "echo 'Press any key to close this window...'\n"
        "read -n 1 -s\n"
        "exit $rc\n",
        encoding="utf-8",
    )
    _chmod_exec(wrapper)
    return wrapper


# ---------------------------------------------------------------------------
# Terminal detection
# ---------------------------------------------------------------------------

def _terminal_candidates(wrapper: str) -> list[tuple[str, list[str]]]:
    return [
        ("konsole",        ["konsole", "--hold", "-e", "bash", wrapper]),
        ("alacritty",      ["alacritty", "-e", "bash", wrapper]),
        ("gnome-terminal", ["gnome-terminal", "--wait", "--", "bash", wrapper]),
        ("xfce4-terminal", ["xfce4-terminal", "--hold", "-e", f"bash {wrapper}"]),
        ("kitty",          ["kitty", "--hold", "bash", wrapper]),
        ("ptyxis",         ["ptyxis", "--new-window", "--", "bash", wrapper]),
        ("xterm",          ["xterm", "-hold", "-e", "bash", wrapper]),
    ]


def clean_env() -> dict:
    """Copy of os.environ with AppImage / bundle vars removed.

    AppImage exports LD_LIBRARY_PATH, QT_PLUGIN_PATH, PYTHONHOME etc. pointing
    into its bundle. Inherited by konsole, those make it load the wrong Qt libs
    and exit immediately. Strip them so the terminal uses host libraries.
    """
    env = os.environ.copy()
    for var in (
        "LD_LIBRARY_PATH", "LD_PRELOAD",
        "PYTHONHOME", "PYTHONPATH",
        "QT_PLUGIN_PATH", "QML2_IMPORT_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
        "GTK_PATH", "GIO_MODULE_DIR", "GSETTINGS_SCHEMA_DIR",
        "GDK_PIXBUF_MODULE_FILE", "GDK_PIXBUF_MODULEDIR",
        "XDG_DATA_DIRS_APPIMAGE", "PERLLIB", "GCONV_PATH",
        "APPDIR", "APPIMAGE", "ARGV0", "OWD",
    ):
        env.pop(var, None)
    env.setdefault("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    return env


def _spawn_host_prefix() -> list[str]:
    """flatpak-spawn invocation that starts on the host with a safe cwd.

    Inside a flatpak the sandbox cwd doesn't exist on the host, so the portal
    call fails to change directory; passing --directory=$HOME avoids it.
    """
    home = os.environ.get("HOME") or "/tmp"
    return ["flatpak-spawn", "--host", f"--directory={home}"]


def _host_has(exe: str, env: dict) -> bool:
    """Ask the host (via flatpak-spawn) whether *exe* is on its PATH."""
    try:
        r = subprocess.run(
            _spawn_host_prefix() + ["sh", "-c", f"command -v {exe}"],
            env=env, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def find_terminal_cmd(wrapper: str, log_fn: LogFn = _noop
                      ) -> tuple[list[str], dict] | None:
    """Return (argv, env) for a terminal that actually launches, or None.

    Order of preference:
      1. Host-side terminal that passes ``<bin> --version`` under a cleaned env
         (catches AppImage library-conflict crashes).
      2. Host terminal via flatpak-spawn --host (flatpak builds) — selected by
         asking the host ``command -v <exe>``, not by probing.
      3. x-terminal-emulator / xdg-terminal-exec.
      4. Forced direct launch of a host terminal on PATH even if it failed the
         probe.
      5. Forced flatpak-spawn of a host terminal without host-check.
    """
    env = clean_env()
    have_spawn = shutil.which("flatpak-spawn") is not None

    def _probe_host(exe: str) -> bool:
        try:
            r = subprocess.run([exe, "--version"], env=env,
                               capture_output=True, text=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            log_fn(f"SMAPI Wizard: probe {exe} failed: {exc}")
            return False
        if r.returncode != 0:
            log_fn(f"SMAPI Wizard: probe {exe} rc={r.returncode} "
                   f"stderr={r.stderr.strip()[:200]}")
            return False
        return True

    candidates = _terminal_candidates(wrapper)

    # 1. Direct host invocation with a working probe.
    for exe, argv in candidates:
        if shutil.which(exe) and _probe_host(exe):
            log_fn(f"SMAPI Wizard: selected host terminal: {exe}")
            return argv, env

    # 2. flatpak-spawn --host, selecting by `command -v` on the host.
    if have_spawn:
        for exe, argv in candidates:
            if _host_has(exe, env):
                log_fn(f"SMAPI Wizard: selected host terminal via flatpak-spawn: {exe}")
                return _spawn_host_prefix() + argv, env

    # 3. Generic terminal wrappers.
    for generic in ("x-terminal-emulator", "xdg-terminal-exec"):
        if shutil.which(generic):
            log_fn(f"SMAPI Wizard: falling back to {generic}")
            return [generic, "-e", "bash", wrapper], env

    # 4. Host terminal on PATH that failed probe — force it.
    for exe, argv in candidates:
        if shutil.which(exe):
            log_fn(f"SMAPI Wizard: no terminal passed probe, forcing {exe}")
            return argv, env

    # 5. Last-ditch for flatpak: try flatpak-spawn --host anyway.
    if have_spawn:
        for exe, argv in candidates:
            log_fn(f"SMAPI Wizard: last-ditch flatpak-spawn --host {exe}")
            return _spawn_host_prefix() + argv, env

    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_smapi_installer(archive: Path, log_fn: LogFn = _noop) -> None:
    """Extract *archive*, mark the installer executable, launch it in a
    terminal and wait for it to close.  Deletes the archive on success and
    always removes the temp extraction dir.  Raises on failure."""
    tmp_dir: Path | None = None
    try:
        if archive is None or not archive.is_file():
            raise RuntimeError("Archive not found.")

        log_fn(f"SMAPI Wizard: extracting {archive.name}")
        # Extract under ~/.cache, not /tmp: inside a flatpak /tmp is a private
        # sandbox mount the host can't see, so flatpak-spawn --host bash
        # /tmp/... fails. ~/.cache is shared (--filesystem=home).
        cache_root = Path.home() / ".cache" / "amethyst-smapi"
        cache_root.mkdir(parents=True, exist_ok=True)
        tmp_dir = Path(tempfile.mkdtemp(prefix="smapi_install_",
                                        dir=str(cache_root)))
        _extract_zip(archive, tmp_dir)

        script: Path | None = None
        for candidate in tmp_dir.rglob("install on Linux.sh"):
            script = candidate
            break
        if script is None:
            raise RuntimeError('Could not find "install on Linux.sh" inside '
                               'the archive.')

        _chmod_exec(script)
        installer_bin = script.parent / "internal" / "linux" / "SMAPI.Installer"
        if installer_bin.is_file():
            _chmod_exec(installer_bin)
        for p in script.parent.rglob("*"):
            if p.is_file() and (p.suffix == ".sh" or "Installer" in p.name):
                _chmod_exec(p)

        wrapper = build_wrapper(script, tmp_dir)
        log_fn("SMAPI Wizard: launching SMAPI installer in terminal")

        result = find_terminal_cmd(str(wrapper), log_fn=log_fn)
        if result is None:
            raise RuntimeError(
                "No terminal emulator found (tried konsole, alacritty, "
                "gnome-terminal, xfce4-terminal, kitty, ptyxis, xterm). "
                f"Please run the installer manually:\n  {wrapper}")
        terminal_cmd, term_env = result

        log_fn(f"SMAPI Wizard: terminal cmd: {' '.join(terminal_cmd)}")
        proc = subprocess.run(terminal_cmd, cwd=str(script.parent),
                              env=term_env, capture_output=True, text=True)
        if proc.returncode != 0:
            log_fn(f"SMAPI Wizard: terminal exited with code {proc.returncode}")
            stderr = (proc.stderr or "").strip()
            if stderr:
                log_fn(f"SMAPI Wizard: terminal stderr: {stderr[:500]}")

        log_fn("SMAPI Wizard: SMAPI installer completed.")
        try:
            archive.unlink()
            log_fn(f"SMAPI Wizard: deleted {archive.name} from Downloads.")
        except OSError as exc:
            log_fn(f"SMAPI Wizard: could not delete archive: {exc}")
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

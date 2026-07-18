"""
App update check: fetch latest version from repo and compare.
Used by the app shell. No dependency on any gui modules.
"""

import os
import re
import subprocess

from Utils.gh_cache import fetch_text as _gh_fetch_text

_APP_UPDATE_RELEASES_API_URL = "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/releases/latest"
_APP_UPDATE_RELEASES_LIST_API_URL = "https://api.github.com/repos/ChrisDKN/Amethyst-Mod-Manager/releases?per_page=20"
_APP_UPDATE_RELEASES_URL = "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases"
_APP_UPDATE_INSTALLER_URL = "https://raw.githubusercontent.com/ChrisDKN/Amethyst-Mod-Manager/main/src/appimage/Amethyst-MM-installer.sh"
_APP_UPDATE_FLATPAK_BUNDLE_URL = (
    "https://github.com/ChrisDKN/Amethyst-Mod-Manager/releases/download/"
    "v{tag}/AmethystModManager.flatpak"
)
_APP_ID = "io.github.Amethyst.ModManager"

# Hosted Flatpak remote (GitHub Pages). Adding this remote lets the OS handle
# updates natively (`flatpak update`, GNOME Software, Discover) with delta
# downloads. `stable` and `beta` are the two OSTree branches published to it.
_FLATPAK_REMOTE_NAME = "amethyst"
_FLATPAK_REMOTE_REPO_URL = "https://chrisdkn.github.io/Amethyst-Mod-Manager/repo/"
_FLATPAK_REMOTE_FILE_URL = (
    "https://chrisdkn.github.io/Amethyst-Mod-Manager/amethyst.flatpakrepo"
)

_AUR_API_URL = "https://aur.archlinux.org/rpc/v5/info/amethyst-mod-manager"
_AUR_PACKAGE_URL = "https://aur.archlinux.org/packages/amethyst-mod-manager"


def is_appimage() -> bool:
    """Return True if we are running inside an AppImage."""
    return bool(os.environ.get("APPIMAGE"))


def is_flatpak() -> bool:
    """Return True if we are running as the Amethyst flatpak.

    NB: match FLATPAK_ID against OUR id, not "/.flatpak-info exists" —
    running from source inside another flatpak (e.g. a flatpak VS Code
    terminal) sandboxes us under that host app, so the file test would
    wrongly steer from-source sessions to the flatpak update path.
    """
    return os.environ.get("FLATPAK_ID") == "io.github.Amethyst.ModManager"


def _parse_version(s: str) -> tuple:
    """Parse a version string into a sortable tuple following SemVer pre-release rules.

    '1.3.1'         -> ((1, 3, 1), (1,))                # stable sorts last
    '1.3.1-beta.1'  -> ((1, 3, 1), (0, 'beta', 1))      # pre-release sorts before stable
    """
    s = s.strip().lstrip("v")
    if "-" in s:
        core, pre = s.split("-", 1)
    else:
        core, pre = s, ""
    nums = []
    for part in core.split("."):
        part = re.sub(r"[^0-9].*$", "", part)
        nums.append(int(part) if part.isdigit() else 0)
    if not pre:
        return (tuple(nums), (1,))
    pre_key: list = []
    for part in pre.split("."):
        pre_key.append(int(part) if part.isdigit() else part)
    return (tuple(nums), (0, *pre_key))


def _fetch_latest_version(
    allow_prerelease: bool = False,
    *,
    force: bool = False,
) -> tuple[str, bool] | None:
    """Return (tag, is_prerelease) of the highest applicable release, or None on error.

    With allow_prerelease=False, queries /releases/latest (stable-only).
    With allow_prerelease=True, lists recent releases and picks the highest non-draft
    by SemVer comparison — which may be either a stable or a pre-release.

    Uses ETag caching + a 1-hour throttle. Pass force=True to bypass the
    throttle (e.g. when the user manually toggles the pre-release channel).
    """
    import json
    try:
        if not allow_prerelease:
            raw = _gh_fetch_text(
                _APP_UPDATE_RELEASES_API_URL,
                timeout=10,
                min_interval=3600,
                force=force,
            )
            if raw is None:
                return None
            data = json.loads(raw)
            tag = data.get("tag_name", "").lstrip("v")
            return (tag, False) if tag else None

        raw = _gh_fetch_text(
            _APP_UPDATE_RELEASES_LIST_API_URL,
            timeout=10,
            min_interval=3600,
            force=force,
        )
        if raw is None:
            return None
        releases = json.loads(raw)
        candidates = [
            (r.get("tag_name", "").lstrip("v"), bool(r.get("prerelease", False)))
            for r in releases
            if not r.get("draft", False) and r.get("tag_name")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda tp: _parse_version(tp[0]), reverse=True)
        return candidates[0]
    except Exception:
        return None


def _fetch_aur_version(*, force: bool = False) -> str | None:
    """Fetch the current AUR package version; return None on error.

    The AUR version string includes a pkgrel suffix (e.g. '0.7.9-1').
    We strip everything from the first '-' onwards so callers get a plain
    version number comparable with __version__.

    Uses ETag caching + a 1-hour throttle (AUR supports conditional GETs too).
    """
    import json
    try:
        raw = _gh_fetch_text(
            _AUR_API_URL,
            accept="application/json",
            timeout=10,
            min_interval=3600,
            force=force,
        )
        if raw is None:
            return None
        data = json.loads(raw)
        results = data.get("results", [])
        if not results:
            return None
        ver = results[0].get("Version", "")
        # Strip pkgrel: '0.7.9-1' -> '0.7.9'
        ver = ver.split("-")[0]
        return ver if ver else None
    except Exception:
        return None


def _is_newer_version(current: str, latest: str) -> bool:
    """Return True if latest is newer than current (strictly greater)."""
    try:
        return _parse_version(latest) > _parse_version(current)
    except (ValueError, TypeError):
        return False


def _major_minor(s: str) -> tuple[int, int] | None:
    """Parse a version string and return (major, minor). Beta/pre-release suffix is ignored.

    '1.3'           -> (1, 3)
    '1.3.0'         -> (1, 3)
    '1.3.0-beta.3'  -> (1, 3)
    """
    if not s:
        return None
    try:
        core = s.strip().lstrip("v").split("-", 1)[0]
        parts = core.split(".")
        if len(parts) < 2:
            return None
        return (int(parts[0]), int(parts[1]))
    except (ValueError, AttributeError):
        return None


def _meets_min_app_version(min_ver: str, app_ver: str) -> bool:
    """Return True if app_ver satisfies a major.minor floor of min_ver.

    Beta builds satisfy the floor for their major.minor (e.g. 1.3.0-beta.2
    satisfies "1.3"). An empty/missing min_ver always returns True.
    """
    if not min_ver:
        return True
    floor = _major_minor(min_ver)
    have = _major_minor(app_ver)
    if floor is None or have is None:
        return True  # malformed → don't block
    return have >= floor


def run_installer(allow_prerelease: bool = False):
    """Run the AppImage installer in a detached subprocess.

    The AppImage runtime sets SSL_CERT_FILE / CURL_CA_BUNDLE to a path inside
    its own mount point.  That mount is gone once the app exits, so curl would
    fail with a certificate error.  We scrub those variables (and any other
    AppImage-injected ones) from the child environment before launching.
    Output is logged to $XDG_CONFIG_HOME/amethyst-update.log for debugging.
    sleep 2 gives the app time to fully exit before the installer overwrites
    the running AppImage.
    """
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "AmethystModManager",
    )
    os.makedirs(config_dir, exist_ok=True)
    log_path = os.path.join(config_dir, "amethyst-update.log")
    installer_args = " --prerelease" if allow_prerelease else ""
    cmd = (
        f"sleep 2 && "
        f"SCRIPT=$(mktemp /tmp/amethyst-installer-XXXXXX.sh) && "
        f"curl -sSL {_APP_UPDATE_INSTALLER_URL} -o \"$SCRIPT\" && "
        f"chmod +x \"$SCRIPT\" && "
        f"bash \"$SCRIPT\"{installer_args} && "
        f"rm -f \"$SCRIPT\" && "
        f"nohup \"$HOME/Applications/AmethystModManager-x86_64.AppImage\" &>/dev/null &"
    )

    # Build a clean environment: start from the current env then strip every
    # variable that the AppImage runtime injects and that would be invalid once
    # the mount is gone.
    _APPIMAGE_ENV_PREFIXES = (
        "APPDIR", "APPIMAGE", "OWD",
        "SSL_CERT_FILE", "SSL_CERT_DIR",
        "CURL_CA_BUNDLE",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PYTHONHOME", "PYTHONPATH",
        "GDK_PIXBUF_MODULEDIR", "GDK_PIXBUF_MODULE_FILE",
        "GIO_MODULE_DIR",
        "GSETTINGS_SCHEMA_DIR",
        "GTK_PATH", "GTK_IM_MODULE_FILE",
        "QT_PLUGIN_PATH",
        "PERLLIB", "PERL5LIB",
    )
    clean_env = {
        k: v for k, v in os.environ.items()
        if not any(k.startswith(p) for p in _APPIMAGE_ENV_PREFIXES)
    }

    try:
        subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=open(log_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=clean_env,
        )
    except Exception:
        pass


def run_flatpak_installer(latest_tag: str) -> bool:
    """Download the latest .flatpak bundle and reinstall it on the host.

    The AppImage path replaces the running binary in-place; a Flatpak can't do
    that from inside its own sandbox, so we forward the install to the host's
    ``flatpak`` CLI via ``flatpak-spawn --host`` (our manifest grants
    ``--talk-name=org.freedesktop.Flatpak``, which is what makes this reachable).

    Flow, run detached so it survives our own shutdown:
      1. curl the release's ``AmethystModManager.flatpak`` bundle to a temp file.
      2. ``flatpak install --user --bundle --reinstall -y`` it on the host.
      3. relaunch ``flatpak run <app-id>`` and clean up the temp bundle.

    Output is logged to $XDG_CONFIG_HOME/amethyst-update.log (same as AppImage;
    under flatpak XDG_CONFIG_HOME is redirected into ~/.var/app/<id>/config).
    A ``sleep 2`` lets us exit first. Returns True if the child launched.

    NB: the bundle is stamped without a tag, so the download URL carries the
    version. ``latest_tag`` is the release tag (with or without a leading 'v').
    """
    import shutil

    if not shutil.which("flatpak-spawn"):
        return False

    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "AmethystModManager",
    )
    os.makedirs(config_dir, exist_ok=True)
    log_path = os.path.join(config_dir, "amethyst-update.log")

    tag = latest_tag.lstrip("v")
    bundle_url = _APP_UPDATE_FLATPAK_BUNDLE_URL.format(tag=tag)

    # The temp bundle must live somewhere the HOST flatpak can read. ~/Downloads
    # is inside our --filesystem=home grant AND visible to the host, so it works
    # from both sides; /tmp is sandbox-private and unreadable to the host.
    dl_dir = os.path.join(os.path.expanduser("~"), "Downloads")
    try:
        os.makedirs(dl_dir, exist_ok=True)
    except Exception:
        dl_dir = os.path.expanduser("~")
    bundle_path = os.path.join(dl_dir, "AmethystModManager.update.flatpak")

    # curl runs in-sandbox (network is granted); install/run go to the host.
    host = "flatpak-spawn --host"
    cmd = (
        f"sleep 2 && "
        f"curl -fsSL {bundle_url} -o {bundle_path!r} && "
        f"{host} flatpak install --user --bundle --reinstall --noninteractive -y "
        f"{bundle_path!r} && "
        f"rm -f {bundle_path!r} && "
        f"{host} flatpak run {_APP_ID} &>/dev/null &"
    )

    try:
        subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=open(log_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


# ── Hosted-remote update path (preferred over the bundle download) ──────────
#
# Once the app is installed from our GitHub Pages remote, updates are the OS's
# job: `flatpak update` pulls only changed OSTree objects (delta), and GNOME
# Software / Discover surface the update natively. These helpers (a) tell
# whether we're already tracking the remote, (b) enrol a bundle-installed user
# onto it, and (c) trigger an update. All host calls go via `flatpak-spawn
# --host` — our manifest grants `--talk-name=org.freedesktop.Flatpak`.


def _host_flatpak(*args: str, timeout: int = 60):
    """Run `flatpak <args>` on the host, returning CompletedProcess or None.

    Uses flatpak-spawn --host (we're sandboxed). Returns None if flatpak-spawn
    is unavailable or the call raises, so callers can treat that as "unknown".
    """
    import shutil
    if not shutil.which("flatpak-spawn"):
        return None
    try:
        return subprocess.run(
            ["flatpak-spawn", "--host", "flatpak", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None


def flatpak_installed_from_remote() -> bool:
    """True if our flatpak install tracks the `amethyst` remote (not a bundle).

    `flatpak info --show-origin <app>` prints the origin remote name for a
    remote-tracked install, or reports no origin / errors for a bundle install.
    Conservatively returns False when the host can't be queried.
    """
    cp = _host_flatpak("info", "--show-origin", _APP_ID)
    if cp is None or cp.returncode != 0:
        return False
    return cp.stdout.strip() == _FLATPAK_REMOTE_NAME


def flatpak_remote_present() -> bool:
    """True if the `amethyst` remote is already configured on the host."""
    cp = _host_flatpak("remotes", "--columns=name")
    if cp is None or cp.returncode != 0:
        return False
    return any(line.strip() == _FLATPAK_REMOTE_NAME
               for line in cp.stdout.splitlines())


def enroll_flatpak_remote(*, allow_prerelease: bool = False) -> bool:
    """Add the hosted remote and reinstall the app from it (detached).

    This is the one-time migration for bundle-installed users. After it, all
    future updates are native `flatpak update`. Adds the remote (idempotent via
    --if-not-exists), then `flatpak install --reinstall` from it on the chosen
    branch, then relaunches. Runs detached with a 2s delay so we exit first.

    Returns True if the child launched. GPG verification is left to the remote's
    own config (the .flatpakrepo carries the key); we add by URL with
    --no-gpg-verify only as a fallback is NOT used here — the remote file
    provides the key so verification stays on.
    """
    import shutil
    if not shutil.which("flatpak-spawn"):
        return False

    branch = "beta" if allow_prerelease else "stable"
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "AmethystModManager",
    )
    os.makedirs(config_dir, exist_ok=True)
    log_path = os.path.join(config_dir, "amethyst-update.log")

    host = "flatpak-spawn --host"
    ref = f"{_APP_ID}/x86_64/{branch}"
    cmd = (
        f"sleep 2 && "
        f"{host} flatpak remote-add --user --if-not-exists "
        f"{_FLATPAK_REMOTE_NAME} {_FLATPAK_REMOTE_FILE_URL} && "
        f"{host} flatpak install --user --reinstall --noninteractive -y "
        f"{_FLATPAK_REMOTE_NAME} {ref} && "
        f"{host} flatpak run {_APP_ID} &>/dev/null &"
    )
    try:
        subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=open(log_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def update_flatpak_from_remote(*, allow_prerelease: bool = False) -> bool:
    """Update (or branch-switch) the app from the hosted remote (detached).

    If the running branch matches the requested channel, a plain
    `flatpak update` pulls the delta. If the channel changed (user toggled the
    pre-release box), reinstall the other branch instead — `flatpak update`
    won't cross branches. Relaunches afterwards. Returns True if launched.
    """
    import shutil
    if not shutil.which("flatpak-spawn"):
        return False

    branch = "beta" if allow_prerelease else "stable"
    config_dir = os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        "AmethystModManager",
    )
    os.makedirs(config_dir, exist_ok=True)
    log_path = os.path.join(config_dir, "amethyst-update.log")

    host = "flatpak-spawn --host"
    ref = f"{_APP_ID}/x86_64/{branch}"
    # Reinstall pins the branch (handles both same-branch update and channel
    # switch); it's a no-op download when already current, so it's safe as the
    # single path. --reinstall forces a re-pull even if the ref looks present.
    cmd = (
        f"sleep 2 && "
        f"{host} flatpak install --user --reinstall --noninteractive -y "
        f"{_FLATPAK_REMOTE_NAME} {ref} && "
        f"{host} flatpak run {_APP_ID} &>/dev/null &"
    )
    try:
        subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=open(log_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return True
    except Exception:
        return False

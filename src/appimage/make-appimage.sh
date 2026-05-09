#!/bin/bash
# Build the Amethyst Mod Manager AppImage.
#
# Two modes:
#   MM_USE_PKGBUILD=1 (default in CI) — build a real Arch package via
#     makepkg, install it to the host's /usr, then run quick-sharun. This
#     is what we ship; quick-sharun's `/usr → "$APPDIR"` path-rewriting
#     catches hardcoded paths inside vendored deps that an AppDir-staged
#     build would miss.
#   MM_USE_PKGBUILD=0 (default locally) — stage straight into the AppDir
#     without touching /usr. Faster, non-invasive, but no path-rewriting
#     safety net. Suitable for iterative dev; not what we release.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"   # = src/
SRC_TREE="$(dirname "$PROJECT_DIR")"     # = repo root

export PATH="$HOME/.local/bin:$PATH"

ARCH=$(uname -m)
VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "${PROJECT_DIR}/version.py")
[ -n "$VERSION" ] || { echo "ERROR: cannot read __version__" >&2; exit 1; }

WORK_DIR="${TMPDIR:-/tmp}/amethyst-mm-build"
OUTPATH="${WORK_DIR}/dist"
APPDIR="${WORK_DIR}/AppDir"
FINAL_OUTPATH="${SCRIPT_DIR}/dist"

# Default to PKGBUILD path in CI ($CI is set by GitHub Actions), AppDir
# path otherwise. Override with MM_USE_PKGBUILD=1/0.
: "${MM_USE_PKGBUILD:=${CI:+1}}"
: "${MM_USE_PKGBUILD:=0}"

# ── Tooling check ────────────────────────────────────────────────────
_required_tools=(quick-sharun cc awk find ldd strings wget)
if [ "$MM_USE_PKGBUILD" = "1" ]; then
    _required_tools+=(makepkg pacman)
fi
for tool in "${_required_tools[@]}"; do
    command -v "$tool" >/dev/null || {
        echo "ERROR: '$tool' not found in PATH" >&2
        exit 1
    }
done

if ! /usr/bin/python3 -c 'import tkinter' 2>/dev/null; then
    echo "ERROR: /usr/bin/python3 cannot import tkinter." >&2
    echo "Install the system 'tk' package (Arch: pacman -S tk)." >&2
    exit 1
fi

# ── Clean ────────────────────────────────────────────────────────────
echo "=== Cleaning previous build ==="
rm -rf "$WORK_DIR" "$FINAL_OUTPATH"
mkdir -p "$APPDIR/bin" "$OUTPATH" "$FINAL_OUTPATH"

# ── Aux staging (bsdtar + Cantarell font) ────────────────────────────
# 7zzs and zenity-rs are bundled by the PKGBUILD itself (PKGBUILD mode) or
# downloaded inline (AppDir mode below). Only bsdtar and the font live here:
# bsdtar would conflict with the libarchive package, and a font isn't a /usr
# binary.
AUX_DIR="${WORK_DIR}/aux"
mkdir -p "$AUX_DIR/bin" "$AUX_DIR/fonts"

echo "=== Bundling bsdtar ==="
BSDTAR_BIN="$(command -v bsdtar 2>/dev/null || true)"
if [ -n "$BSDTAR_BIN" ]; then
    cp "$BSDTAR_BIN" "$AUX_DIR/bin/bsdtar"
    chmod +x "$AUX_DIR/bin/bsdtar"
fi

echo "=== Bundling Cantarell font ==="
CANTARELL_URL="https://gitlab.gnome.org/GNOME/cantarell-fonts/-/archive/0.303.1/cantarell-fonts-0.303.1.tar.gz"
CANTARELL_TMP="${WORK_DIR}/.cantarell-tmp"
mkdir -p "$CANTARELL_TMP"
if wget -q -O "${CANTARELL_TMP}/cantarell.tar.gz" "$CANTARELL_URL"; then
    tar -xf "${CANTARELL_TMP}/cantarell.tar.gz" -C "$CANTARELL_TMP"
    VF_OTF="$(find "$CANTARELL_TMP" -name 'Cantarell-VF.otf' | head -1)"
    [ -n "$VF_OTF" ] && cp "$VF_OTF" "$AUX_DIR/fonts/Cantarell-VF.otf"
fi
rm -rf "$CANTARELL_TMP"

# Tcl/Tk runtime libs — copied to a writable staging dir so quick-sharun
# can pull demos-stripped versions. Reused by both modes.
TCLTK_STAGE="${WORK_DIR}/lib"
mkdir -p "$TCLTK_STAGE"
cp -a /usr/lib/tcl8.6 "$TCLTK_STAGE/tcl8.6"
cp -a /usr/lib/tk8.6  "$TCLTK_STAGE/tk8.6"
rm -rf "$TCLTK_STAGE/tk8.6/demos" "$TCLTK_STAGE/tcl8.6/demos"

# Desktop / icon — quick-sharun reads these via env vars.
ASSETS_DIR="${WORK_DIR}/assets"
mkdir -p "$ASSETS_DIR"
cp "${SCRIPT_DIR}/mod-manager.desktop" "$ASSETS_DIR/mod-manager.desktop"
cp "${SCRIPT_DIR}/mod-manager.png"     "$ASSETS_DIR/mod-manager.png"

# ── Locate the libloot extension (built by the separate CI job or by
# running src/LOOT/rebuild_libloot.sh locally).
LIBLOOT_SO="$(find "${PROJECT_DIR}/LOOT" -maxdepth 1 -name 'loot.cpython-*-x86_64-linux-gnu.so' 2>/dev/null | head -1 || true)"
if [ -z "$LIBLOOT_SO" ]; then
    echo "WARN: no libloot .so found in src/LOOT/ — the AppImage will lack LOOT support" >&2
fi

# ── Run quick-sharun env (shared between both modes) ─────────────────
# DEPLOY_PYTHON=1   pulls /usr/bin/python3 + stdlib (incl tkinter)
# DEPLOY_GTK=1      pulls libgtk-3 + gi typelibs for splash_gtk.py
# ALWAYS_SOFTWARE=1 forces software rendering (matches upstream)
# ANYLINUX_LIB=1    builds anylinux.so (LD_PRELOAD env-scrubber for child procs)
export ARCH VERSION OUTPATH APPDIR
export ICON="${ASSETS_DIR}/mod-manager.png"
export DESKTOP="${ASSETS_DIR}/mod-manager.desktop"
export DEPLOY_PYTHON=1
export DEPLOY_GTK=1
export ALWAYS_SOFTWARE=1
export ANYLINUX_LIB=1

# SteamOS strips glibc headers from /usr/include; quick-sharun's anylinux.so
# build needs dlfcn.h. Sysroot at ~/sdk/include works around that.
if [ -f "$HOME/sdk/include/dlfcn.h" ]; then
    export C_INCLUDE_PATH="$HOME/sdk/include${C_INCLUDE_PATH:+:$C_INCLUDE_PATH}"
elif [ ! -f /usr/include/dlfcn.h ]; then
    echo "  NOTE: ~/sdk/include not found and /usr/include/dlfcn.h missing — disabling anylinux.so." >&2
    export ANYLINUX_LIB=0
fi

if [ "$MM_USE_PKGBUILD" = "1" ]; then
    # ── PKGBUILD mode ────────────────────────────────────────────────
    echo "=== Building amethyst-mod-manager package via makepkg ==="
    PKG_BUILD_DIR="${WORK_DIR}/pkgbuild"
    mkdir -p "$PKG_BUILD_DIR"
    cp "${SCRIPT_DIR}/PKGBUILD" "$PKG_BUILD_DIR/PKGBUILD"

    # makepkg refuses to run as root. If we are root, drop to a non-root
    # user. The CI workflow creates a 'builder' user with passwordless sudo;
    # locally, the script is presumably already non-root.
    _makepkg_uid=""
    if [ "$(id -u)" = "0" ]; then
        if id builder >/dev/null 2>&1; then
            _makepkg_uid=builder
        else
            echo "ERROR: makepkg cannot run as root; create a 'builder' user first" >&2
            exit 1
        fi
        # The build user needs read access to both the PKGBUILD scratch dir
        # and the source tree (PKGBUILD reads $SRC_TREE/src/version.py and
        # package() copies from there).
        chown -R "$_makepkg_uid":"$_makepkg_uid" "$PKG_BUILD_DIR"
        chmod -R a+rX "$SRC_TREE"
    fi

    # Pass SRC_TREE / LIBLOOT_SO through the env so the PKGBUILD picks them up.
    _libloot_arg=""
    [ -n "$LIBLOOT_SO" ] && _libloot_arg="LIBLOOT_SO=$LIBLOOT_SO"
    if [ -n "$_makepkg_uid" ]; then
        sudo -u "$_makepkg_uid" \
             env SRC_TREE="$SRC_TREE" $_libloot_arg \
             bash -c "cd '$PKG_BUILD_DIR' && makepkg --noconfirm --nodeps"
    else
        ( cd "$PKG_BUILD_DIR" && SRC_TREE="$SRC_TREE" ${_libloot_arg:+env $_libloot_arg} makepkg --noconfirm --nodeps )
    fi

    PKG_FILE=$(find "$PKG_BUILD_DIR" -maxdepth 1 -name 'amethyst-mod-manager-*.pkg.tar.*' -type f | head -1)
    [ -n "$PKG_FILE" ] || { echo "ERROR: makepkg produced no package" >&2; exit 1; }

    echo "=== Installing $PKG_FILE ==="
    # --overwrite for re-runs that hit the same version; --nodeps because
    # we vendor everything pip-installed and depend only on python/gtk3/tk
    # which are already present in the container.
    pacman -U --noconfirm --overwrite '*' --nodeps "$PKG_FILE"

    echo "=== Running quick-sharun (PKGBUILD mode) ==="
    # Stdlib extension modules in lib-dynload are dlopened at runtime, so
    # quick-sharun's per-binary ldd trace never sees their DT_NEEDED entries
    # and silently drops the underlying libs. Each line below covers a stdlib
    # ext we actually import (directly or transitively):
    #   libssl/libcrypto -> _ssl.so      (HTTPS — Nexus, GitHub, updates)
    #   libuuid          -> _uuid.so     (uuid.uuid4 used by Nexus SSO)
    #   libmpdec         -> _decimal.so  (transitive deps may import decimal)
    quick-sharun \
        /usr/bin/mod-manager               \
        /usr/share/amethyst-mod-manager    \
        /usr/bin/7zzs                      \
        /usr/bin/zenity                    \
        /usr/lib/libgtk-3.so*              \
        /usr/lib/libtcl8.6.so*             \
        /usr/lib/libtk8.6.so*              \
        /usr/lib/libssl.so*                \
        /usr/lib/libcrypto.so*             \
        /usr/lib/libuuid.so*               \
        /usr/lib/libmpdec.so*              \
        "$TCLTK_STAGE/tcl8.6"              \
        "$TCLTK_STAGE/tk8.6"               \
        $( [ -f "$AUX_DIR/bin/bsdtar" ] && printf %s "$AUX_DIR/bin/bsdtar" )

    # Rewrite the wrapper's /usr/share path to "$APPDIR"/share — quick-sharun's
    # built-in /usr → "$APPDIR" rewrite only fires for dotnet scripts, so plain
    # shell wrappers need this manual step.
    sed -i -e 's|/usr/share|"$APPDIR"/share|g' "$APPDIR/bin/mod-manager"

    # Strip __pycache__ from our app tree. The PKGBUILD's package() cleans these,
    # but Arch's python ALPM hook re-generates .pyc files on `pacman -U`; quick-
    # sharun's DEBLOAT_SYS_PYTHON only touches $APPDIR/shared/lib/python*. ~4M.
    find "$APPDIR/share/amethyst-mod-manager" -type d -name '__pycache__' \
        -exec rm -rf {} + 2>/dev/null || true

    # Font goes directly into the AppDir (quick-sharun doesn't deploy fonts).
    if [ -f "$AUX_DIR/fonts/Cantarell-VF.otf" ]; then
        install -Dm644 "$AUX_DIR/fonts/Cantarell-VF.otf" \
            "$APPDIR/share/fonts/amethyst/Cantarell-VF.otf"
    fi
else
    # ── AppDir-staged mode (legacy / local dev) ──────────────────────
    echo "=== Staging source into AppDir ==="
    APP_SHARE="$APPDIR/share/amethyst-mod-manager"
    mkdir -p "$APP_SHARE"

    cp "${PROJECT_DIR}/gui.py"           "$APP_SHARE/"
    cp "${PROJECT_DIR}/cli.py"           "$APP_SHARE/"
    cp "${PROJECT_DIR}/version.py"       "$APP_SHARE/"
    cp "${PROJECT_DIR}/splash_gtk.py"    "$APP_SHARE/" 2>/dev/null || true
    cp "${PROJECT_DIR}/../Changelog.txt" "$APP_SHARE/" 2>/dev/null || true

    for dir in gui Utils Games LOOT Nexus icons wizards wrappers; do
        cp -r "${PROJECT_DIR}/${dir}" "$APP_SHARE/${dir}"
    done

    rm -f "$APP_SHARE/LOOT/rebuild_libloot.sh"
    find "$APP_SHARE" -type f -name '*.sh' -exec rm -f {} \;
    find "$APP_SHARE" -type f -name 'requirements*.txt' -delete 2>/dev/null || true
    find "$APP_SHARE/LOOT" -maxdepth 1 -name 'loot.cpython-*-x86_64-linux-gnu.so' \
        -exec strip --strip-unneeded {} + 2>/dev/null || true
    find "$APP_SHARE" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "$APP_SHARE" -type f -name '*.py' -exec chmod -x {} \;

    # Wrapper — uses the FHS /usr/share path; the post-quick-sharun sed step
    # below rewrites it to "$APPDIR"/share (sharun's flattened runtime layout).
    cat > "$APPDIR/bin/mod-manager" <<'EOF'
#!/bin/sh
# Don't export PYTHONPATH here — it would inherit into any child shell the
# user spawns from the app, shadowing their system Pillow with our vendored
# copy long after the AppImage's /tmp mount has disappeared. gui.py / cli.py
# add _vendor to sys.path themselves when $APPDIR is set.
APP_SHARE=/usr/share/amethyst-mod-manager
export MOD_MANAGER_PROFILES_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/AmethystModManager/Profiles"
mkdir -p "$MOD_MANAGER_PROFILES_DIR"
case "$1" in
    deploy|restore|list-games|list-profiles|clear-credentials)
        exec python3 "$APP_SHARE/cli.py" "$@" ;;
    *)
        exec python3 "$APP_SHARE/gui.py" "$@" ;;
esac
EOF
    chmod +x "$APPDIR/bin/mod-manager"

    echo "=== Installing pip dependencies into AppDir ==="
    VENDOR_DIR="$APP_SHARE/_vendor"
    mkdir -p "$VENDOR_DIR"

    if /usr/bin/python3 -m pip --version >/dev/null 2>&1; then
        PIP_PYTHON=/usr/bin/python3
    elif [ -x "${PROJECT_DIR}/.venv/bin/pip" ]; then
        PIP_PYTHON="${PROJECT_DIR}/.venv/bin/python3"
    elif /usr/bin/python3 -m ensurepip --user >/dev/null 2>&1; then
        PIP_PYTHON=/usr/bin/python3
    else
        echo "ERROR: no working pip found (host python3 lacks it, no .venv either)" >&2
        exit 1
    fi
    echo "  using pip from: $PIP_PYTHON"
    "$PIP_PYTHON" -m pip install --no-cache-dir --quiet \
        --target "$VENDOR_DIR" \
        -r "${PROJECT_DIR}/requirements-appimage.txt"

    rm -rf "$VENDOR_DIR/bin"
    find "$APP_SHARE" -type f -name '*.py' -exec chmod -x {} \;
    find "$VENDOR_DIR" -type f -name '*.so' -exec strip --strip-unneeded {} + 2>/dev/null || true

    # Drop AVIF (mirrors PKGBUILD trim). AVIF is the only Pillow format lazy-
    # loaded via a separate .so plugin; everything else is in _imaging.so's
    # DT_NEEDED and can't be removed without breaking PIL import.
    if [ -d "$VENDOR_DIR/pillow.libs" ]; then
        rm -f "$VENDOR_DIR/pillow.libs/"libavif-*.so* 2>/dev/null || true
    fi
    rm -f "$VENDOR_DIR/PIL/"_avif.cpython-*.so 2>/dev/null || true

    # 7zzs + zenity-rs: PKGBUILD mode gets these from /usr/bin (installed by
    # the package). AppDir mode doesn't run pacman, so download them inline.
    echo "=== Downloading 7zzs + zenity-rs (AppDir mode) ==="
    SEVENZIP_URL="https://github.com/ip7z/7zip/releases/download/26.00/7z2600-linux-x64.tar.xz"
    SEVENZIP_TMP="${WORK_DIR}/.7z-tmp"
    mkdir -p "$SEVENZIP_TMP"
    wget -q -O "${SEVENZIP_TMP}/7z.tar.xz" "$SEVENZIP_URL"
    tar -xf "${SEVENZIP_TMP}/7z.tar.xz" -C "$SEVENZIP_TMP"
    install -Dm755 "$SEVENZIP_TMP/7zzs" "$APPDIR/bin/7zzs"
    ln -sf 7zzs "$APPDIR/bin/7z"
    ln -sf 7zzs "$APPDIR/bin/7za"
    rm -rf "$SEVENZIP_TMP"

    case "$ARCH" in
        x86_64)  ZENITY_RS_ASSET="zenity-rs-x86_64-linux" ;;
        aarch64) ZENITY_RS_ASSET="zenity-rs-aarch64-linux" ;;
        *) echo "ERROR: no zenity-rs build for arch $ARCH" >&2; exit 1 ;;
    esac
    wget -q -O "$APPDIR/bin/zenity" \
        "https://github.com/QaidVoid/zenity-rs/releases/latest/download/${ZENITY_RS_ASSET}"
    chmod +x "$APPDIR/bin/zenity"

    # bsdtar + font from $AUX_DIR
    [ -f "$AUX_DIR/bin/bsdtar" ] && cp -a "$AUX_DIR/bin/bsdtar" "$APPDIR/bin/bsdtar"
    if [ -f "$AUX_DIR/fonts/Cantarell-VF.otf" ]; then
        install -Dm644 "$AUX_DIR/fonts/Cantarell-VF.otf" \
            "$APPDIR/share/fonts/amethyst/Cantarell-VF.otf"
    fi

    echo "=== Running quick-sharun (AppDir mode) ==="
    # See PKGBUILD-mode comment above re: stdlib extension dlopen libs.
    quick-sharun \
        "$APPDIR/bin/mod-manager"        \
        "$APP_SHARE"                     \
        "$APPDIR/bin/7zzs"               \
        "$APPDIR/bin/zenity"             \
        /usr/lib/libgtk-3.so*            \
        /usr/lib/libtcl8.6.so*           \
        /usr/lib/libtk8.6.so*            \
        /usr/lib/libssl.so*              \
        /usr/lib/libcrypto.so*           \
        /usr/lib/libuuid.so*             \
        /usr/lib/libmpdec.so*            \
        "$TCLTK_STAGE/tcl8.6"            \
        "$TCLTK_STAGE/tk8.6"             \
        $( [ -f "$APPDIR/bin/bsdtar" ] && printf %s "$APPDIR/bin/bsdtar" )

    # See PKGBUILD-mode comment above for why this manual rewrite is needed.
    sed -i -e 's|/usr/share|"$APPDIR"/share|g' "$APPDIR/bin/mod-manager"
fi

# ── Hicolor icon for AppImageLauncher / appimaged integration ────────
# libappimage resolves Icon=mod-manager via the FreeDesktop spec, i.e.
# usr/share/icons/hicolor/<size>/apps/<name>.png. Without it AppImageLauncher
# logs "no icon to set" and refuses to write a host .desktop file.
# quick-sharun deploys binaries + libs but doesn't propagate icon themes,
# so we install the icon into the AppDir explicitly here for both modes.
install -Dm644 "${ASSETS_DIR}/mod-manager.png" \
    "$APPDIR/usr/share/icons/hicolor/256x256/apps/mod-manager.png"

# ── Build the AppImage ───────────────────────────────────────────────
echo "=== Building AppImage ==="
quick-sharun --make-appimage

RAW_OUT=$(find "$OUTPATH" -maxdepth 2 -name '*.AppImage' -type f | head -1)
FINAL="${FINAL_OUTPATH}/AmethystModManager-${VERSION}-${ARCH}.AppImage"
if [ -n "$RAW_OUT" ]; then
    mv "$RAW_OUT" "$FINAL"
fi

echo ""
echo "=== Build complete ==="
[ -f "$FINAL" ] && {
    echo "AppImage: $FINAL"
    echo "Size: $(du -h "$FINAL" | cut -f1)"
}

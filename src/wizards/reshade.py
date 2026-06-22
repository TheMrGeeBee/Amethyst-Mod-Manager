"""
ReShade installation wizard.

Multi-step dialog that walks the user through:
  1. Downloading the latest ReShade installer from reshade.me and extracting
     the DLL (the installer is a self-extracting zip).
  2. Installing d3dcompiler_47 into the game's Proton prefix via protontricks.
  3. Copying all ReShade files to the game folder (or Root_Folder staging):
       - <reshade_dll>       (e.g. dxgi.dll)
       - ReShade.ini         (bundled, uses relative shader paths)
       - ReShadePreset.ini   (empty preset)
       - reshade-shaders/    (bundled Shaders + Textures)
     and applying the Wine DLL override to the Proton prefix.
"""

from __future__ import annotations

import io
import re
import shutil
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import customtkinter as ctk

from Utils.protontricks import D3D_DEP_KEY, install_d3dcompiler_47, is_dep_installed
from Utils.deploy import apply_wine_dll_overrides
from gui.mod_name_utils import sanitize_mod_folder_name

if TYPE_CHECKING:
    from Games.base_game import BaseGame

from gui.theme import (
    ACCENT, ACCENT_HOV, BG_DEEP, BG_HEADER, BG_PANEL, BORDER,
    TEXT_ON_ACCENT,
    TEXT_DIM, TEXT_MAIN, TEXT_OK, TEXT_WARN, TEXT_ERR,
    FONT_NORMAL, FONT_BOLD, FONT_SMALL,
)
from gui.wheel_compat import bind_scrollable_wheel

_RESHADE_BASE_URL = "https://reshade.me/downloads/"
_RESHADE_HOME_URL = "https://reshade.me/"

# Rendering API → DLL name the game loads ReShade as.
# (label, dll_name)
_API_CHOICES: list[tuple[str, str]] = [
    ("DirectX 10 / 11 / 12  (dxgi.dll)", "dxgi.dll"),
    ("DirectX 9  (d3d9.dll)", "d3d9.dll"),
    ("OpenGL  (opengl32.dll)", "opengl32.dll"),
    ("Vulkan  (dxgi.dll)", "dxgi.dll"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_latest_reshade_url() -> tuple[str, str]:
    """Return (download_url, version_string) for the latest ReShade release.

    Raises RuntimeError if the version cannot be determined.
    """
    req = urllib.request.Request(
        _RESHADE_HOME_URL,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
    )
    from Utils.ca_bundle import get_ssl_context
    with urllib.request.urlopen(req, timeout=15, context=get_ssl_context()) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # Match e.g. "ReShade_Setup_6.7.3.exe" (not the _Addon variant)
    match = re.search(r'ReShade_Setup_(\d+\.\d+\.\d+)\.exe(?!"[^"]*Addon)', html)
    if not match:
        # Fallback: accept any non-Addon exe
        match = re.search(r'ReShade_Setup_(\d+\.\d+\.\d+)\.exe', html)
    if not match:
        raise RuntimeError("Could not find ReShade download link on reshade.me.")

    version = match.group(1)
    url = f"{_RESHADE_BASE_URL}ReShade_Setup_{version}.exe"
    return url, version


def _download_and_extract_reshade_dll(dest_dir: Path, arch: int = 64) -> Path:
    """Download the latest ReShade installer and extract the DLL to *dest_dir*.

    *arch* selects ``ReShade64.dll`` (default) or ``ReShade32.dll`` for
    32-bit games (Fallout 3/NV, Oblivion, Skyrim classic, etc.).

    The installer .exe is a self-extracting zip; Python's zipfile can read it
    by seeking past the PE stub.

    Returns the path to the extracted DLL.
    Raises RuntimeError on any failure.
    """
    url, version = _fetch_latest_reshade_url()

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
    )
    from Utils.ca_bundle import get_ssl_context
    with urllib.request.urlopen(req, timeout=60, context=get_ssl_context()) as resp:
        data = resp.read()

    want = f"reshade{arch}.dll"
    fallback = "reshade32.dll" if arch == 64 else "reshade64.dll"

    buf = io.BytesIO(data)
    try:
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            dll_name = next((n for n in names if n.lower() == want), None)
            if dll_name is None:
                dll_name = next((n for n in names if n.lower() == fallback), None)
            if dll_name is None:
                raise RuntimeError(
                    f"ReShade installer did not contain {want} or {fallback}. "
                    f"Found: {names}"
                )
            dest_dir.mkdir(parents=True, exist_ok=True)
            out_path = dest_dir / dll_name
            with zf.open(dll_name) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"ReShade installer is not a valid zip archive: {exc}") from exc

    return out_path


# Always downloaded — the official ReShade shader set.
_SHADER_BASE_URL = "https://github.com/crosire/reshade-shaders/archive/refs/heads/slim.zip"
_SHADER_BASE_SUBFOLDER = None  # has Shaders/ and Textures/ at root

# Optional shader packs shown as checkboxes in the wizard.
# Each entry: (label, url, subfolder)
#   subfolder=None  → extract Shaders/ and Textures/ from repo root
#   subfolder="xyz" → strip repo root then extract only the "xyz/" subtree
_OPTIONAL_SHADER_PACKS: list[tuple[str, str, "str | None"]] = [
    ("SweetFX",          "https://github.com/CeeJayDK/SweetFX/archive/refs/heads/master.zip",                  None),
    ("qUINT",            "https://github.com/martymcmodding/qUINT/archive/refs/heads/master.zip",              None),
    ("iMMERSE",          "https://github.com/martymcmodding/iMMERSE/archive/refs/heads/main.zip",              None),
    ("METEOR",           "https://github.com/martymcmodding/METEOR/archive/refs/heads/main.zip",               None),
    ("AstrayFX",         "https://github.com/BlueSkyDefender/AstrayFX/archive/refs/heads/master.zip",          None),
    ("Depth3D",          "https://github.com/BlueSkyDefender/Depth3D/archive/refs/heads/master.zip",           None),
    ("FXShaders",        "https://github.com/luluco250/FXShaders/archive/refs/heads/master.zip",               None),
    ("Pirate Shaders",   "https://github.com/Heathen/Pirate-Shaders/archive/refs/heads/master.zip",            "reshade-shaders"),
    ("OtisFX",           "https://github.com/FransBouma/OtisFX/archive/refs/heads/master.zip",                 None),
    ("AcerolaFX",        "https://github.com/GarrettGunnell/AcerolaFX/archive/refs/heads/main.zip",            None),
    ("prod80",           "https://github.com/prod80/prod80-ReShade-Repository/archive/refs/heads/master.zip",  None),
    ("AlucardDH (DH)",   "https://github.com/AlucardDH/dh-reshade-shaders/archive/refs/heads/master.zip",      None),
    ("CobraFX",          "https://github.com/LordKobra/CobraFX/archive/refs/heads/master.zip",                 None),
    ("CorgiFX",          "https://github.com/originalnicodr/CorgiFX/archive/refs/heads/master.zip",            None),
    ("Daodan317081",     "https://github.com/Daodan317081/reshade-shaders/archive/refs/heads/master.zip",      None),
    ("Brussell Shaders", "https://github.com/brussell1/Shaders/archive/refs/heads/master.zip",                 None),
    ("Matsilagi (Ports)","https://github.com/Matsilagi/reshade-shaders/archive/refs/heads/master.zip",         None),
    ("fubax-shaders",    "https://github.com/Fubaxiusz/fubax-shaders/archive/refs/heads/master.zip",           None),
    ("Insane-Shaders",   "https://github.com/LordOfLunacy/Insane-Shaders/archive/refs/heads/master.zip",       None),
    ("Warp-FX",          "https://github.com/Radegast-FFXIV/Warp-FX/archive/refs/heads/master.zip",            None),
    ("GShade Shaders",   "https://github.com/Mortalitas/GShade-Shaders/archive/refs/heads/master.zip",         None),
    ("Legacy (crosire)", "https://github.com/crosire/reshade-shaders/archive/refs/heads/legacy.zip",           None),
    # --- additions from crosire's official EffectPackages.ini index ---
    ("CRT-Royale",       "https://github.com/akgunter/crt-royale-reshade/archive/refs/heads/master.zip",       "reshade-shaders"),
    ("RSRetroArch",      "https://github.com/Matsilagi/RSRetroArch/archive/refs/heads/main.zip",                None),
    ("VRToolkit",        "https://github.com/retroluxfilm/reshade-vrtoolkit/archive/refs/heads/main.zip",       None),
    ("FGFX",             "https://github.com/AlexTuduran/FGFX/archive/refs/heads/main.zip",                      None),
    ("CShade",           "https://github.com/papadanku/CShade/archive/refs/heads/main.zip",                      None),
    ("Lilium HDR",       "https://github.com/EndlesslyFlowering/ReShade_HDR_shaders/archive/refs/heads/master.zip", None),
    ("vort_Shaders",     "https://github.com/vortigern11/vort_Shaders/archive/refs/heads/main.zip",              None),
    ("BX-Shade",         "https://github.com/liuxd17thu/BX-Shade/archive/refs/heads/main.zip",                   None),
    ("SHADERDECK",       "https://github.com/IAmTreyM/SHADERDECK/archive/refs/heads/main.zip",                   None),
    ("Ann-ReShade",      "https://github.com/AnastasiaGals/Ann-ReShade/archive/refs/heads/main.zip",             None),
    ("PumboAutoHDR",     "https://github.com/Filoppi/PumboAutoHDR/archive/refs/heads/master.zip",                None),
    ("ZenteonFX",        "https://github.com/Zenteon/ZenteonFX/archive/refs/heads/main.zip",                     None),
    ("Ptho-FX",          "https://github.com/PthoEastCoast/Ptho-FX/archive/refs/heads/main.zip",                 None),
    ("potatoFX",         "https://github.com/GimleLarpes/potatoFX/archive/refs/heads/main.zip",                  None),
    ("Anagrama",         "https://github.com/nullfrctl/reshade-shaders/archive/refs/heads/main.zip",             None),
    ("MaxG3D HDR",       "https://github.com/MaxG2D/ReshadeSimpleHDRShaders/archive/refs/heads/main.zip",        None),
    ("Barbatos",         "https://github.com/BarbatosBachiko/Reshade-Shaders/archive/refs/heads/main.zip",       None),
    ("smolbbsoop",       "https://github.com/smolbbsoop/smolbbsoopshaders/archive/refs/heads/main.zip",          None),
    ("BFBFX",            "https://github.com/yplebedev/BFBFX/archive/refs/heads/main.zip",                       "reshade-shaders"),
    ("Rendepth",         "https://github.com/outmode/rendepth-reshade/archive/refs/heads/main.zip",              None),
    ("Crop and Resize",  "https://github.com/P0NYSLAYSTATION/Scaling-Shaders/archive/refs/heads/main.zip",       None),
    ("LumeniteFX",       "https://github.com/umar-afzaal/LumeniteFX/archive/refs/heads/mainline.zip",            None),
    ("Reshade-Shades",   "https://github.com/JakobPCoder/Reshade-Shades/archive/refs/heads/main.zip",            None),
]

# Effect filenames that presets still reference but which no current shader
# pack ships — they were renamed or removed upstream, so they can never be
# downloaded no matter how many packs are ticked.  Used only to give the user
# a clearer "this isn't a bug" message (key = bare filename, lowercase; value
# = explanation).  ReShade resolves effects by bare filename, so the rename
# targets below are the same shader under a new name.
_OBSOLETE_PRESET_EFFECTS: dict[str, str] = {
    "depth3d.fx":            "renamed upstream to SuperDepth3D.fx (Depth3D pack)",
    "superdepth3d_vr+.fx":   "renamed upstream to SuperDepth3D.fx (Depth3D pack)",
    "emotionblur.fx":        "removed from AstrayFX upstream (had a shader bug)",
}


# ReShade's canonical core headers.  These ship with the base ``slim`` repo and
# must always come from it: several packs vendor their own *stale* copy (e.g.
# Matsilagi ships a ReShade 3.x ``ReShade.fxh`` that lacks ``BUFFER_PIXEL_SIZE``
# / ``BUFFER_SCREEN_SIZE``).  If a pack's copy overwrites the base one, every
# modern shader referencing those macros fails to compile (X3004 undeclared
# identifier).  We extract the base repo first and refuse to let any later pack
# overwrite these names.
_CORE_HEADERS = frozenset({"reshade.fxh", "reshadeui.fxh"})


def _extract_zip_into(
    data: bytes, dest: Path, subfolder: "str | None", *, is_base: bool = False
) -> None:
    """Extract the ``Shaders/`` and ``Textures/`` trees from a GitHub repo zip
    into *dest* (which is the ``reshade-shaders/`` output folder).

    *subfolder* — if None, those folders sit at the repo root.  If a string,
    they live inside that subfolder (e.g. Pirate-Shaders wraps everything in a
    ``reshade-shaders/`` dir; qUINT keeps only a ``Shaders/`` dir).  Either
    way the result is merged as ``dest/Shaders/...`` and ``dest/Textures/...``
    so packs never end up double-nested under another ``reshade-shaders/``.

    *is_base* — True only for the base ``slim`` repo (extracted first).  Packs
    (is_base=False) are not allowed to overwrite :data:`_CORE_HEADERS`, so a
    pack shipping a stale ``ReShade.fxh`` can't clobber the canonical one.
    """
    _KEEP = ("Shaders/", "Textures/")

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            top = next(
                (n.split("/")[0] for n in zf.namelist() if "/" in n),
                None,
            )
            if top is None:
                raise RuntimeError("Unexpected zip layout (no top-level folder).")

            # Path prefix to strip: the repo root, plus the wrapper subfolder
            # when one is named.
            prefix = top + "/"
            if subfolder:
                prefix += subfolder.rstrip("/") + "/"

            for member in zf.namelist():
                if not member.startswith(prefix):
                    continue
                rel = member[len(prefix):]
                if not rel:
                    continue

                # Keep only the Shaders/ and Textures/ subtrees.  Match the top
                # folder case-insensitively (some packs ship lowercase
                # ``shaders/`` — e.g. CShade) and canonicalise it to the
                # capitalised name so every pack merges into the same
                # dest/Shaders and dest/Textures and the pruner finds Textures/.
                head, _, tail = rel.partition("/")
                canon = next((k.rstrip("/") for k in _KEEP
                              if head.lower() == k.rstrip("/").lower()), None)
                if canon is None:
                    continue
                rel = canon + ("/" + tail if tail else "")

                target = dest / rel
                if member.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    # Don't let a pack overwrite a canonical core header that
                    # the base repo already provided — packs sometimes vendor a
                    # stale copy that breaks every modern shader.
                    if (
                        not is_base
                        and target.name.lower() in _CORE_HEADERS
                        and target.exists()
                    ):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Shader zip is not a valid archive: {exc}") from exc


def _pack_cache_filename(url: str) -> str:
    """Readable, collision-free filename for a pack's cached zip.

    GitHub archive URLs look like
    ``…/<owner>/<repo>/archive/refs/heads/<branch>.zip`` — fold owner+repo+
    branch into the name so two repos with the same branch don't clash, and
    sanitise for the filesystem.
    """
    try:
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        owner, repo = parts[0], parts[1]
        branch = parts[-1].rsplit(".", 1)[0]  # drop .zip
        stem = f"{owner}__{repo}__{branch}"
    except Exception:
        stem = url
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem)
    return stem + ".zip"


def _fetch_pack_zip(url: str, cache_dir: Path, timeout: float = 60.0) -> bytes:
    """Return a pack's zip bytes, reusing a cached copy when GitHub reports the
    content is unchanged (HTTP 304 via ``If-None-Match``).

    The zip is cached under *cache_dir* as ``<owner>__<repo>__<branch>.zip``
    with a sidecar ``.etag`` file.  A 304 (or any network error when a cached
    copy exists) reuses the cached zip, so a second wizard run re-downloads
    nothing.  Raises RuntimeError only when there is neither a fresh download
    nor a usable cached copy.
    """
    zip_path = cache_dir / _pack_cache_filename(url)
    etag_path = zip_path.with_suffix(zip_path.suffix + ".etag")

    cached = zip_path.read_bytes() if zip_path.is_file() else None
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    }
    if cached is not None and etag_path.is_file():
        try:
            tag = etag_path.read_text(encoding="utf-8").strip()
            if tag:
                headers["If-None-Match"] = tag
        except OSError:
            pass

    req = urllib.request.Request(url, headers=headers)
    try:
        from Utils.ca_bundle import get_ssl_context
        with urllib.request.urlopen(req, timeout=timeout, context=get_ssl_context()) as resp:
            body = resp.read()
            try:
                zip_path.write_bytes(body)
                new_tag = resp.headers.get("ETag")
                if new_tag:
                    etag_path.write_text(new_tag, encoding="utf-8")
            except OSError:
                pass  # cache write is best-effort; the bytes are still usable
            return body
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached is not None:
            return cached  # unchanged upstream — reuse the cached zip
        if cached is not None:
            return cached
        raise RuntimeError(f"{url}: {exc}") from exc
    except Exception as exc:
        if cached is not None:
            return cached  # offline / transient error — fall back to cache
        raise RuntimeError(f"{url}: {exc}") from exc


def _download_and_extract_shaders(
    dest_dir: Path,
    optional_packs: "list[tuple[str, str, str | None]]",
) -> Path:
    """Download the base shader repo plus any selected optional packs and merge
    them all into *dest_dir*/reshade-shaders/.

    Each pack's zip is cached under ``dest_dir/packs/`` (ETag-validated), so a
    second run reuses the cached zips and downloads nothing that hasn't changed
    upstream.

    *optional_packs* is a subset of :data:`_OPTIONAL_SHADER_PACKS` — the
    entries the user ticked.  Downloads run in parallel.

    Returns the path to the ``reshade-shaders/`` folder.
    Raises RuntimeError on any failure.
    """
    out = dest_dir / "reshade-shaders"
    # Start from a clean tree so a previous preset-pruned run can't leave the
    # cache missing effects for a later non-preset (or different-preset) run.
    if out.exists():
        shutil.rmtree(str(out), ignore_errors=True)
    out.mkdir(parents=True, exist_ok=True)

    cache_dir = dest_dir / "packs"
    cache_dir.mkdir(parents=True, exist_ok=True)

    to_fetch = [(_SHADER_BASE_URL, _SHADER_BASE_SUBFOLDER)] + [
        (url, sub) for (_, url, sub) in optional_packs
    ]

    errors: list[str] = []
    results: list[tuple[bytes, "str | None"] | None] = [None] * len(to_fetch)

    def _fetch(idx: int, url: str, subfolder: "str | None") -> None:
        try:
            results[idx] = (_fetch_pack_zip(url, cache_dir), subfolder)
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    threads = [
        threading.Thread(target=_fetch, args=(i, url, sub), daemon=True)
        for i, (url, sub) in enumerate(to_fetch)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        raise RuntimeError("Shader download failed:\n" + "\n".join(errors))

    # Index 0 is always the base repo (see to_fetch above).  Extract it first
    # with is_base=True so its canonical core headers are laid down before any
    # pack, and later packs can't overwrite them.
    for idx, entry in enumerate(results):
        if entry is not None:
            data, subfolder = entry
            _extract_zip_into(data, out, subfolder, is_base=(idx == 0))

    return out


# ---------------------------------------------------------------------------
# Preset-driven effect selection
# ---------------------------------------------------------------------------

def _set_preset_path_in_ini(ini_path: Path, preset_filename: str) -> None:
    """Rewrite the ``PresetPath=`` line in a ReShade.ini so ReShade loads the
    preset under *preset_filename* (a bare name, placed next to the ini).

    Best-effort: if the ini can't be read/written the caller's preset still
    installs, just under a name ReShade won't auto-select.
    """
    try:
        lines = ini_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    new_value = ".\\" + preset_filename
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip().lower() == "presetpath":
            lines[i] = "PresetPath=" + new_value
            break
    else:
        return  # no PresetPath key — leave the ini untouched
    try:
        ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def parse_preset_effect_files(preset_path: Path) -> set[str]:
    """Return the set of ``.fx`` filenames referenced by a ReShade preset.

    ReShade presets list active effects in a top-level (section-less)
    ``Techniques=`` key as comma-separated ``TechniqueName@File.fx`` entries.
    The part after the ``@`` is the effect filename.  This mirrors the
    official ReShade installer's SelectEffectsPage logic.

    Only ``Techniques=`` (the *enabled* effects) drives what gets installed.
    A preset also carries a ``TechniqueSorting=`` key, but that lists every
    effect ReShade has ever seen — its display/sort order, not a request to
    install them.  Including it kept dozens of un-enabled effects (Layer.fx,
    ASCII.fx, …) whose include closure the pruner never fully satisfied, so
    they threw compile errors (e.g. undeclared ``BUFFER_SCREEN_SIZE``) at
    startup even though the preset never used them.

    Returns an empty set if no ``Techniques`` key is found (caller should
    treat that as "install everything").
    """
    wanted: set[str] = set()
    try:
        text = preset_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return wanted

    # Only the section-less Techniques key matters; it appears before the
    # first "[Section]" header in a real preset.
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            break  # entered the per-effect sections — stop
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key.strip() != "Techniques":
            continue
        for entry in value.split(","):
            entry = entry.strip()
            at = entry.find("@")
            if at >= 0:
                fname = entry[at + 1:].strip()
                if fname:
                    wanted.add(fname.lower())
    return wanted


# Matches ``#include "Foo.fxh"`` (or .fx) — captures the bare filename.
_INCLUDE_RE = re.compile(
    r'#\s*include\s+[<"]\s*([^>"]+?)\s*[>"]', re.IGNORECASE
)
# Matches a quoted texture filename referenced inside a shader, e.g.
# ``source = "lut.png";`` — captures the basename of any image asset.
_TEXTURE_REF_RE = re.compile(
    r'["\']([^"\']+\.(?:png|jpg|jpeg|bmp|dds|tga))["\']', re.IGNORECASE
)


def _scan_includes_and_textures(
    path: Path,
) -> "tuple[set[str], set[str]]":
    """Return (included basenames lower, texture basenames lower) referenced
    directly by the shader at *path*.  Best-effort text scan — never raises."""
    incs: set[str] = set()
    texs: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return incs, texs
    for m in _INCLUDE_RE.finditer(text):
        incs.add(Path(m.group(1).replace("\\", "/")).name.lower())
    for m in _TEXTURE_REF_RE.finditer(text):
        texs.add(Path(m.group(1).replace("\\", "/")).name.lower())
    return incs, texs


def prune_shaders_to_preset(shaders_dir: Path, wanted: "set[str]") -> set[str]:
    """Delete every ``.fx`` under *shaders_dir* whose filename is not in
    *wanted*, then prune the ``.fxh`` include headers and textures down to only
    those the surviving effects transitively reference.

    The same effect filename can exist in several downloaded packs.  ReShade
    resolves effects by bare filename across its recursive search path, so two
    copies of e.g. ``LiftGammaGain.fx`` would collide as duplicate techniques.
    Only the first copy of each wanted effect is kept; later duplicates are
    removed.

    Headers/textures are matched by bare filename (the same way ReShade's
    search path resolves them), so a kept effect that ``#include``\\ s
    ``ReShade.fxh`` keeps the first ``ReShade.fxh`` found anywhere in the tree.
    Without this the merged set from ~20 packs leaves hundreds of unused
    ``.fxh``/texture files behind even when the preset uses a handful of
    effects.

    Returns the subset of *wanted* that was **not** found on disk, so the
    caller can warn about effects missing from the downloaded packs.
    """
    if not wanted:
        return set()

    # --- pass 1: keep only the wanted .fx (first copy of each) ----------------
    found: set[str] = set()
    kept_fx: list[Path] = []
    for fx in sorted(shaders_dir.rglob("*.fx"), key=lambda p: (len(p.parts), str(p))):
        name = fx.name.lower()
        if name in wanted and name not in found:
            found.add(name)        # keep the first copy
            kept_fx.append(fx)
        else:
            # Unwanted effect, or a duplicate of one already kept.
            try:
                fx.unlink()
            except OSError:
                pass

    # --- pass 2: resolve transitive #include closure + texture refs -----------
    # Index every remaining include header by bare name so includes resolve the
    # way ReShade's search path does.  Headers aren't always ``.fxh``: some
    # packs (e.g. Pirate Shaders) ship preprocessor config as ``.cfg`` and
    # ``#include`` it from the effect, so any non-.fx include extension counts.
    header_by_name: dict[str, Path] = {}
    for ext in ("*.fxh", "*.cfg", "*.h"):
        for h in sorted(shaders_dir.rglob(ext), key=lambda p: (len(p.parts), str(p))):
            header_by_name.setdefault(h.name.lower(), h)

    needed_headers: set[str] = set()
    needed_tex: set[str] = set()
    # BFS over includes starting from the kept effects.
    queue: list[Path] = list(kept_fx)
    seen: set[str] = set()
    while queue:
        cur = queue.pop()
        incs, texs = _scan_includes_and_textures(cur)
        needed_tex |= texs
        for inc in incs:
            if inc in seen:
                continue
            seen.add(inc)
            if inc.endswith(".fx"):
                # An effect including another effect — already kept if wanted;
                # otherwise we can't satisfy it, ignore.
                continue
            if inc in header_by_name:
                needed_headers.add(inc)
                queue.append(header_by_name[inc])

    # --- pass 3: delete unreferenced headers and textures ---------------------
    # Keep only the first copy of each needed header (the one ``header_by_name``
    # resolved to); drop unreferenced headers and same-named duplicates from
    # other packs so the tree mirrors a hand-authored preset mod.
    keep_header_paths = {header_by_name[n] for n in needed_headers if n in header_by_name}
    header_files = (
        list(shaders_dir.rglob("*.fxh"))
        + list(shaders_dir.rglob("*.cfg"))
        + list(shaders_dir.rglob("*.h"))
    )
    for h in header_files:
        if h not in keep_header_paths:
            try:
                h.unlink()
            except OSError:
                pass

    tex_root = shaders_dir / "Textures"
    if tex_root.is_dir():
        kept_tex_names: set[str] = set()
        for t in sorted(tex_root.rglob("*"), key=lambda p: (len(p.parts), str(p))):
            if not t.is_file():
                continue
            name = t.name.lower()
            if name in needed_tex and name not in kept_tex_names:
                kept_tex_names.add(name)        # keep the first copy
            else:
                try:
                    t.unlink()
                except OSError:
                    pass

    # Clean up now-empty directories left behind by the deletions.
    for d in sorted(shaders_dir.rglob("*"), key=lambda p: -len(p.parts)):
        if d.is_dir():
            try:
                next(d.iterdir())
            except StopIteration:
                try:
                    d.rmdir()
                except OSError:
                    pass
            except OSError:
                pass

    return {w for w in wanted if w not in found}


# ============================================================================
# Wizard
# ============================================================================

class ReShadeWizard(ctk.CTkFrame):
    """Three-step wizard: download ReShade, install d3dcompiler_47, deploy files."""

    def __init__(
        self,
        parent,
        game: "BaseGame",
        log_fn=None,
        *,
        on_close=None,
        reshade_dll: "str | None" = None,
        reshade_arch: "int | None" = None,
    ):
        super().__init__(parent, fg_color=BG_DEEP, corner_radius=0)
        self._on_close_cb = on_close or (lambda: None)
        self._game = game
        self._log = log_fn or (lambda _: None)

        # Suggested defaults from the game handler (may be None for custom
        # games). The user confirms / overrides these in step 1.
        self._reshade_dll = reshade_dll or "dxgi.dll"   # e.g. "dxgi.dll"
        self._reshade_arch = reshade_arch or 64         # 32 or 64

        # DLL stem used for the Wine override key, e.g. "dxgi" — recomputed
        # from the user's selection in _apply_api_choice().
        self._override_key = Path(self._reshade_dll).stem

        # Tk vars backing the step-1 selectors (created in _show_step_api).
        self._api_var: "ctk.StringVar | None" = None
        self._arch_var: "ctk.StringVar | None" = None

        self._extracted_dll: Path | None = None      # path to ReShade DLL in download cache
        self._extracted_shaders: Path | None = None  # path to reshade-shaders/ in download cache

        # Optional shader pack checkboxes — populated in step 1
        self._shader_pack_vars: list[ctk.BooleanVar] = [
            ctk.BooleanVar(value=False) for _ in _OPTIONAL_SHADER_PACKS
        ]

        # Optional user-supplied ReShade preset. When set, every shader pack
        # is downloaded and then pruned to only the effects the preset uses.
        self._preset_path: Path | None = None
        self._preset_wanted: set[str] = set()    # required .fx filenames (lower)
        self._preset_missing: set[str] = set()   # wanted effects not found on disk

        # Install destination: "game" | "root_folder" | "mod"
        self._install_dest = ctk.StringVar(value="game")
        self._mod_name_var = ctk.StringVar(value="ReShade")
        # Track whether the user has typed in the mod-name field, so the
        # preset-derived default doesn't overwrite a manual edit on re-entry.
        self._mod_name_edited = False
        self._suppress_mod_name_trace = False
        self._mod_name_var.trace_add("write", self._on_mod_name_typed)

        # --- Title bar ---
        title_bar = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=40)
        title_bar.pack(fill="x")
        title_bar.pack_propagate(False)
        ctk.CTkLabel(
            title_bar, text=f"Install ReShade \u2014 {game.name}",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(side="left", padx=12, pady=8)
        ctk.CTkButton(
            title_bar, text="\u2715", width=32, height=32, font=FONT_BOLD,
            fg_color="transparent", hover_color=BG_PANEL, text_color=TEXT_MAIN,
            command=self._on_cancel,
        ).pack(side="right", padx=4, pady=4)

        self._body = ctk.CTkFrame(self, fg_color=BG_DEEP)
        self._body.pack(fill="both", expand=True, padx=20, pady=20)

        self._show_step_api()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _on_cancel(self):
        self._cleanup_tmp()
        self._on_close_cb()

    def _cleanup_tmp(self):
        # Pack zips are kept under download_cache/reshade/packs/ (ETag-cached)
        # so the next run reuses them instead of re-downloading.
        pass

    def _clear_body(self):
        for w in self._body.winfo_children():
            w.destroy()

    # ------------------------------------------------------------------
    # Step 1 — Rendering API / architecture
    # ------------------------------------------------------------------

    def _show_step_api(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 1: Rendering API & Architecture",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 4))

        ctk.CTkLabel(
            self._body,
            text=(
                "Choose the graphics API this game uses and its executable\n"
                "architecture. If you're not sure, dxgi.dll / 64-bit works for\n"
                "most modern games."
            ),
            font=FONT_SMALL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 14))

        form = ctk.CTkFrame(self._body, fg_color=BG_PANEL, corner_radius=6)
        form.pack(fill="x", pady=(0, 12))

        # --- API / DLL selector ---
        ctk.CTkLabel(
            form, text="Rendering API (DLL)",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(anchor="w", padx=12, pady=(12, 2))

        api_labels = [lbl for lbl, _dll in _API_CHOICES]
        # Pre-select the label whose DLL matches the handler suggestion.
        default_label = next(
            (lbl for lbl, dll in _API_CHOICES if dll == self._reshade_dll),
            api_labels[0],
        )
        self._api_var = ctk.StringVar(value=default_label)
        ctk.CTkOptionMenu(
            form, values=api_labels, variable=self._api_var,
            font=FONT_NORMAL, fg_color=BG_DEEP, button_color=ACCENT,
            button_hover_color=ACCENT_HOV, text_color=TEXT_MAIN,
            dropdown_fg_color=BG_PANEL, dropdown_text_color=TEXT_MAIN,
        ).pack(fill="x", padx=12, pady=(0, 10))

        # --- Architecture selector ---
        ctk.CTkLabel(
            form, text="Executable architecture",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(anchor="w", padx=12, pady=(4, 2))

        self._arch_var = ctk.StringVar(value=str(self._reshade_arch))
        arch_row = ctk.CTkFrame(form, fg_color="transparent")
        arch_row.pack(anchor="w", padx=12, pady=(0, 12))
        for val, label in (("64", "64-bit"), ("32", "32-bit")):
            ctk.CTkRadioButton(
                arch_row, text=label, value=val, variable=self._arch_var,
                font=FONT_NORMAL, text_color=TEXT_MAIN,
                fg_color=ACCENT, hover_color=ACCENT_HOV,
            ).pack(side="left", padx=(0, 20))

        ctk.CTkButton(
            self._body, text="Next →", width=120, height=36,
            font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._apply_api_choice,
        ).pack(side="bottom")

    def _apply_api_choice(self):
        """Persist the API/arch selection and advance to shader selection."""
        if self._api_var is not None:
            chosen = self._api_var.get()
            self._reshade_dll = next(
                (dll for lbl, dll in _API_CHOICES if lbl == chosen),
                self._reshade_dll,
            )
            self._override_key = Path(self._reshade_dll).stem
        if self._arch_var is not None:
            try:
                self._reshade_arch = int(self._arch_var.get())
            except ValueError:
                self._reshade_arch = 64
        self._show_step_shaders()

    # ------------------------------------------------------------------
    # Step 2 — Shader pack selection
    # ------------------------------------------------------------------

    def _show_step_shaders(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 2: Select Shader Packs",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 4))

        ctk.CTkLabel(
            self._body,
            text="The official ReShade shaders are always included.\nSelect any additional packs to download:",
            font=FONT_SMALL, text_color=TEXT_DIM, justify="center",
        ).pack(pady=(0, 8))

        # --- Optional preset input ------------------------------------------
        preset_box = ctk.CTkFrame(self._body, fg_color=BG_PANEL, corner_radius=6)
        preset_box.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(
            preset_box, text="Install from a preset (optional)",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(anchor="w", padx=12, pady=(10, 0))
        ctk.CTkLabel(
            preset_box,
            text=("Pick a ReShade preset (.ini) to install only the effects it\n"
                  "uses. All packs are downloaded then trimmed to the preset."),
            font=FONT_SMALL, text_color=TEXT_DIM, justify="left", anchor="w",
        ).pack(anchor="w", padx=12, pady=(0, 6))

        prow = ctk.CTkFrame(preset_box, fg_color="transparent")
        prow.pack(fill="x", padx=12, pady=(0, 10))
        self._preset_label = ctk.CTkLabel(
            prow,
            text=(self._preset_path.name if self._preset_path else "No preset selected"),
            font=FONT_SMALL,
            text_color=(TEXT_OK if self._preset_path else TEXT_DIM),
            anchor="w",
        )
        self._preset_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            prow, text="Browse\u2026", width=90, height=30, font=FONT_BOLD,
            fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._browse_preset,
        ).pack(side="right")
        if self._preset_path:
            ctk.CTkButton(
                prow, text="Clear", width=70, height=30, font=FONT_BOLD,
                fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
                command=self._clear_preset,
            ).pack(side="right", padx=(0, 6))

        # --- Pack checkboxes (disabled when a preset is loaded) -------------
        packs_disabled = self._preset_path is not None
        scroll = ctk.CTkScrollableFrame(self._body, fg_color=BG_PANEL, corner_radius=6)
        scroll.pack(fill="both", expand=True, pady=(0, 12))

        if packs_disabled:
            ctk.CTkLabel(
                scroll,
                text=("A preset is loaded \u2014 every pack will be downloaded and\n"
                      "trimmed to the preset's effects."),
                font=FONT_SMALL, text_color=TEXT_DIM, justify="left",
            ).pack(anchor="w", padx=12, pady=8)
        else:
            for i, (label, _url, _sub) in enumerate(_OPTIONAL_SHADER_PACKS):
                ctk.CTkCheckBox(
                    scroll, text=label,
                    variable=self._shader_pack_vars[i],
                    font=FONT_NORMAL, text_color=TEXT_MAIN,
                    fg_color=ACCENT, hover_color=ACCENT_HOV, checkmark_color="white",
                ).pack(anchor="w", padx=12, pady=4)

        # Linux X11 (Tk 8.6) wheel notches don't reach CTkScrollableFrame \u2014
        # bind Button-4/5 so the pack list scrolls under the pointer.
        bind_scrollable_wheel(scroll)

        ctk.CTkButton(
            self._body, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._show_step_download,
        ).pack(side="bottom")

    def _browse_preset(self):
        from Utils.portal_filechooser import pick_preset_file
        pick_preset_file(
            "Select a ReShade preset",
            lambda p: self.after(0, lambda: self._on_preset_picked(p)),
        )

    def _on_preset_picked(self, preset: "Path | None"):
        if not preset:
            return
        wanted = parse_preset_effect_files(preset)
        if not wanted:
            self._preset_path = None
            self._preset_wanted = set()
            self._log(
                f"ReShade wizard: '{preset.name}' has no Techniques= line \u2014 "
                "not a usable preset, ignoring."
            )
        else:
            self._preset_path = preset
            self._preset_wanted = wanted
            self._log(
                f"ReShade wizard: preset '{preset.name}' selected \u2014 "
                f"{len(wanted)} effect file(s) required."
            )
        self._show_step_shaders()

    def _clear_preset(self):
        self._preset_path = None
        self._preset_wanted = set()
        self._preset_missing = set()
        self._show_step_shaders()

    # ------------------------------------------------------------------
    # Step 3 — Download ReShade
    # ------------------------------------------------------------------

    def _show_step_download(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 3: Download ReShade",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        self._dl_status = ctk.CTkLabel(
            self._body,
            text="Fetching latest ReShade version\u2026",
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        )
        self._dl_status.pack(pady=(0, 16))

        self._progress = ctk.CTkProgressBar(self._body, mode="indeterminate", width=340)
        self._progress.pack(pady=(0, 16))
        self._progress.start()

        self._dl_next_btn = ctk.CTkButton(
            self._body, text="Next \u2192", width=120, height=36,
            font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._show_step_d3dcompiler, state="disabled",
        )
        self._dl_next_btn.pack(side="bottom")

        threading.Thread(target=self._do_download, daemon=True).start()

    def _do_download(self):
        try:
            self._set_dl_status("Downloading ReShade and shaders\u2026")
            from Utils.config_paths import get_config_dir
            tmp_path = get_config_dir() / "download_cache" / "reshade"
            tmp_path.mkdir(parents=True, exist_ok=True)

            dll_exc: list[Exception] = []
            shaders_exc: list[Exception] = []
            dll_result: list[Path] = []
            shaders_result: list[Path] = []

            arch = self._reshade_arch

            def _get_dll():
                try:
                    dll_result.append(_download_and_extract_reshade_dll(tmp_path, arch))
                except Exception as e:
                    dll_exc.append(e)

            # With a preset, download every pack and trim afterwards;
            # otherwise only the packs the user ticked.
            if self._preset_path is not None:
                selected_packs = list(_OPTIONAL_SHADER_PACKS)
            else:
                selected_packs = [
                    pack for pack, var in zip(_OPTIONAL_SHADER_PACKS, self._shader_pack_vars)
                    if var.get()
                ]

            def _get_shaders():
                try:
                    shaders_result.append(_download_and_extract_shaders(tmp_path, selected_packs))
                except Exception as e:
                    shaders_exc.append(e)

            t1 = threading.Thread(target=_get_dll, daemon=True)
            t2 = threading.Thread(target=_get_shaders, daemon=True)
            t1.start(); t2.start()
            t1.join(); t2.join()

            if dll_exc:
                raise RuntimeError(f"ReShade DLL: {dll_exc[0]}")
            if shaders_exc:
                raise RuntimeError(f"Shaders: {shaders_exc[0]}")

            self._extracted_dll = dll_result[0]
            self._extracted_shaders = shaders_result[0]
            self._log(f"ReShade wizard: downloaded {self._extracted_dll.name} and shaders.")

            # Trim the assembled shader set down to the preset's effects.
            ok_msg = "Downloaded ReShade and shaders successfully."
            if self._preset_path is not None and self._preset_wanted:
                self._set_dl_status("Trimming shaders to preset…")
                self._preset_missing = prune_shaders_to_preset(
                    self._extracted_shaders, self._preset_wanted
                )
                kept = len(self._preset_wanted) - len(self._preset_missing)
                self._log(
                    f"ReShade wizard: kept {kept}/{len(self._preset_wanted)} "
                    f"preset effect(s); {len(self._preset_missing)} missing."
                )
                if self._preset_missing:
                    # Split into "obsolete/renamed upstream" (unfixable, not a
                    # bug) and effects that genuinely weren't in any pack.
                    obsolete = sorted(
                        m for m in self._preset_missing if m in _OBSOLETE_PRESET_EFFECTS
                    )
                    unavailable = sorted(
                        m for m in self._preset_missing if m not in _OBSOLETE_PRESET_EFFECTS
                    )
                    lines = [
                        f"Installed {kept} of {len(self._preset_wanted)} preset effects."
                    ]
                    if unavailable:
                        lines.append("Missing (not in any pack): " + ", ".join(unavailable))
                    if obsolete:
                        lines.append(
                            "Skipped (renamed/removed upstream, no pack provides these): "
                            + ", ".join(obsolete)
                        )
                    ok_msg = "\n".join(lines)
                    miss = ", ".join(sorted(self._preset_missing))
                    self._log(f"ReShade wizard: missing preset effects: {miss}")
                else:
                    ok_msg = f"Trimmed shaders to {kept} preset effect(s)."
            self._set_dl_status(ok_msg, color=TEXT_OK)
            self.after(0, lambda: [
                self._progress.stop(),
                self._progress.pack_forget(),
                self._dl_next_btn.configure(state="normal"),
            ])
        except Exception as exc:
            self._log(f"ReShade wizard: download failed: {exc}")
            self._set_dl_status(f"Download failed:\n{exc}\n\nCheck your internet connection and try again.", color=TEXT_ERR)
            self.after(0, lambda: [
                self._progress.stop(),
                self._progress.pack_forget(),
                self._dl_next_btn.configure(state="normal", text="Retry \u21ba",
                                             command=self._show_step_download),
            ])

    def _set_dl_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._dl_status.configure(text=text, text_color=color))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Step 4 — Install d3dcompiler_47
    # ------------------------------------------------------------------

    def _show_step_d3dcompiler(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 4: Install d3dcompiler_47",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        from Utils.steam_finder import game_steam_id
        steam_id = game_steam_id(self._game)
        prefix_path = getattr(self._game, "_prefix_path", None)
        has_prefix = bool(prefix_path) and Path(prefix_path).is_dir()
        has_steam_id = bool(steam_id)
        can_install = has_prefix or has_steam_id
        already_installed = has_prefix and is_dep_installed(Path(prefix_path), D3D_DEP_KEY)

        if already_installed:
            info = (
                "d3dcompiler_47 is already installed in this prefix.\n"
                "You can skip this step."
            )
            color = TEXT_OK
        elif not can_install:
            info = (
                "No Proton prefix or Steam ID is configured for this game —\n"
                "d3dcompiler_47 cannot be installed automatically. Install it\n"
                "manually via winecfg before running the game with ReShade."
            )
            color = TEXT_WARN
        else:
            info = (
                "d3dcompiler_47 will be installed into the Proton prefix for\n"
                "this game (via protontricks if available, otherwise bundled\n"
                "winetricks).\n\n"
                "This may take up to a minute."
            )
            color = TEXT_DIM

        ctk.CTkLabel(
            self._body, text=info,
            font=FONT_NORMAL, text_color=color, justify="center", wraplength=460,
        ).pack(pady=(0, 16))

        self._d3d_status = ctk.CTkLabel(
            self._body, text="", font=FONT_NORMAL, text_color=TEXT_DIM,
            justify="center", wraplength=460,
        )
        self._d3d_status.pack(pady=(0, 8))

        btn_row = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_row.pack(side="bottom", pady=(8, 0))

        skip_btn = ctk.CTkButton(
            btn_row, text="Skip", width=100, height=36,
            font=FONT_BOLD, fg_color=BG_HEADER, hover_color="#3d3d3d", text_color=TEXT_MAIN,
            command=self._show_step_install,
        )
        skip_btn.pack(side="left", padx=(0, 8))

        if already_installed:
            self._d3d_install_btn = ctk.CTkButton(
                btn_row, text="Next →", width=200, height=36,
                font=FONT_BOLD, fg_color="#2d7a2d", hover_color="#3a9e3a",
                text_color=TEXT_ON_ACCENT, command=self._show_step_install,
            )
        else:
            self._d3d_install_btn = ctk.CTkButton(
                btn_row, text="Install d3dcompiler_47", width=200, height=36,
                font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV,
                text_color=TEXT_ON_ACCENT, command=self._do_install_d3dcompiler,
                state="normal" if can_install else "disabled",
            )
        self._d3d_install_btn.pack(side="left")

    def _do_install_d3dcompiler(self):
        self._d3d_install_btn.configure(state="disabled", text="Installing\u2026")
        from Utils.steam_finder import game_steam_id
        steam_id = game_steam_id(self._game)

        prefix = getattr(self._game, "_prefix_path", None)

        def _run():
            ok = install_d3dcompiler_47(
                steam_id,
                log_fn=lambda msg: self._set_d3d_status(msg),
                prefix_path=prefix,
            )
            color = TEXT_OK if ok else TEXT_ERR
            self._set_d3d_status(
                "d3dcompiler_47 installed successfully.\nClick Next to continue." if ok
                else "Install failed — you can Skip and install it manually.",
                color=color,
            )
            self.after(0, lambda: self._d3d_install_btn.configure(
                state="normal",
                text="Next \u2192" if ok else "Retry",
                fg_color=("#2d7a2d" if ok else ACCENT),
                hover_color=("#3a9e3a" if ok else ACCENT_HOV),
                command=self._show_step_install if ok else self._do_install_d3dcompiler,
            ))

        threading.Thread(target=_run, daemon=True).start()

    def _set_d3d_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._d3d_status.configure(text=text, text_color=color))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Step 5 — Install files
    # ------------------------------------------------------------------

    def _show_step_install(self):
        self._clear_body()

        ctk.CTkLabel(
            self._body, text="Step 5: Install ReShade",
            font=FONT_BOLD, text_color=TEXT_MAIN,
        ).pack(pady=(0, 12))

        info = (
            f"ReShade will be installed as  {self._reshade_dll}\n"
            f"and the Wine DLL override  {self._override_key}=native,builtin\n"
            f"will be written to the Proton prefix."
        )
        ctk.CTkLabel(
            self._body, text=info,
            font=FONT_NORMAL, text_color=TEXT_DIM, justify="center", wraplength=460,
        ).pack(pady=(0, 12))

        dest_box = ctk.CTkFrame(self._body, fg_color=BG_PANEL, corner_radius=6)
        dest_box.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            dest_box, text="Install destination",
            font=FONT_BOLD, text_color=TEXT_MAIN, anchor="w",
        ).pack(anchor="w", padx=12, pady=(10, 4))

        ctk.CTkRadioButton(
            dest_box, text="Game folder", value="game", variable=self._install_dest,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            command=self._sync_mod_name_state,
        ).pack(anchor="w", padx=12, pady=2)

        ctk.CTkRadioButton(
            dest_box, text="Root_Folder (staging)", value="root_folder", variable=self._install_dest,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            command=self._sync_mod_name_state,
        ).pack(anchor="w", padx=12, pady=2)

        ctk.CTkRadioButton(
            dest_box, text="As a managed mod (root-flagged)", value="mod", variable=self._install_dest,
            font=FONT_NORMAL, text_color=TEXT_MAIN,
            fg_color=ACCENT, hover_color=ACCENT_HOV,
            command=self._sync_mod_name_state,
        ).pack(anchor="w", padx=12, pady=2)

        # Default the mod name to "<preset> - ReShade" when a preset is in use
        # so installing several presets gives distinct, self-describing mods.
        # Only set it while the field is still untouched (the plain "ReShade"
        # default) so a name the user already typed is preserved on re-entry.
        if (
            self._preset_path is not None
            and not self._mod_name_edited
            and self._mod_name_var.get().strip() in ("", "ReShade")
        ):
            preset_label = self._preset_path.stem.replace("_", " ").strip()
            if preset_label:
                self._suppress_mod_name_trace = True
                self._mod_name_var.set(f"{preset_label} - ReShade")
                self._suppress_mod_name_trace = False

        mod_row = ctk.CTkFrame(dest_box, fg_color="transparent")
        mod_row.pack(fill="x", padx=12, pady=(2, 10))
        ctk.CTkLabel(
            mod_row, text="Mod name:", font=FONT_SMALL, text_color=TEXT_DIM,
        ).pack(side="left", padx=(24, 6))
        self._mod_name_entry = ctk.CTkEntry(
            mod_row, textvariable=self._mod_name_var, width=240,
            font=FONT_NORMAL, fg_color=BG_DEEP, text_color=TEXT_MAIN,
        )
        self._mod_name_entry.pack(side="left")
        self._sync_mod_name_state()

        self._install_status = ctk.CTkLabel(
            self._body, text="", font=FONT_NORMAL, text_color=TEXT_DIM,
            justify="center", wraplength=460,
        )
        self._install_status.pack(pady=(0, 8))

        btn_row = ctk.CTkFrame(self._body, fg_color="transparent")
        btn_row.pack(side="bottom", pady=(8, 0))

        self._done_btn = ctk.CTkButton(
            btn_row, text="Done", width=100, height=36,
            font=FONT_BOLD, fg_color="#2d7a2d", hover_color="#3a9e3a", text_color="white",
            command=self._finish, state="disabled",
        )
        self._done_btn.pack(side="right", padx=(8, 0))

        self._do_install_btn = ctk.CTkButton(
            btn_row, text="Install", width=120, height=36,
            font=FONT_BOLD, fg_color=ACCENT, hover_color=ACCENT_HOV, text_color=TEXT_ON_ACCENT,
            command=self._do_install,
        )
        self._do_install_btn.pack(side="right")

    def _on_mod_name_typed(self, *_):
        """Mark the mod-name field as user-edited (unless we set it ourselves),
        so the preset-derived default stops overriding it."""
        if not self._suppress_mod_name_trace:
            self._mod_name_edited = True

    def _sync_mod_name_state(self):
        """Enable the mod-name entry only when installing as a managed mod."""
        try:
            state = "normal" if self._install_dest.get() == "mod" else "disabled"
            self._mod_name_entry.configure(state=state)
        except Exception:
            pass

    def _do_install(self):
        self._do_install_btn.configure(state="disabled", text="Installing\u2026")

        def _run():
            try:
                dest = self._install_dest.get()

                # ReShade must live next to the rendering exe.  Ask the game
                # handler for the install subdir relative to the game root
                # (e.g. "bin/x64" for Cyberpunk, "<Project>/Binaries/Win64" for
                # UE5).  This subdir is mirrored inside Root_Folder / mod staging
                # too, since those deploy verbatim to the game root.
                game_root = self._game.get_game_path()
                try:
                    exe_subdir = self._game.reshade_install_subdir(game_root)
                except Exception as exc:
                    self._log(f"ReShade wizard: reshade_install_subdir failed ({exc}); using game root.")
                    exe_subdir = None
                if exe_subdir is not None:
                    self._log(f"ReShade wizard: install subdir resolved to '{exe_subdir}'.")

                mod_dir: "Path | None" = None
                if dest == "mod":
                    mod_name = self._mod_name_var.get().strip() or "ReShade"
                    from wizards._install_as_mod import register_as_mod
                    # Creates staging/<mod>/, writes meta.ini (rootFolder=true),
                    # prepends to modlist and refreshes the panel.
                    mod_dir = register_as_mod(
                        self._game, mod_name, None,
                        parent_widget=self, log_fn=self._log,
                        root_folder=True,
                    )
                    # Root-flagged mods deploy verbatim to game root, so mirror
                    # the exe subdir inside the mod folder.
                    base_dir = mod_dir
                    dest_label = f"managed mod \u201c{mod_name}\u201d (root-flagged)"
                elif dest == "root_folder":
                    base_dir = self._game.get_effective_root_folder_path()
                    base_dir.mkdir(parents=True, exist_ok=True)
                    dest_label = "Root_Folder (staging)"
                else:
                    base_dir = self._game.get_game_path()
                    if base_dir is None:
                        raise RuntimeError("Game path is not configured.")
                    dest_label = "game folder"

                dest_dir = base_dir / exe_subdir if exe_subdir else base_dir
                dest_dir.mkdir(parents=True, exist_ok=True)
                if exe_subdir:
                    dest_label += f" (under {exe_subdir})"

                dll_src = self._extracted_dll
                if dll_src is None or not dll_src.is_file():
                    raise RuntimeError("ReShade DLL not found — please restart the wizard.")

                # 1. Copy the ReShade DLL renamed to the game's override name
                shutil.copy2(str(dll_src), str(dest_dir / self._reshade_dll))
                self._log(f"ReShade wizard: copied {dll_src.name} → {self._reshade_dll}")

                # 1b. For managed installs (mod / root_folder), seed an empty
                #     ReShade.log next to the DLL.  ReShade writes this log at
                #     runtime; seeding it in staging means it's a tracked,
                #     deployed file and gets cleaned up properly on restore
                #     instead of being left behind in the game folder.
                if dest in ("mod", "root_folder"):
                    (dest_dir / "ReShade.log").touch()
                    self._log("ReShade wizard: created empty ReShade.log")

                # 2. Copy bundled ReShade.ini and the preset (or a blank one).
                #    A user-supplied preset keeps its own (Wine-safe) filename so
                #    it's recognisable in-game; ReShade.ini's PresetPath is
                #    patched to match.  A blank preset uses the default name.
                if self._preset_path is not None and self._preset_path.is_file():
                    preset_name = sanitize_mod_folder_name(self._preset_path.stem) + ".ini"
                else:
                    preset_name = "ReShadePreset.ini"

                src_ini = Path(__file__).parent / "ReShade.ini"
                if src_ini.is_file():
                    shutil.copy2(str(src_ini), str(dest_dir / "ReShade.ini"))
                    if preset_name != "ReShadePreset.ini":
                        _set_preset_path_in_ini(dest_dir / "ReShade.ini", preset_name)
                    self._log("ReShade wizard: copied ReShade.ini")
                if self._preset_path is not None and self._preset_path.is_file():
                    shutil.copy2(str(self._preset_path), str(dest_dir / preset_name))
                    self._log(f"ReShade wizard: installed preset {self._preset_path.name} as {preset_name}")
                else:
                    (dest_dir / preset_name).touch()
                    self._log(f"ReShade wizard: created {preset_name}")

                # 3. Copy reshade-shaders/ directly into dest_dir
                shaders_src = self._extracted_shaders
                if shaders_src is None or not shaders_src.is_dir():
                    raise RuntimeError("Shader files not found — please restart the wizard.")
                shaders_dest = dest_dir / "reshade-shaders"
                if shaders_dest.exists():
                    shutil.rmtree(str(shaders_dest))
                shutil.copytree(str(shaders_src), str(shaders_dest))
                self._log("ReShade wizard: copied reshade-shaders/")

                # 4. Apply Wine DLL override to the Proton prefix
                prefix = getattr(self._game, "_prefix_path", None)
                if prefix and Path(prefix).is_dir():
                    apply_wine_dll_overrides(
                        Path(prefix),
                        {self._override_key: "native,builtin"},
                        log_fn=self._log,
                    )
                    self._log(f"ReShade wizard: applied Wine override {self._override_key}=native,builtin")
                    override_note = f"\u2713 Wine override {self._override_key}=native,builtin applied."
                else:
                    override_note = (
                        f"\u26a0 Could not apply Wine override automatically.\n"
                        f"Add to Steam launch options:\n"
                        f'WINEDLLOVERRIDES="{self._override_key}=native,builtin" %command%'
                    )

                deploy_note = (
                    "\nDeploy your mods to copy ReShade into the game folder.\n"
                    if dest in ("mod", "root_folder") else ""
                )
                self._set_install_status(
                    f"\u2713 ReShade installed to {dest_label}.\n"
                    f"{override_note}\n"
                    f"{deploy_note}\n"
                    "Click Done to close.",
                    color=TEXT_OK,
                )
                self._log("ReShade wizard: installation complete.")
                self._cleanup_tmp()
                self.after(0, lambda: self._done_btn.configure(state="normal"))

            except Exception as exc:
                self._log(f"ReShade wizard error: {exc}")
                self._set_install_status(f"Error: {exc}", color=TEXT_ERR)
                self.after(0, lambda: self._do_install_btn.configure(
                    state="normal", text="Retry",
                ))

        threading.Thread(target=_run, daemon=True).start()

    def _set_install_status(self, text: str, color: str = TEXT_DIM):
        try:
            self.after(0, lambda: self._install_status.configure(text=text, text_color=color))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Finish
    # ------------------------------------------------------------------

    def _finish(self):
        self._cleanup_tmp()
        self._log("ReShade wizard: closed.")
        self._on_close_cb()

"""Define-Custom-Game view — Qt port of gui/custom_game_dialog.CustomGamePanel.

Opens as a (detachable) tab. Lets the user define a brand-new game handler from
a JSON definition (name, exe, deploy type, mod sub-folder, Steam/Nexus IDs,
banner image, advanced mod-handling options, custom routing rules, framework
detection). On save the definition is written via the toolkit-neutral backend
``Games/Custom/custom_game.py`` and the caller chains to the Configure-Game tab
so the user can set the install path/prefix.

Supports both create and edit: pass an ``existing`` definition dict to
prepopulate + show a Delete button.
"""

from __future__ import annotations

import io
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QFrame, QRadioButton, QCheckBox, QButtonGroup, QComboBox,
    QPlainTextEdit,
)

from gui_qt.theme_qt import active_palette, _c
from gui_qt.icons import icon, icon_rotated
from gui_qt.safe_emit import safe_emit
from Games.Custom.custom_game import (
    _make_game_id,
    delete_custom_game_definition,
    make_custom_game,
    save_custom_game_definition,
)
from Utils.config_paths import get_custom_game_images_dir


# Deploy-type radios: (label, value, description) — mirrors the Tk dialog.
_DEPLOY_OPTIONS = [
    ("Standard", "standard",
     "Mods install into a single sub-folder (e.g. Data/, BepInEx/plugins/). "
     "Same as Bethesda games and BepInEx."),
    ("Root", "root",
     "Mods deploy directly to the game's root folder. "
     "Same as The Witcher 3 and Cyberpunk 2077."),
    ("UE5", "ue5",
     "Unreal Engine 5 — pak files → Content/Paks/~mods/, UE4SS/lua → "
     "Binaries/Win64/, DLLs → Binaries/Win64/. Same routing as Hogwarts "
     "Legacy / Oblivion Remastered."),
]

# Display-label ↔ stored-value mapping for the filemap_casing dropdown.
_FILEMAP_CASING_OPTIONS = [
    ("Most uppercase",       "upper"),
    ("Most lowercase",       "lower"),
    ("Lowercase everything", "force_lower"),
    ("Uppercase everything", "force_upper"),
]
_FILEMAP_CASING_LABEL_BY_VALUE = {v: lbl for lbl, v in _FILEMAP_CASING_OPTIONS}
_FILEMAP_CASING_VALUE_BY_LABEL = {lbl: v for lbl, v in _FILEMAP_CASING_OPTIONS}


# --- dialog ↔ JSON value helpers (ported from custom_game_dialog.py) -------

def _set_to_str(value) -> str:
    if isinstance(value, (list, set)):
        return ", ".join(str(v) for v in value)
    return str(value) if value else ""


def _dll_to_str(value) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{k}={v}" for k, v in value.items())
    return str(value) if value else ""


def _str_to_list(text: str) -> list[str]:
    return [s.strip() for s in text.split(",") if s.strip()]


def _parse_dll_text(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip():
                result[k.strip()] = v.strip()
    return result


class _ImageSignals(QObject):
    status = Signal(str, str)   # (text, tone_key)


class CustomGameView(QWidget):
    """*on_done(saved_defn: dict | None, deleted: bool)* is called after
    Save/Delete/Cancel so the window can refresh the game list and close the
    tab. ``saved_defn`` is None on cancel/delete."""

    _TYPE_COMBO_W = 110   # fixed width of the routing match-type combo

    def __init__(self, on_done, existing: dict | None = None, parent=None):
        super().__init__(parent)
        self._on_done = on_done or (lambda saved, deleted: None)
        self._existing = existing
        self._p = active_palette()

        # Dynamic-table row state.
        self._routing_rows: list[dict] = []
        self._framework_rows: list[dict] = []

        self._img_sig = _ImageSignals()
        self._img_sig.status.connect(self._set_image_status)

        self._build()
        self._prepopulate()
        self._update_data_path_visibility()

    # ---- styling helpers --------------------------------------------------
    def _c(self, k):
        return _c(self._p, k)

    def _section_header(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-size:14px; font-weight:600; color:{self._c('TEXT_SEP')};")
        return lbl

    def _hint(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
        return lbl

    def _mono_edit(self, placeholder: str = "") -> QLineEdit:
        e = QLineEdit()
        e.setObjectName("PathEdit")
        f = QFont("monospace"); f.setStyleHint(QFont.Monospace); e.setFont(f)
        if placeholder:
            e.setPlaceholderText(placeholder)
        return e

    def _divider(self) -> QFrame:
        f = QFrame(); f.setFrameShape(QFrame.HLine)
        f.setStyleSheet(f"color:{self._c('BORDER')};")
        return f

    # ---- build ------------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Title bar.
        header = QWidget(); header.setObjectName("HeaderBar")
        hb = QHBoxLayout(header); hb.setContentsMargins(12, 8, 12, 8)
        title = QLabel("Edit Custom Game" if self._existing else "Define Custom Game")
        title.setStyleSheet("font-size:15px; font-weight:600;")
        hb.addWidget(title); hb.addStretch(1)
        outer.addWidget(header)

        # Scrollable body.
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget(); body.setObjectName("FormBody")
        v = QVBoxLayout(body); v.setContentsMargins(16, 14, 16, 14); v.setSpacing(6)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # --- Game Name ---
        v.addWidget(self._section_header("Game Name"))
        v.addWidget(self._hint("The display name shown in the game selector "
                               "(must be unique)."))
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. My Favourite Game")
        v.addWidget(self._name_edit)
        v.addWidget(self._divider())

        # --- Executable Filename ---
        v.addWidget(self._section_header("Executable Filename"))
        v.addWidget(self._hint(
            "The .exe location from the game's root folder. e.g. bin/bg3.exe "
            "for BG3 or SkyrimSELauncher.exe for Skyrim SE"))
        self._exe_edit = self._mono_edit("e.g. MyGame.exe")
        v.addWidget(self._exe_edit)
        v.addWidget(self._divider())

        # --- Deploy Method ---
        v.addWidget(self._section_header("Deploy Method"))
        self._deploy_group = QButtonGroup(self)
        self._deploy_buttons: dict[str, QRadioButton] = {}
        for label, value, desc in _DEPLOY_OPTIONS:
            rb = QRadioButton(label)
            rb.setStyleSheet("font-weight:600;")
            self._deploy_group.addButton(rb)
            self._deploy_buttons[value] = rb
            rb.toggled.connect(self._update_data_path_visibility)
            v.addWidget(rb)
            d = self._hint(desc)
            d.setContentsMargins(20, 0, 0, 4)
            v.addWidget(d)
        self._deploy_buttons["standard"].setChecked(True)
        v.addWidget(self._divider())

        # --- Mod Sub-folder (standard / ue5; disabled for root) ---
        self._dp_header = self._section_header("Mod Sub-folder")
        v.addWidget(self._dp_header)
        self._dp_hint = self._hint("")
        v.addWidget(self._dp_hint)
        self._data_path_edit = self._mono_edit()
        v.addWidget(self._data_path_edit)
        v.addWidget(self._divider())

        # --- Steam App ID ---
        v.addWidget(self._section_header("Steam App ID  (optional)"))
        v.addWidget(self._hint("Used to auto-detect the Proton prefix. Leave "
                               "empty if not on Steam."))
        self._steam_edit = self._mono_edit("e.g. 377160")
        v.addWidget(self._steam_edit)
        v.addWidget(self._divider())

        # --- Nexus domain ---
        v.addWidget(self._section_header("Nexus Mods Domain  (optional)"))
        v.addWidget(self._hint("The game's slug on nexusmods.com. "
                               "e.g. 'skyrimspecialedition'."))
        self._nexus_edit = self._mono_edit("e.g. myfavouritegame")
        v.addWidget(self._nexus_edit)
        v.addWidget(self._divider())

        # --- Banner image URL ---
        v.addWidget(self._section_header("Banner Image URL  (optional)"))
        v.addWidget(self._hint(
            "A direct URL to a PNG/JPG image shown in the game picker card. "
            "The image is downloaded once and cached locally."))
        self._image_edit = self._mono_edit("https://example.com/banner.jpg")
        v.addWidget(self._image_edit)
        self._image_status = QLabel("")
        self._image_status.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
        v.addWidget(self._image_status)
        v.addWidget(self._divider())

        # --- Advanced options ---
        v.addWidget(self._section_header("Advanced Options  (optional)"))
        v.addWidget(self._hint(
            "Used to change the folder structure of an installed mod to match "
            "what is required by the manager."))

        # (key, label, hint) — same order as the Tk _adv_fields list.
        _adv_fields = [
            ("mod_folder_strip_prefixes", "Strip Prefixes",
             "Comma-separated top-level folder names to strip from mod files "
             "during filemap building (case-insensitive). e.g. Data, data"),
            ("mod_install_prefix", "Prepend Prefix",
             "Path segment prepended to every installed file. "
             "e.g. 'mods' so files land at mods/<ModName>/…"),
            ("mod_required_top_level_folders", "Required Top-Level Folders",
             "Comma-separated folder names a mod must contain at its root. "
             "If none match, the user is prompted to set a data directory."),
            ("mod_required_file_types", "Required File Types",
             "Comma-separated file extensions a mod must contain at its root. "
             "e.g. .esp, .esm — works standalone or as a fallback after "
             "Required Top-Level Folders."),
            ("mod_folder_strip_prefixes_post", "Strip Prefixes (post-install)",
             "Like Strip Prefixes but applied after Required Top-Level Folders "
             "validation. e.g. reframework"),
            ("conflict_ignore_filenames", "Conflict Ignore Filenames",
             "Comma-separated filenames excluded from conflict detection. "
             "Supports glob patterns: *.<ext> matches any file with that "
             "extension, <name>.* matches that name with any extension. "
             "e.g. modinfo.ini, manifest.json, *.txt, LICENCE.*"),
        ]
        self._adv_edits: dict[str, QLineEdit] = {}

        def _render_entry(key, label, hint):
            lbl = QLabel(label); lbl.setStyleSheet("font-weight:600;")
            v.addWidget(lbl)
            v.addWidget(self._hint(hint))
            e = self._mono_edit()
            v.addWidget(e)
            self._adv_edits[key] = e

        self._adv_toggles: dict[str, QCheckBox] = {}

        def _render_toggle(key, label, hint, default=False):
            lbl = QLabel(label); lbl.setStyleSheet("font-weight:600;")
            v.addWidget(lbl)
            v.addWidget(self._hint(hint))
            cb = QCheckBox("Enable")
            cb.setChecked(default)
            v.addWidget(cb)
            self._adv_toggles[key] = cb

        for key, label, hint in _adv_fields:
            _render_entry(key, label, hint)
            # The two Required-Top-Level decision toggles render right after
            # Required File Types (matches the install-pipeline ordering).
            if key == "mod_required_file_types":
                _render_toggle(
                    "mod_auto_strip_until_required", "Auto Strip Until Required",
                    "When enabled and Required Top-Level Folders is set, strip "
                    "leading path segments automatically instead of prompting "
                    "the user.")
                _render_toggle(
                    "mod_install_as_is_if_no_match", "Install As-Is If No Match",
                    "When enabled, if both Required Top-Level Folders and "
                    "Required File Types checks fail, the mod is installed "
                    "as-is without showing the prefix dialog.")

        _render_toggle(
            "restore_before_deploy", "Restore Before Deploy",
            "When enabled (default), the manager runs Restore before every "
            "Deploy to clean the game state first. Disable only if the game's "
            "deploy cycle handles its own cleanup internally.", default=True)
        _render_toggle(
            "normalize_folder_case", "Normalize Folder Case",
            "When enabled (default), folder names that differ only in case "
            "across mods are unified to a single casing. Disable for "
            "Linux-native games where folder casing is significant.",
            default=True)

        # Filemap casing strategy.
        lbl = QLabel("Filemap Casing"); lbl.setStyleSheet("font-weight:600;")
        v.addWidget(lbl)
        v.addWidget(self._hint(
            "How to pick canonical folder casing when mods disagree. "
            "Only used when Normalize Folder Case is enabled."))
        self._casing_combo = QComboBox()
        for label, _val in _FILEMAP_CASING_OPTIONS:
            self._casing_combo.addItem(label)
        v.addWidget(self._casing_combo)

        # Wine DLL overrides (multi-line).
        lbl = QLabel("Wine DLL Overrides"); lbl.setStyleSheet("font-weight:600;")
        v.addWidget(lbl)
        v.addWidget(self._hint("One override per line: dll_name=load_order  "
                               "e.g. winhttp=native,builtin"))
        self._dll_edit = QPlainTextEdit()
        self._dll_edit.setFixedHeight(72)
        f = QFont("monospace"); f.setStyleHint(QFont.Monospace); self._dll_edit.setFont(f)
        v.addWidget(self._dll_edit)
        v.addWidget(self._divider())

        # --- Custom Routing Rules ---
        v.addWidget(self._section_header("Custom Routing Rules"))
        v.addWidget(self._hint(
            "Route specific files to alternate destinations during deploy. "
            "Each rule maps files (by extension, folder or filename) to a "
            "game-root-relative directory. For extensions, append (.ext, .ext) "
            "to also route same-stem siblings (e.g. .asi (.ini) sends Foo.ini "
            "alongside Foo.asi). Flatten drops subfolders below the matched "
            "folder. To Prefix routes relative to the Proton/Wine prefix root "
            "instead of the game install root."))
        add_rule = QPushButton("+ Add Rule")
        add_rule.setObjectName("FormButton")
        add_rule.setCursor(Qt.PointingHandCursor)
        add_rule.clicked.connect(lambda: self._add_routing_rule())
        v.addWidget(add_rule, alignment=Qt.AlignLeft)
        self._routing_container = QWidget()
        self._routing_vbox = QVBoxLayout(self._routing_container)
        self._routing_vbox.setContentsMargins(0, 0, 0, 0)
        self._routing_vbox.setSpacing(2)
        # Column headers over the dest / match-value inputs — shown only while
        # at least one rule row exists. Stretch factors mirror the row layout.
        self._routing_header = QWidget()
        rh = QHBoxLayout(self._routing_header)
        rh.setContentsMargins(4, 0, 4, 0); rh.setSpacing(4)
        rh.addSpacing(24 + 4)               # up/down button column
        dest_lbl = QLabel("Destination")
        dest_lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
        file_lbl = QLabel("File/folder")
        file_lbl.setStyleSheet(f"color:{self._c('TEXT_DIM')};")
        rh.addWidget(dest_lbl, 1)
        rh.addSpacing(self._TYPE_COMBO_W)   # match-type combo column
        rh.addWidget(file_lbl, 1)
        rh.addStretch(0)
        self._routing_header.setVisible(False)
        v.addWidget(self._routing_header)
        v.addWidget(self._routing_container)
        v.addWidget(self._divider())

        # --- Framework Detection ---
        v.addWidget(self._section_header("Framework Detection"))
        v.addWidget(self._hint(
            "Display a status banner in the Plugins tab when a framework is "
            "installed. Enter the framework name on the left and its file path "
            "relative to the game root on the right."))
        add_fw = QPushButton("+ Add Framework")
        add_fw.setObjectName("FormButton")
        add_fw.setCursor(Qt.PointingHandCursor)
        add_fw.clicked.connect(lambda: self._add_framework())
        v.addWidget(add_fw, alignment=Qt.AlignLeft)
        self._framework_container = QWidget()
        self._framework_vbox = QVBoxLayout(self._framework_container)
        self._framework_vbox.setContentsMargins(0, 0, 0, 0)
        self._framework_vbox.setSpacing(2)
        v.addWidget(self._framework_container)

        # Validation label.
        self._validation = QLabel("")
        self._validation.setWordWrap(True)
        self._validation.setStyleSheet(f"color:{self._c('TEXT_ERR')};")
        v.addWidget(self._validation)

        v.addStretch(1)

        # --- Button bar ---
        bar = QWidget(); bar.setObjectName("BottomBar")
        bb = QHBoxLayout(bar); bb.setContentsMargins(12, 8, 12, 8)
        if self._existing:
            del_btn = QPushButton("Delete")
            del_btn.setObjectName("DangerButton")
            del_btn.setCursor(Qt.PointingHandCursor)
            del_btn.clicked.connect(self._on_delete)
            bb.addWidget(del_btn)
        bb.addStretch(1)
        self._save_btn = QPushButton("Save Game")
        self._save_btn.setObjectName("PrimaryButton")
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.clicked.connect(self._on_save)
        bb.addWidget(self._save_btn)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("FormButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(lambda: self._on_done(None, False))
        bb.addWidget(cancel)
        outer.addWidget(bar)

    # ---- deploy-type-dependent sub-folder row -----------------------------
    def _update_data_path_visibility(self, *_):
        # The deploy radios' toggled signal can fire during _build() before the
        # sub-folder widgets exist; ignore until they're created.
        if not hasattr(self, "_dp_header"):
            return
        deploy = self._current_deploy_type()
        enabled = deploy != "root"
        self._dp_header.setEnabled(enabled)
        self._dp_hint.setEnabled(enabled)
        self._data_path_edit.setEnabled(enabled)
        if deploy == "ue5":
            self._dp_header.setText("Game Sub-folder  (optional)")
            self._dp_hint.setText(
                "Location of the folder from root where deployed mods are sent "
                "to. e.g. Phoenix for Hogwarts Legacy.")
            self._data_path_edit.setPlaceholderText("e.g. OblivionRemastered")
        else:
            self._dp_header.setText("Mod Sub-folder")
            self._dp_hint.setText(
                "Path relative to the game root where mod files are installed. "
                "e.g. 'Data' for Bethesda games, 'BepInEx/plugins' for BepInEx. "
                "Leave empty to target the game root directly.")
            self._data_path_edit.setPlaceholderText(
                "e.g. Data   (leave empty for game root)")

    def _current_deploy_type(self) -> str:
        for value, rb in self._deploy_buttons.items():
            if rb.isChecked():
                return value
        return "standard"

    # ---- routing-rule rows ------------------------------------------------
    def _add_routing_rule(self, dest="", match_type="extensions", match_value="",
                          loose_only=False, flatten=False, include_siblings=False,
                          to_prefix=False):
        row = QFrame(); row.setObjectName("RuleRow")
        row.setFrameShape(QFrame.StyledPanel)
        hb = QHBoxLayout(row); hb.setContentsMargins(4, 4, 4, 4); hb.setSpacing(4)

        up = QPushButton(); up.setFixedWidth(24)
        up.setIcon(icon_rotated("arrow.png", 180, 12, "#ffffff"))
        up.setToolTip("Move up")
        down = QPushButton(); down.setFixedWidth(24)
        down.setIcon(icon_rotated("arrow.png", 0, 12, "#ffffff"))
        down.setToolTip("Move down")

        dest_edit = self._mono_edit("Destination")
        type_combo = QComboBox()
        type_combo.addItems(["extensions", "folders", "filenames"])
        type_combo.setCurrentText(match_type)
        type_combo.setFixedWidth(self._TYPE_COMBO_W)
        value_edit = self._mono_edit("File/Folder")
        dest_edit.setText(dest)
        value_edit.setText(match_value)

        cb_loose = QCheckBox("Loose only"); cb_loose.setChecked(loose_only)
        cb_flat = QCheckBox("Flatten"); cb_flat.setChecked(flatten)
        cb_sib = QCheckBox("Include Siblings"); cb_sib.setChecked(include_siblings)
        cb_pfx = QCheckBox("To Prefix"); cb_pfx.setChecked(to_prefix)

        remove = QPushButton(); remove.setObjectName("DangerButton")
        remove.setIcon(icon("close_white.png", 12))
        remove.setToolTip("Remove rule")
        remove.setFixedWidth(28)

        vbtns = QVBoxLayout(); vbtns.setSpacing(0); vbtns.setContentsMargins(0, 0, 0, 0)
        vbtns.addWidget(up); vbtns.addWidget(down)
        hb.addLayout(vbtns)
        hb.addWidget(dest_edit, 1)
        hb.addWidget(type_combo)
        hb.addWidget(value_edit, 1)
        hb.addWidget(cb_loose); hb.addWidget(cb_flat)
        hb.addWidget(cb_sib); hb.addWidget(cb_pfx)
        hb.addWidget(remove)

        rd = {"frame": row, "dest": dest_edit, "type": type_combo,
              "value": value_edit, "loose_only": cb_loose, "flatten": cb_flat,
              "include_siblings": cb_sib, "to_prefix": cb_pfx}
        self._routing_rows.append(rd)
        self._routing_vbox.addWidget(row)
        self._routing_header.setVisible(True)

        up.clicked.connect(lambda: self._move_routing_rule(rd, -1))
        down.clicked.connect(lambda: self._move_routing_rule(rd, 1))
        remove.clicked.connect(lambda: self._remove_routing_rule(rd))

    def _remove_routing_rule(self, rd):
        if rd in self._routing_rows:
            self._routing_rows.remove(rd)
            self._routing_vbox.removeWidget(rd["frame"])
            rd["frame"].deleteLater()
            if not self._routing_rows:
                self._routing_header.setVisible(False)

    def _move_routing_rule(self, rd, delta):
        rows = self._routing_rows
        if rd not in rows:
            return
        i = rows.index(rd)
        j = i + delta
        if j < 0 or j >= len(rows):
            return
        rows[i], rows[j] = rows[j], rows[i]
        self._routing_vbox.removeWidget(rd["frame"])
        self._routing_vbox.insertWidget(j, rd["frame"])

    def _collect_routing_rules(self) -> list[dict]:
        rules = []
        for rd in self._routing_rows:
            dest = rd["dest"].text().strip()
            match_type = rd["type"].currentText()
            raw_value = rd["value"].text().strip()

            companions: list[str] = []
            if match_type == "extensions" and "(" in raw_value and ")" in raw_value:
                before, _, rest = raw_value.partition("(")
                inside, _, after = rest.partition(")")
                companions = [v.strip() for v in inside.split(",") if v.strip()]
                raw_value = (before + " " + after).strip().rstrip(",").strip()

            values = [v.strip() for v in raw_value.split(",") if v.strip()]
            if not values and not dest:
                continue
            rule: dict = {"dest": dest}
            if match_type == "extensions":
                rule["extensions"] = values
                if companions:
                    rule["companion_extensions"] = companions
            elif match_type == "filenames":
                rule["filenames"] = values
            else:
                rule["folders"] = values
            if rd["loose_only"].isChecked():
                rule["loose_only"] = True
            if rd["flatten"].isChecked():
                rule["flatten"] = True
            if rd["include_siblings"].isChecked():
                rule["include_siblings"] = True
            if rd["to_prefix"].isChecked():
                rule["to_prefix"] = True
            rules.append(rule)
        return rules

    # ---- framework rows ---------------------------------------------------
    def _add_framework(self, name="", path=""):
        row = QFrame(); row.setObjectName("FwRow")
        row.setFrameShape(QFrame.StyledPanel)
        hb = QHBoxLayout(row); hb.setContentsMargins(4, 4, 4, 4); hb.setSpacing(4)
        name_edit = self._mono_edit("e.g. Script Extender")
        path_edit = self._mono_edit("e.g. skse64_loader.exe")
        name_edit.setText(name); path_edit.setText(path)
        remove = QPushButton(); remove.setObjectName("DangerButton")
        remove.setIcon(icon("close_white.png", 12))
        remove.setToolTip("Remove framework")
        remove.setFixedWidth(28)
        hb.addWidget(name_edit, 1)
        hb.addWidget(path_edit, 2)
        hb.addWidget(remove)

        rd = {"frame": row, "name": name_edit, "path": path_edit}
        self._framework_rows.append(rd)
        self._framework_vbox.addWidget(row)
        remove.clicked.connect(lambda: self._remove_framework(rd))

    def _remove_framework(self, rd):
        if rd in self._framework_rows:
            self._framework_rows.remove(rd)
            self._framework_vbox.removeWidget(rd["frame"])
            rd["frame"].deleteLater()

    def _collect_frameworks(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for rd in self._framework_rows:
            name = rd["name"].text().strip()
            path = rd["path"].text().strip()
            if name and path:
                result[name] = path
        return result

    # ---- prepopulate (edit mode) ------------------------------------------
    def _prepopulate(self):
        e = self._existing
        if not e:
            return
        self._name_edit.setText(e.get("name", ""))
        self._exe_edit.setText(e.get("exe_name", ""))
        dep = e.get("deploy_type", "standard")
        if dep in self._deploy_buttons:
            self._deploy_buttons[dep].setChecked(True)
        self._data_path_edit.setText(e.get("mod_data_path", ""))
        self._steam_edit.setText(e.get("steam_id", ""))
        self._nexus_edit.setText(e.get("nexus_game_domain", ""))
        self._image_edit.setText(e.get("image_url", ""))

        self._adv_edits["mod_folder_strip_prefixes"].setText(
            _set_to_str(e.get("mod_folder_strip_prefixes", [])))
        self._adv_edits["mod_install_prefix"].setText(e.get("mod_install_prefix", ""))
        self._adv_edits["mod_required_top_level_folders"].setText(
            _set_to_str(e.get("mod_required_top_level_folders", [])))
        self._adv_edits["mod_required_file_types"].setText(
            _set_to_str(e.get("mod_required_file_types", [])))
        self._adv_edits["mod_folder_strip_prefixes_post"].setText(
            _set_to_str(e.get("mod_folder_strip_prefixes_post", [])))
        self._adv_edits["conflict_ignore_filenames"].setText(
            _set_to_str(e.get("conflict_ignore_filenames", [])))

        self._adv_toggles["mod_auto_strip_until_required"].setChecked(
            bool(e.get("mod_auto_strip_until_required", False)))
        self._adv_toggles["mod_install_as_is_if_no_match"].setChecked(
            bool(e.get("mod_install_as_is_if_no_match", False)))
        self._adv_toggles["restore_before_deploy"].setChecked(
            bool(e.get("restore_before_deploy", True)))
        self._adv_toggles["normalize_folder_case"].setChecked(
            bool(e.get("normalize_folder_case", True)))

        casing_label = _FILEMAP_CASING_LABEL_BY_VALUE.get(
            e.get("filemap_casing", "upper"), "Most uppercase")
        self._casing_combo.setCurrentText(casing_label)

        self._dll_edit.setPlainText(_dll_to_str(e.get("wine_dll_overrides", {})))

        for rule in e.get("custom_routing_rules", []) or []:
            if not isinstance(rule, dict):
                continue
            companions = rule.get("companion_extensions") or []
            if rule.get("filenames"):
                mt, mv = "filenames", ", ".join(rule["filenames"])
            elif rule.get("extensions"):
                mt = "extensions"
                mv = ", ".join(rule["extensions"])
                if companions:
                    mv = f"{mv} ({', '.join(companions)})"
            else:
                mt, mv = "folders", ", ".join(rule.get("folders") or [])
            self._add_routing_rule(
                dest=rule.get("dest", ""), match_type=mt, match_value=mv,
                loose_only=bool(rule.get("loose_only", False)),
                flatten=bool(rule.get("flatten", False)),
                include_siblings=bool(rule.get("include_siblings", False)),
                to_prefix=bool(rule.get("to_prefix", False)))

        fw = e.get("custom_frameworks", {})
        if isinstance(fw, dict):
            for name, path in fw.items():
                self._add_framework(name=name, path=path)

    # ---- validate ---------------------------------------------------------
    def _validate(self) -> str | None:
        name = self._name_edit.text().strip()
        exe = self._exe_edit.text().strip()
        if not name:
            return "Game Name is required."
        if not exe:
            return "Executable Filename is required."
        if len(name) > 120:
            return "Game Name is too long (max 120 characters)."
        return None

    # ---- banner image download (worker → signal) -------------------------
    def _set_image_status(self, text, tone):
        self._image_status.setText(text)
        self._image_status.setStyleSheet(f"color:{self._c(tone)};")

    def _download_image(self, url: str, game_id: str):
        def _worker():
            try:
                import requests
                from PIL import Image as PilImage
                safe_emit(self._img_sig.status, "Downloading image…", "TEXT_WARN")
                resp = requests.get(url, timeout=15)
                resp.raise_for_status()
                img = PilImage.open(io.BytesIO(resp.content)).convert("RGBA")
                out = get_custom_game_images_dir() / f"{game_id}.png"
                img.save(out, "PNG")
                safe_emit(self._img_sig.status, "Image cached.", "TEXT_OK")
            except Exception as exc:
                safe_emit(self._img_sig.status,
                          f"Image download failed: {exc}", "TEXT_ERR")

        threading.Thread(target=_worker, daemon=True).start()

    # ---- save / delete / cancel -------------------------------------------
    def _on_save(self):
        err = self._validate()
        if err:
            self._validation.setText(err)
            return
        self._validation.setText("")

        name = self._name_edit.text().strip()
        exe = self._exe_edit.text().strip()
        deploy = self._current_deploy_type()
        data_path = (self._data_path_edit.text().strip()
                     if deploy in ("standard", "ue5") else "")
        image_url = self._image_edit.text().strip()

        game_id = (self._existing.get("game_id") if self._existing else None) \
            or _make_game_id(name)

        defn = {
            "name":              name,
            "game_id":           game_id,
            "exe_name":          exe,
            "deploy_type":       deploy,
            "mod_data_path":     data_path,
            "steam_id":          self._steam_edit.text().strip(),
            "nexus_game_domain": self._nexus_edit.text().strip(),
            "image_url":         image_url,
            "mod_folder_strip_prefixes":
                _str_to_list(self._adv_edits["mod_folder_strip_prefixes"].text()),
            "conflict_ignore_filenames":
                _str_to_list(self._adv_edits["conflict_ignore_filenames"].text()),
            "mod_folder_strip_prefixes_post":
                _str_to_list(self._adv_edits["mod_folder_strip_prefixes_post"].text()),
            "mod_install_prefix":
                self._adv_edits["mod_install_prefix"].text().strip(),
            "mod_required_top_level_folders":
                _str_to_list(self._adv_edits["mod_required_top_level_folders"].text()),
            "mod_auto_strip_until_required":
                self._adv_toggles["mod_auto_strip_until_required"].isChecked(),
            "mod_required_file_types":
                _str_to_list(self._adv_edits["mod_required_file_types"].text()),
            "mod_install_as_is_if_no_match":
                self._adv_toggles["mod_install_as_is_if_no_match"].isChecked(),
            "restore_before_deploy":
                self._adv_toggles["restore_before_deploy"].isChecked(),
            "normalize_folder_case":
                self._adv_toggles["normalize_folder_case"].isChecked(),
            "filemap_casing":
                _FILEMAP_CASING_VALUE_BY_LABEL.get(
                    self._casing_combo.currentText(), "upper"),
            "wine_dll_overrides": _parse_dll_text(self._dll_edit.toPlainText()),
            "custom_routing_rules": self._collect_routing_rules(),
            "custom_frameworks": self._collect_frameworks(),
        }

        # Preserve repo-handler metadata when editing.
        if self._existing:
            for key in ("version", "editable"):
                if key in self._existing:
                    defn[key] = self._existing[key]

        save_custom_game_definition(defn)
        # Materialise the handler so any errors surface here, not later.
        make_custom_game(defn)

        if image_url:
            self._download_image(image_url, game_id)

        self._on_done(defn, False)

    def _on_delete(self):
        if self._existing is None:
            return
        game_id = self._existing.get("game_id", "")
        if game_id:
            delete_custom_game_definition(game_id)
            img = get_custom_game_images_dir() / f"{game_id}.png"
            img.unlink(missing_ok=True)
        self._on_done(None, True)

"""Game/profile state controller for the Qt UI.

Thin wrapper over the real (toolkit-neutral) helpers in gui.game_helpers and
Games.base_game so the Qt app drives the same load flow as the Tk app:
discover games, list profiles, switch the active game/profile, and resolve the
active modlist.txt + staging dir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from gui.game_helpers import (
    _load_games, _profiles_for_game, _load_last_game, _save_last_game, _GAMES,
)


@dataclass
class ConflictData:
    """Everything the modlist/plugins panels need to draw conflicts + cross-panel
    highlights. All maps key on mod name. *_codes are 1 win / -1 lose / 2 mixed.
    *_overrides[mod] = mods this mod beats; *_overridden_by[mod] = mods that beat
    it. plugin_owner maps a plugin filename (lower) → the mod that deploys it."""
    loose_codes: dict[str, int] = field(default_factory=dict)
    bsa_codes: dict[str, int] = field(default_factory=dict)
    overrides: dict[str, set] = field(default_factory=dict)
    overridden_by: dict[str, set] = field(default_factory=dict)
    bsa_overrides: dict[str, set] = field(default_factory=dict)
    bsa_overridden_by: dict[str, set] = field(default_factory=dict)
    plugin_owner: dict[str, str] = field(default_factory=dict)


class GameState:
    def __init__(self):
        self.game_names: list[str] = []
        self.game_name: str | None = None
        self.profile: str | None = None

    # -- discovery / load ---------------------------------------------------
    def load(self) -> None:
        """Discover games and select the last-used (or first) game + its default
        profile. Populates game_names / game_name / profile."""
        self.game_names = _load_games()
        last = _load_last_game()
        if last and last in self.game_names:
            self.game_name = last
        elif self.game_names and self.game_names[0] != "No games configured":
            self.game_name = self.game_names[0]
        else:
            self.game_name = None
        self._select_default_profile()
        self._apply_active_profile()

    # -- current handler ----------------------------------------------------
    @property
    def game(self):
        return _GAMES.get(self.game_name) if self.game_name else None

    def profiles(self) -> list[str]:
        return _profiles_for_game(self.game_name) if self.game_name else []

    # -- switching ----------------------------------------------------------
    def set_game(self, name: str) -> None:
        if name == self.game_name or name not in self.game_names:
            return
        self.game_name = name
        _save_last_game(name)
        self._select_default_profile()
        self._apply_active_profile()

    def set_profile(self, profile: str) -> None:
        if profile == self.profile:
            return
        self.profile = profile
        self._apply_active_profile()

    # -- resolved paths -----------------------------------------------------
    def modlist_path(self) -> Path | None:
        g = self.game
        if g is None or not self.profile:
            return None
        return g.get_profile_root() / "profiles" / self.profile / "modlist.txt"

    def profile_dir(self) -> Path | None:
        """Active profile dir — where per-profile state (collapsed separators,
        separator locks, etc.) is stored."""
        g = self.game
        if g is None or not self.profile:
            return None
        return g.get_profile_root() / "profiles" / self.profile

    def staging_dir(self) -> Path | None:
        g = self.game
        if g is None:
            return None
        try:
            p = g.get_effective_mod_staging_path()
            return p if p.is_dir() else None
        except Exception:
            return None

    def bsa_index_path(self) -> Path | None:
        """Location of bsa_index.bin (next to filemap.txt), or None."""
        staging = self.staging_dir()
        if staging is None:
            return None
        p = staging.parent / "bsa_index.bin"
        return p if p.is_file() else None

    def build_conflicts(self, log_fn=None, rescan_index: bool = False) -> "ConflictData":
        """Build the filemap for the active game/profile and return a ConflictData
        with loose + BSA conflict codes, the override maps (for highlights), and a
        plugin→owner map. Expensive — run off-thread. Empty on failure.

        rescan_index=True forces a full re-scan of every mod folder from disk
        (the Refresh path) so file changes inside existing mods are picked up."""
        g = self.game
        if g is None or not self.profile:
            return ConflictData()
        from Utils.deploy_pipeline import _build_filemap_for_game
        from gui_qt.modlist_data import display_codes_from_conflict_map
        log = log_fn or (lambda _m: None)
        result = _build_filemap_for_game(
            g, self.profile, log_fn=log, rescan_index=rescan_index)
        if not result:
            return ConflictData()
        _count, _conflict_map, overrides, overridden_by = result
        data = ConflictData(
            # Use the backend's real conflict_map so FULL (redundant) is kept.
            loose_codes=display_codes_from_conflict_map(_conflict_map),
            overrides={k: set(v) for k, v in (overrides or {}).items()},
            overridden_by={k: set(v) for k, v in (overridden_by or {}).items()},
        )
        (data.bsa_codes, data.bsa_overrides,
         data.bsa_overridden_by) = self._build_bsa_conflicts(g, log)
        data.plugin_owner = self._build_plugin_owner(g)
        return data

    def _build_plugin_owner(self, g) -> dict[str, str]:
        """Map plugin filename (lower) → the mod that wins it, from filemap.txt.
        Used for plugin↔mod cross-panel highlighting."""
        staging = self.staging_dir()
        if staging is None:
            return {}
        fm = staging.parent / "filemap.txt"
        if not fm.is_file():
            return {}
        exts = tuple(e.lower() for e in (getattr(g, "plugin_extensions", []) or []))
        if not exts:
            exts = (".esp", ".esm", ".esl")
        owner: dict[str, str] = {}
        try:
            for line in fm.read_text(encoding="utf-8").splitlines():
                if "\t" not in line:
                    continue
                rel_key, mod = line.split("\t", 1)
                base = rel_key.rsplit("/", 1)[-1].lower()
                if base.endswith(exts):
                    owner[base] = mod
        except Exception:
            return {}
        return owner

    def _build_bsa_conflicts(self, g, log):
        """Compute BSA/BA2 archive conflicts. Returns (codes, overrides,
        overridden_by) — codes as 1 win / -1 lose / 2 mixed; the two maps key
        mod → set(mods). Empty triple for non-archive games or on failure."""
        empty = ({}, {}, {})
        exts = frozenset(getattr(g, "archive_extensions", frozenset()) or frozenset())
        if not exts:
            return empty
        staging = self.staging_dir()
        ml = self.modlist_path()
        if staging is None or ml is None or not ml.is_file():
            return empty
        try:
            from Utils.bsa_filemap import build_bsa_conflicts, rebuild_bsa_index
            from Utils.plugins import read_loadorder
        except Exception:
            return empty
        out_dir = staging.parent
        bsa_index = out_dir / "bsa_index.bin"
        try:
            if not bsa_index.is_file():
                rebuild_bsa_index(bsa_index, staging, exts, log_fn=log)
            pdir = self.profile_dir()
            plugin_order = (read_loadorder(pdir / "loadorder.txt")
                            if pdir is not None else None)
            plugin_exts = frozenset(getattr(g, "plugin_extensions", []) or [])
            (bsa_map, bsa_over, bsa_overby, lob, bol) = build_bsa_conflicts(
                ml, bsa_index, exts,
                loose_index_path=out_dir / "modindex.bin",
                plugin_order=plugin_order or None,
                plugin_extensions=plugin_exts or None,
                log_fn=log,
            )
        except Exception as exc:
            log(f"BSA conflict build failed: {exc}")
            return empty
        # build_bsa_conflicts returns CONFLICT_* codes; normalise to our scheme
        # (1 win / -1 lose / 2 mixed) matching conflicts_from_filemap.
        from Utils.filemap import (
            CONFLICT_WINS, CONFLICT_LOSES, CONFLICT_PARTIAL, CONFLICT_FULL,
        )
        codes: dict[str, int] = {}
        for name, c in bsa_map.items():
            if c == CONFLICT_WINS:
                codes[name] = 1
            elif c == CONFLICT_LOSES:
                codes[name] = -1
            elif c in (CONFLICT_PARTIAL, CONFLICT_FULL):
                codes[name] = 2
        # Fold loose↔BSA cross relationships so highlights match the engine's
        # "loose beats BSA" rule (lob: loose_mod→{bsa_mod}, bol: bsa_mod→{loose}).
        over = {k: set(v) for k, v in (bsa_over or {}).items()}
        overby = {k: set(v) for k, v in (bsa_overby or {}).items()}
        for loose_mod, bsa_mods in (lob or {}).items():
            over.setdefault(loose_mod, set()).update(bsa_mods)
        for bsa_mod, loose_mods in (bol or {}).items():
            overby.setdefault(bsa_mod, set()).update(loose_mods)
        return codes, over, overby

    # -- internals ----------------------------------------------------------
    def _select_default_profile(self) -> None:
        profs = self.profiles()
        self.profile = profs[0] if profs else None

    def _apply_active_profile(self) -> None:
        g = self.game
        if g is not None and self.profile:
            g.set_active_profile_dir(
                g.get_profile_root() / "profiles" / self.profile)

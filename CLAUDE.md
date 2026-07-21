# Amethyst Mod Manager — Fork (BG3 modding focus)

## Project context
- Personal fork of TheMrGeeBee/Amethyst-Mod-Manager (upstream: ChrisDKN's Amethyst Mod Manager, GPL-3)
- Focus: Baldur's Gate 3 modding support
- Upstream remote is tracked separately from origin; upstream maintainer communication has historically been difficult — do not assume upstream will merge quickly or engage constructively

## Version & sync rules
- Fork's version always wins over upstream on conflict (e.g. `2.0.4-beta.X` beats `2.0.3-beta.X`)
- Use `sync-upstream.sh` (repo root) for upstream merges — it has an explicit confirmation gate before touching `upstream/Testing`. Never bypass that gate.
- Before opening a PR upstream, create a clean branch — branch contamination from other work has previously forced a close/recreate (see PR #264 history)

## CI/CD
- Three workflows: `test-build.yml`, `build.yml`, `release.yml`
- Release pipeline triggers on `v*` tags
- Changelog extraction looks for `- v{major.minor.patch}` headers — keep that format exact

## Known-good recovery paths
- If a broad automated tool (e.g. `ruff --fix`) or `git restore .` clobbers uncommitted work, VSCodium Local History has recovered it before — check there first, don't panic-recreate

## Conventions
- Follow existing patterns in the codebase before introducing new ones
- Prefer clean, maintainable code over clever solutions
- Recently touched for code quality: `ue5_game.py`, `nexus_requirements.py`, `ba2_writer.py`

## Gotchas
- Collection install metadata (uploader, category, endorsement) is populated via a background GraphQL fetch during install, reconciled in a Step 5 pass — don't assume it's synchronous

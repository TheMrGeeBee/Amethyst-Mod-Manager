#!/usr/bin/env bash
# sync-upstream.sh — sync your Amethyst-Mod-Manager fork's `main` with
# ChrisDKN/Amethyst-Mod-Manager, and push the result to your own GitHub.
#
# Does NOT touch PR branches — this is only for keeping your fork's main
# current. PR branches should stay separate and surgical (fresh branch off
# upstream/Testing or upstream/main, cherry-pick only).
#
# Usage:
#   ./sync-upstream.sh          # sync main with upstream/main only (safe default)
#   ./sync-upstream.sh --testing   # also offer to merge upstream/Testing

set -euo pipefail

WANT_TESTING=false
if [[ "${1:-}" == "--testing" ]]; then
    WANT_TESTING=true
fi

# --- sanity checks -----------------------------------------------------
if ! git remote get-url upstream >/dev/null 2>&1; then
    echo "ERROR: no 'upstream' remote configured. Run:"
    echo "  git remote add upstream https://github.com/ChrisDKN/Amethyst-Mod-Manager.git"
    exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    echo "ERROR: working tree has uncommitted changes to tracked files. Commit or stash first."
    git status --short --untracked-files=no
    exit 1
fi

# --- fetch ---------------------------------------------------------------
echo "==> Fetching upstream..."
git fetch upstream

echo
echo "==> New in upstream/main since your main:"
git log HEAD..upstream/main --oneline || true
echo
echo "==> New in upstream/Testing that upstream/main doesn't have:"
git log upstream/main..upstream/Testing --oneline || true
echo

# --- switch to main --------------------------------------------------------
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$CURRENT_BRANCH" != "main" ]]; then
    echo "==> Switching to main (was on '$CURRENT_BRANCH')..."
    git checkout main
fi

# --- merge upstream/main ---------------------------------------------------
AHEAD_COUNT=$(git rev-list --count HEAD..upstream/main)
if [[ "$AHEAD_COUNT" -eq 0 ]]; then
    echo "==> main is already up to date with upstream/main."
else
    echo "==> Merging upstream/main into main ($AHEAD_COUNT new commit(s))..."
    git merge upstream/main --no-edit
fi

# --- optionally merge upstream/Testing (deliberate, asks first) ------------
if [[ "$WANT_TESTING" == true ]]; then
    TESTING_AHEAD=$(git rev-list --count main..upstream/Testing)
    if [[ "$TESTING_AHEAD" -eq 0 ]]; then
        echo "==> Testing has nothing new beyond what main already has."
    else
        echo
        echo "upstream/Testing is $TESTING_AHEAD commit(s) ahead of main."
        read -r -p "Merge upstream/Testing into main now? [y/N] " REPLY
        if [[ "$REPLY" =~ ^[Yy]$ ]]; then
            echo "==> Merging upstream/Testing into main..."
            if ! git merge upstream/Testing --no-edit; then
                echo
                echo "MERGE CONFLICT. Resolve manually, then:"
                echo "  git add <resolved files>"
                echo "  git commit"
                echo "  git push origin main"
                exit 1
            fi
        else
            echo "==> Skipped merging Testing."
        fi
    fi
fi

# --- push to your fork -------------------------------------------------
echo
read -r -p "Push main to origin now? [y/N] " REPLY
if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    git push origin main
    echo "==> Pushed."
else
    echo "==> Not pushed. Run 'git push origin main' when ready."
fi
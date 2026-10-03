#!/usr/bin/env bash
# Re-attribute every commit to a real account, without the address ever reaching me.
#
# Why this exists: a forge attributes a commit by matching the author email against
# addresses verified in account settings, so registering contributions requires putting a
# real address into the history. That address should never pass through a conversation with
# a model provider, so you supply it locally in a mailmap file and this script only reads it.
#
# Usage:
#   1. cp .mailmap.example .mailmap.local     # never commit this
#   2. edit .mailmap.local -- one line, see that file
#   3. ./scripts/rewrite-author.sh
#
# The address is read from disk and never passed as an argument (argv is visible in /proc
# and in shell history), never printed, and never committed. That is the whole point.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MAILMAP=".mailmap.local"
FILTER_REPO="$ROOT/.venv/bin/git-filter-repo"

fail() { printf 'error: %s\n' "$1" >&2; exit 1; }

# --- preconditions -------------------------------------------------------------
[ -f "$MAILMAP" ] || fail "$MAILMAP not found. Copy .mailmap.example to $MAILMAP and fill it in."
[ -x "$FILTER_REPO" ] || fail "git-filter-repo missing. Install it with:
    uv pip install --python .venv/bin/python git-filter-repo"

if ! git diff --quiet || ! git diff --cached --quiet; then
  fail "working tree is dirty -- commit or stash first. A history rewrite must not race local edits."
fi

# --- capture the identity we are replacing --------------------------------------
OLD_NAME="$(git log -1 --format='%an')"
OLD_ADDR="$(git log -1 --format='%ae')"

# Pre-flight: the mailmap must name the OLD identity in full.
#
# filter-repo matches a rule only when the old name AND the old email both match, so a
# mailmap naming only the address parses cleanly and matches nothing -- the rewrite then
# reports success while changing zero commits. Catching that here costs a second.
#
# Deliberately a literal substring test rather than `git check-mailmap`, which resolves on
# a name-only match and so reports success for a mailmap whose address is wrong.
if ! grep -qF "$OLD_NAME <$OLD_ADDR>" "$MAILMAP"; then
  fail "the mailmap does not name the current author identity, so the rewrite would change nothing.
    a line in $MAILMAP must contain, verbatim:
      <proper-name> <proper-address> $OLD_NAME <$OLD_ADDR>
    both halves of the OLD identity are required -- see $ROOT/.mailmap.example"
fi

NEW_NAME="$(sed -n 's/^\([^<]*\)<.*/\1/p' "$MAILMAP" | head -1 | sed 's/[[:space:]]*$//')"
[ -n "$NEW_NAME" ] || fail "could not read a target name from $MAILMAP"

printf 'rewriting %s commits\n' "$(git rev-list --count --all)"

backup="$ROOT/.git/author-rewrite-$(date +%Y%m%d-%H%M%S).bundle"
git bundle create "$backup" --all >/dev/null
printf 'backup: %s\n' "$backup"

# --- the rewrite ---------------------------------------------------------------
"$FILTER_REPO" --force --mailmap "$MAILMAP"

# --- verify --------------------------------------------------------------------
# Assert the OLD identity is gone, rather than comparing against the new one. That keeps
# every string in the assertion on something this script already captured, instead of
# re-deriving an address it is trying not to mishandle.
leftover_name="$(git log --all --format='%an' | grep -cxF "$OLD_NAME" || true)"
leftover_addr="$(git log --all --format='%ae' | grep -cxF "$OLD_ADDR" || true)"

if [ "${leftover_name:-0}" -ne 0 ] || [ "${leftover_addr:-0}" -ne 0 ]; then
  fail "rewrite did not take: $leftover_name commits still carry the old name, $leftover_addr the old address.
    distinct author names now:
$(git log --all --format='%an' | sort -u | sed 's/^/      /')
    restore with: git clone $backup .
    Note: the usual mailmap trap is omitting the trailing '<old-name> <old-address>' pair."
fi

printf '\nOK: all %s commits now authored by "%s"\n' \
  "$(git rev-list --count --all)" "$NEW_NAME"

cat <<'EOF'

Next steps
----------
  git remote add origin <your-forge-url>
  git push -u origin <branch> --force

A force-push is required: this rewrote every commit hash. Anyone else holding a clone must
re-clone rather than pull.
EOF
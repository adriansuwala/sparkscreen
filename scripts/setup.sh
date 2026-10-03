#!/usr/bin/env bash
# Set up a sparkscreen development environment from scratch.
#
# The point of this script is that the repo is not hostage to one machine's
# idiosyncrasies. Nothing here assumes a particular Python, a particular OS, or a
# pre-existing JDK: the versions are declared in pyproject.toml, the JVM is discovered
# rather than hardcoded, and every hard-to-guess path is echoed as it is chosen.
#
#   ./scripts/setup.sh              # fast dev env only (~15s, no JVM needed)
#   ./scripts/setup.sh --full       # + differential env + Java + ANTLR (~2min, 500MB)
#   ./scripts/setup.sh --check      # verify an existing env, change nothing
#
# Safe to re-run: every step is idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

FAST_VENV=".venv"
DIFF_VENV=".venv-pyspark"
MIN_PY="3.10"
SPARK_VERSION="3.5.1"      # must match tests/differential expectations
ANTLR_VERSION="4.13.1"     # both grammars are generated with this; see docs/agents.md

FULL=0
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --full)  FULL=1 ;;
    --check) CHECK=1 ;;
    -h|--help) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------------
say "Checking prerequisites"

command -v uv >/dev/null || die "uv not found. Install: https://docs.astral.sh/uv/"

# `uv` can fetch a suitable interpreter itself, so a system python3 is a convenience
# rather than a requirement. That is deliberate: requiring a system Python of a specific
# version is how dev setups become unreproducible.
PY=""
if uv python find "$MIN_PY" >/dev/null 2>&1; then
  PY="$(uv python find "$MIN_PY")"
elif command -v python3 >/dev/null && python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
  PY="$(command -v python3)"
else
  die "no Python >= $MIN_PY available. Try: uv python install $MIN_PY"
fi
say "Python: $PY ($("$PY" -V 2>&1))"
echo "    uv:   $(uv --version)"

# ---------------------------------------------------------------------------
# 1. The fast environment
# ---------------------------------------------------------------------------
# The whole reason for two venvs: the differential suite needs a real PySpark, and
# installing it into the everyday environment makes the fast suite slow for no benefit.
# See AGENTS.md.
say "Fast environment ($FAST_VENV) — tests, no JVM required"

if [ "$CHECK" = 1 ]; then
  [ -d "$FAST_VENV" ] || die "$FAST_VENV missing; run without --check"
  PYTHONPATH=src "$FAST_VENV/bin/python" -c "import sparkscreen, antlr4; print('    imports OK, version', sparkscreen.__version__)"
  if PYTHONPATH=src "$FAST_VENV/bin/python" -c "import pyspark" 2>/dev/null; then
    warn "pyspark IS installed in $FAST_VENV -- it should not be. The differential"
    warn "suite belongs in $DIFF_VENV. Harmless, but it slows the fast loop."
  fi
else
  [ -d "$FAST_VENV" ] || uv venv "$FAST_VENV" --python "$PY"
  uv pip install --python "$FAST_VENV/bin/python" -q -e ".[dev]"
  say "Verifying"
  PYTHONPATH=src "$FAST_VENV/bin/python" -c "
import sparkscreen
from sparkscreen.grammar.spec import SPECS
assert len(SPECS) >= 2, 'expected both pinned grammars'
print('    version   ', sparkscreen.__version__)
print('    grammars  ', ', '.join(sorted(s.key for s in SPECS)))
"
  # The generated parsers are committed on purpose. If they were missing the import
  # above would already have failed, so reaching here proves the modules are present;
  # this second check proves they are *usable*, which is the claim that actually matters
  # for a wheel with no JVM.
  PYTHONPATH=src "$FAST_VENV/bin/python" -c "
from sparkscreen.grammar.parser import get_parser
for key in ('spark-4.0', 'spark-3.5.1'):
    parsed = get_parser(key).parse('select 1')
    assert parsed, key
print('    parsers load and parse, no JVM needed')
"
fi

if [ "$FULL" = 0 ]; then
  say "Done. Next:"
  echo "    PYTHONPATH=src $FAST_VENV/bin/python -m pytest tests/ -q"
  echo "    ./scripts/setup.sh --full     # adds the differential suite"
  exit 0
fi

# ---------------------------------------------------------------------------
# 2. Java, for grammar regeneration and the differential suite
# ---------------------------------------------------------------------------
# Only needed for --full. The JVM is *discovered*, never installed or hardcoded, because
# a setup script that apt-gets into the host is not a setup script, it is a deployment.
say "Locating a JVM (needed for the differential suite and grammar regeneration)"

JAVA_HOME_FOUND=""
for cand in "${JAVA_HOME:-}" /opt/data/home/.jre/* /usr/lib/jvm/*/*; do
  if [ -x "$cand/bin/java" ]; then JAVA_HOME_FOUND="$cand"; break; fi
done

if [ -z "$JAVA_HOME_FOUND" ]; then
  warn "No JVM found. The differential suite will be skipped."
  warn "Install one, or set JAVA_HOME, then re-run. On this host the JRE already"
  warn "lives at /opt/data/home/.jre/<version> and is discovered automatically."
else
  say "JAVA_HOME: $JAVA_HOME_FOUND"
  "$JAVA_HOME_FOUND/bin/java" -version 2>&1 | head -1 | sed 's/^/    /'
  export JAVA_HOME="$JAVA_HOME_FOUND"
  export PATH="$JAVA_HOME/bin:$PATH"
fi

# ---------------------------------------------------------------------------
# 3. The differential environment
# ---------------------------------------------------------------------------
if [ "$CHECK" = 0 ]; then
  say "Differential environment ($DIFF_VENV) — pyspark $SPARK_VERSION"
  [ -d "$DIFF_VENV" ] || uv venv "$DIFF_VENV" --python "$PY"
  uv pip install --python "$DIFF_VENV/bin/python" -q -e ".[dev]" ".[diff]"

  say "Verifying"
  "$DIFF_VENV/bin/python" -c "import pyspark; print('    pyspark', pyspark.__version__)"
  if [ -n "$JAVA_HOME_FOUND" ]; then
    say "Differential suite (first run builds a Spark session, so this takes a minute)"
    PYTHONPATH=src "$DIFF_VENV/bin/python" -m pytest tests/differential/ -q \
      -p no:cacheprovider 2>&1 | tail -3
  fi
fi

# ---------------------------------------------------------------------------
# 4. ANTLR, for maintainers only
# ---------------------------------------------------------------------------
say "ANTLR $ANTLR_VERSION"
if [ -f "src/sparkscreen/grammar/.cache/antlr-$ANTLR_VERSION-complete.jar" ]; then
  echo "    already present at src/sparkscreen/grammar/.cache/ (gitignored)"
else
  warn "Not vendored. Needed only to re-generate the parsers from the .g4 sources:"
  warn "  curl -sSL -o src/sparkscreen/grammar/.cache/antlr-$ANTLR_VERSION-complete.jar \\"
  warn "    https://www.antlr.org/download/antlr-$ANTLR_VERSION-complete.jar"
  warn "The committed parsers mean you do not need this to build, test, or release."
fi

say "Done"
echo "  fast suite:  PYTHONPATH=src $FAST_VENV/bin/python -m pytest tests/ -q"
if [ -n "$JAVA_HOME_FOUND" ]; then
  echo "  live Spark:  JAVA_HOME=$JAVA_HOME_FOUND PYTHONPATH=src $DIFF_VENV/bin/python -m pytest tests/differential/ -q"
fi
echo "  doc audits:  _verify_docs.py, _verify_effect.py, _verify_agents.py"

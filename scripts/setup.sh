#!/usr/bin/env bash
# Set up a sparkscreen development environment from scratch.
#
# The point of this script is that the repo is not hostage to one machine's
# idiosyncrasies. Nothing here assumes a particular Python, a particular OS, or a
# pre-existing JDK: the versions are declared in pyproject.toml, the JVM is discovered
# rather than hardcoded, and every hard-to-guess path is echoed as it is chosen.
#
#   ./scripts/setup.sh                    # fast dev env only (~15s, no JVM needed)
#   ./scripts/setup.sh --full             # + differential env + Java + ANTLR (~2min, 500MB)
#   ./scripts/setup.sh --check            # verify an existing env, change nothing
#   ./scripts/setup.sh --engine 4.1.3     # + a second engine venv (.venv-pyspark-4.1.3)
#
# Three engines ship grammars, and the CI matrix runs all three. --full provisions the
# oldest by default because it is the cheapest useful one; --engine provisions any other.
# Each engine venv is ~500MB (462MB of which is Spark's own jars), so ask for the ones
# you need rather than all of them.
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

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# Prints the header comment block as help text. The range is derived from the file rather
# than hardcoded (`sed -n '2,25p'` was already wrong the first time the header grew), so
# editing the header above cannot make --help print shell code.
usage() {
  awk 'NR>1 && !/^#/ {exit} NR>1 {sub(/^# ?/, ""); print}' "$0"
}

FULL=0
CHECK=0
# Parsed with a while/case over "$@" rather than `for arg in "$@"`: `--engine 4.1.3` is
# two words, and `shift` inside a `for` loop only shifts the positional parameters, not the
# loop's word list, so the version was seen again as an unknown option. (`--engine=4.1.3`
# still works and is the form to use in scripts.)
ENGINE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --full)  FULL=1; shift ;;
    --check) CHECK=1; shift ;;
    --engine)
      [ $# -ge 2 ] || die "--engine needs a version, e.g. --engine=4.1.3"
      ENGINE="$2"; shift 2
      FULL=1   # an engine is what --full provisions; do not make them ask for both
      ;;
    --engine=*) ENGINE="${1#--engine=}"; FULL=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

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
  # PYTHONPATH is needed for `python -c` here but not for `pytest`: pythonpath=["src"] in
  # [tool.pytest.ini_options] only applies to the latter.
  if "$FAST_VENV/bin/python" -c "import pyspark" 2>/dev/null; then
    warn "pyspark IS installed in $FAST_VENV -- it should not be. The differential"
    warn "suite belongs in $DIFF_VENV. Harmless, but it slows the fast loop."
  else
    say "    pyspark correctly absent from the fast venv"
  fi
else
  [ -d "$FAST_VENV" ] || uv venv "$FAST_VENV" --python "$PY"
  uv pip install --python "$FAST_VENV/bin/python" -q -e ".[dev]"
  say "Verifying"
  PYTHONPATH=src "$FAST_VENV/bin/python" -c "
import sparkscreen
from sparkscreen.grammar.spec import SPECS
assert SPECS, 'no grammars are pinned'
print('    version   ', sparkscreen.__version__)
print('    grammars  ', ', '.join(sorted(s.key for s in SPECS)))
"
  # The generated parsers are committed on purpose. If they were missing the import above
  # would already have failed, so reaching here proves the modules are present; this check
  # proves they are *usable*, which is the claim that actually matters for a wheel with no
  # JVM.
  #
  # The key list is read from SPECS, never written out here. This loop once named
  # spark-4.0, which F17 removed -- so setup.sh --full crashed on its own verification step
  # and nothing noticed, because no CI job runs this script and _verify_readme.py only
  # checks that the file is executable. Deriving the list means a future removal cannot
  # leave this behind, and a test now asserts every key parses (see
  # _verify_refs.py and tests/test_parser.py::test_version_specific_statements).
  PYTHONPATH=src "$FAST_VENV/bin/python" -c "
from sparkscreen.grammar.parser import get_parser
from sparkscreen.grammar.spec import SPECS
for spec in SPECS:
    assert get_parser(spec.key).parse('select 1'), spec.key
print('    parsers load and parse, no JVM needed:', len(SPECS), 'grammars')
"
fi

if [ "$FULL" = 0 ]; then
  say "Done. Next:"
  echo "    $FAST_VENV/bin/python -m pytest tests/ -q"
  echo "    ./scripts/setup.sh --full     # adds the differential suite"
  exit 0
fi

# ---------------------------------------------------------------------------
# 2. Java, for grammar regeneration and the differential suite
# ---------------------------------------------------------------------------
# Only needed for --full. The JVM is *discovered*, never installed or hardcoded, because
# a setup script that apt-gets into the host is not a setup script, it is a deployment.
say "Locating a JVM (needed for the differential suite and grammar regeneration)"

# Ordered by how portable each location is. The first entry was previously the only one,
# which meant a contributor on any other host got a discovery miss and a silent skip --
# the JVM is needed for the differential suite, so failing to find it quietly disabled the
# exact thing --engine exists to enable. /usr/lib/jvm and /usr/java cover Linux and the
# macOS Homebrew prefix; JAVA_HOME always wins if it is already set.
# Each pattern is expanded unquoted so a glob that matches nothing degrades to a literal
# path that simply fails the -x test. Quoting a pattern inside a for-list does NOT glob it
# in bash -- "/opt/*/.jre/*" stays literal and the -x test fails against "*bin/java" --
# which is how the original single-entry list stopped finding this host's JRE.
JAVA_HOME_FOUND=""
for cand in "${JAVA_HOME:-}" /usr/lib/jvm/* /usr/java/* /opt/java/* /Library/Java/JavaVirtualMachines/*/Contents/Home /opt/data/home/.jre/*; do
  [ -n "$cand" ] || continue
  if [ -x "${cand}/bin/java" ]; then JAVA_HOME_FOUND="$cand"; break; fi
done
# A java on PATH is the last resort, since that yields a bin dir rather than a JAVA_HOME.
if [ -z "$JAVA_HOME_FOUND" ] && command -v java >/dev/null 2>&1; then
  _jb="$(command -v java)"
  JAVA_HOME_FOUND="$(cd "$(dirname "$_jb")/.." && pwd)"
fi

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
provision_engine() {
  # provision_engine <venv-path> <pyspark-version> <grammar-key>
  local venv="$1" ver="$2" key="$3"
  say "Differential environment ($venv) — pyspark $ver, grammar $key"

  if [ "$CHECK" = 1 ]; then
    [ -d "$venv" ] || die "$venv missing; run without --check"
    got=$("$venv/bin/python" -c "import pyspark; print(pyspark.__version__)" 2>/dev/null) \
      || die "$venv cannot import pyspark"
    [ "$got" = "$ver" ] || die "$venv has pyspark $got, expected $ver"
    echo "    pyspark $got"
    return 0
  fi

  [ -d "$venv" ] || uv venv "$venv" --python "$PY"
  # The pin is explicit rather than taken from [diff], because [diff] defaults to the
  # oldest line. Verified afterwards against BOTH the engine version and the grammar the
  # engine is supposed to be screened with -- engine_matrix maps one to the other, and a
  # venv with the right engine wired to the wrong grammar is the silent-wrong-answer case.
  uv pip install --python "$venv/bin/python" -q -e ".[dev]" \
    "pyspark==$ver" "antlr4-python3-runtime==$ANTLR_VERSION" pytest

  say "Verifying"
  "$venv/bin/python" - <<PYEOF
import sys
sys.path.insert(0, "src")
import pyspark
from tests.differential.engine_matrix import grammar_key_for_engine
assert pyspark.__version__ == "$ver", f"installed {pyspark.__version__}, wanted $ver"
key = grammar_key_for_engine(pyspark.__version__)
assert key == "$key", f"engine maps to {key}, setup.sh expected $key"
print(f"    pyspark {pyspark.__version__} -> grammar {key}")
PYEOF

  if [ -n "$JAVA_HOME_FOUND" ]; then
    say "Differential suite on $ver (first run starts a Spark session, so ~1min)"
    "$venv/bin/python" -m pytest tests/differential/ -q -p no:cacheprovider 2>&1 | tail -3
  else
    warn "No JVM: skipping the live suite for $ver. The engine is installed, but"
    warn "differential tests need java. Set JAVA_HOME and re-run with --check."
  fi
}

provision_engine "$DIFF_VENV" "$SPARK_VERSION" "spark-3.5.1"

if [ -n "$ENGINE" ]; then
  # The grammar key is the engine version truncated to major.minor: 4.1.3 -> spark-4.1.
  # Derived rather than looked up in a table, because a table is another thing to forget
  # to update. _verify_refs.py fails if this produces a key spec.py does not ship.
  ENGINE_KEY="spark-$(echo "$ENGINE" | cut -d. -f1,2)"
  if [ "$ENGINE" = "$SPARK_VERSION" ]; then
    say "Engine $ENGINE is the default; $DIFF_VENV already covers it"
  else
    provision_engine ".venv-pyspark-$ENGINE" "$ENGINE" "$ENGINE_KEY"
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
echo "  fast suite:  $FAST_VENV/bin/python -m pytest tests/ -q"
if [ -n "$JAVA_HOME_FOUND" ]; then
  echo "  live Spark:  JAVA_HOME=$JAVA_HOME_FOUND $DIFF_VENV/bin/python -m pytest tests/differential/ -q"
fi
echo "  doc audits:  _verify_docs.py, _verify_effect.py, _verify_agents.py"

"""Pinned Spark grammar sources and the build that turns them into Python parsers.

Each `GrammarSpec` pins one Spark release to an exact commit. The commit hash is the
authority: a grammar file is fetched by commit, never by tag or branch, so a force-push
or tag move cannot silently change what we parse with.

Generated parsers are committed to the repository and shipped in the wheel, so neither
installing nor running sparkscreen requires a JVM. `python -m sparkscreen.grammar.build`
regenerates them; CI checks the committed output matches, so the port cannot rot.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .port import PortError, SUPPORTED_ANTLR_VERSION, port_grammar, verify_tokens_renamed

GRAMMAR_DIR = Path(__file__).parent
VENDORED = GRAMMAR_DIR / "vendored"
GENERATED = GRAMMAR_DIR / "generated"

_GRAMMAR_BASE = (
    "https://raw.githubusercontent.com/apache/spark/{ref}/"
    "sql/api/src/main/antlr4/org/apache/spark/sql/catalyst/parser/{name}.g4"
)


@dataclass(frozen=True)
class GrammarSpec:
    """One supported Spark line, pinned to a commit."""

    #: Public name used in config, e.g. "spark-4.0" or "spark-3.5.1"
    key: str
    #: Exact Spark commit the grammar was taken from. Authoritative.
    commit: str
    #: Human-readable Spark versions this commit belongs to.
    spark_versions: tuple[str, ...]
    #: ANTLR version used to *generate* (not necessarily Spark's own pin).
    antlr_version: str = SUPPORTED_ANTLR_VERSION

    @property
    def files(self) -> tuple[str, ...]:
        return ("SqlBaseLexer", "SqlBaseParser")

    @property
    def module_name(self) -> str:
        """Importable package name for the generated parser.

        Grammar keys contain dots ("spark-4.0") which are illegal in Python module
        names, so the on-disk directory and import path use underscores.
        """
        return self.key.replace("-", "_").replace(".", "_")

    def vendored_path(self, name: str) -> Path:
        return VENDORED / self.key / f"{name}.g4"

    def generated_dir(self) -> Path:
        return GENERATED / self.module_name

    def fetch_url(self, name: str) -> str:
        return _GRAMMAR_BASE.format(ref=self.commit, name=name)

    def python_module(self, name: str) -> str:
        """Fully-qualified module name for a generated parser module.

        Uses `module_name`, not `key`: the public key is "spark-4.0", which contains a
        dot and is not an importable package name. The generated directories are
        "spark_4_0".
        """
        return f"sparkscreen.grammar.generated.{self.module_name}.{name}"


# ---------------------------------------------------------------------------
# The compatibility matrix.
#
# Every `commit` MUST be a full 40-character SHA of an immutable Spark commit.
# This is a supply-chain property, not a style preference: a moving ref -- a branch, or
# even a tag, which can be re-pointed -- means the grammar that gets parsed with can
# change under a released version of this package, and the two grammars do not agree
# (`spark-4.0` accepts `CALL` and `|>`; `spark-3.5.1` rejects them). Two of these were
# originally a short SHA and a bare `v3.5.1` tag; tests/test_grammar_port.py now
# enforces the full-SHA invariant so that cannot regress.
#
# spark-4.0  : grammar as of 2026-08-03. Largest surface: dollar-quoted strings,
#              STRUCT<..> type-level counting, single-char pipe operators, CALL,
#              BEGIN...END scripts, 24 embedded Java constructs.
# spark-3.5.1: release v3.5.1. Older and much smaller grammar (1875 parser lines),
#              6 embedded Java constructs, no dollar-quoting, pipe operators, CALL,
#              or scripts.
#
# Both are generated with ANTLR 4.13.1 even though Spark 3.5.1 pins 4.9.3:
# 4.9.3 rejects that grammar for the Python target (labels `from=`, `input=`,
# `property=` collide with Python runtime attribute names), 4.13.1 accepts it.
# ---------------------------------------------------------------------------

SPECS: tuple[GrammarSpec, ...] = (
    GrammarSpec(
        key="spark-4.0",
        commit="3c28a9c093f1026d76e53d3eb2b846ffb28465c8",
        spark_versions=("4.0.0", "5.0.0"),
        antlr_version="4.13.1",
    ),
    GrammarSpec(
        key="spark-3.5.1",
        commit="fd86f85e181fc2dc0f50a096855acf83a6cc5d9c",
        spark_versions=("3.5.1",),
        antlr_version="4.13.1",
    ),
)

DEFAULT_SPEC_KEY = "spark-4.0"


def get_spec(key: str | None = None) -> GrammarSpec:
    key = key or DEFAULT_SPEC_KEY
    for s in SPECS:
        if s.key == key:
            return s
    known = ", ".join(s.key for s in SPECS)
    raise KeyError(f"unknown grammar {key!r}; supported: {known}")


def spec_for_spark_version(version: str) -> GrammarSpec:
    """Pick the grammar for a Spark version like '3.5.1', '4.0.0', or '4.0'.

    Matching is exact first, then falls back to a major.minor prefix, because Spark
    versions get written both ways in practice ('4.0' as well as '4.0.0') and the
    grammar we ship is a single pinned commit per line rather than one per patch
    release. The prefix match is deliberately restricted to two components so that
    '3.5' cannot silently select a 3.5.1-specific grammar as if it were 3.5.0.
    """
    norm = version.strip().lstrip("vV")
    for s in SPECS:
        if norm in s.spark_versions:
            return s
    parts = norm.split(".")
    if len(parts) == 2:
        for s in SPECS:
            if any(v.startswith(norm + ".") for v in s.spark_versions):
                return s
    known = ", ".join(f"{s.key}={s.spark_versions}" for s in SPECS)
    raise KeyError(f"no pinned grammar for Spark {version!r}; supported: {known}")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

def fetch(spec: GrammarSpec, *, force: bool = False) -> list[Path]:
    """Download the pinned grammar files. Requires network access."""
    import urllib.request

    out: list[Path] = []
    for name in spec.files:
        dst = spec.vendored_path(name)
        if dst.exists() and not force:
            out.append(dst)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        url = spec.fetch_url(name)
        with urllib.request.urlopen(url) as r:
            data = r.read()
        tmp = dst.with_suffix(".g4.tmp")
        tmp.write_bytes(data)
        tmp.replace(dst)
        out.append(dst)
    return out


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------

def _java() -> str:
    """Locate a JRE. Only needed to *regenerate*; not needed to run sparkscreen."""
    import glob
    import os
    import shutil

    found = shutil.which("java")
    if found:
        return found
    for pattern in (
        os.path.expanduser("~/.jre/*/bin/java"),
        "/usr/lib/jvm/*/bin/java",
        "/opt/*/bin/java",
    ):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    raise RuntimeError(
        "no java found; needed only to regenerate parsers "
        "(committed generated parsers are shipped, so this is not a runtime dep)"
    )


def _antlr_jar(spec: GrammarSpec, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    jar = cache / f"antlr-{spec.antlr_version}-complete.jar"
    if not jar.exists():
        import urllib.request
        url = f"https://www.antlr.org/download/antlr-{spec.antlr_version}-complete.jar"
        with urllib.request.urlopen(url) as r:
            data = r.read()
        jar.write_bytes(data)
    return jar


def port_to_python(spec: GrammarSpec, out_dir: Path) -> list[Path]:
    """Translate the vendored grammars into Python-target grammars under out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    texts: dict[str, str] = {}
    for name in spec.files:
        src = spec.vendored_path(name)
        if not src.exists():
            raise PortError(f"missing vendored grammar {src}; run fetch() first")
        texts[name] = src.read_text()

    # Token availability is a property of the *pair*, so resolve it before porting:
    # the parser members must only reference tokens this grammar actually defines.
    pipe_tokens = verify_tokens_renamed(texts["SqlBaseLexer"], texts["SqlBaseParser"])

    for name in spec.files:
        result = port_grammar(
            texts[name],
            is_lexer=name.endswith("Lexer"),
            pipe_start_tokens=tuple(sorted(pipe_tokens)),
        )
        dst = out_dir / f"{name}.g4"
        dst.write_text(result.text)
        written[name] = dst

    return list(written.values())


def generate(spec: GrammarSpec, *, jar_cache: Path | None = None) -> list[Path]:
    """Port then generate Python parsers. Requires java + vendored grammars.

    ANTLR runs in a staging directory and the results are then promoted into the
    spec's generated directory, so a half-finished build can never leave a spec
    looking present-but-broken to an importer.
    """
    staging = spec.generated_dir().parent / f"._staging_{spec.module_name}"
    if staging.exists():
        shutil.rmtree(staging)
    ported = port_to_python(spec, staging)

    jar = _antlr_jar(spec, jar_cache or (GRAMMAR_DIR / ".cache"))
    java = _java()
    cmd = [
        java, "-jar", str(jar),
        "-Dlanguage=Python3", "-visitor", "-no-listener",
        "SqlBaseLexer.g4", "SqlBaseParser.g4",
    ]
    res = subprocess.run(cmd, cwd=staging, capture_output=True, text=True, timeout=900)
    # ANTLR reports label/runtime collisions as errors but still emits files, so the
    # return code -- not merely the presence of output -- decides success.
    if res.returncode != 0:
        raise PortError(f"ANTLR failed for {spec.key}:\n{res.stdout}\n{res.stderr}")
    if res.stderr.strip():
        print(f"[{spec.key}] ANTLR warnings:\n{res.stderr}", file=sys.stderr)

    required = ("SqlBaseLexer.py", "SqlBaseParser.py", "SqlBaseParserVisitor.py")
    for name in required:
        if not (staging / name).exists():
            raise PortError(f"ANTLR did not produce {name} for {spec.key}")

    # promote atomically-ish: build the new tree beside the old, then swap
    target = spec.generated_dir()
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    promoted: list[Path] = []
    for name in required + ("SqlBaseLexer.tokens", "SqlBaseParser.tokens",
                            "SqlBaseLexer.g4", "SqlBaseParser.g4"):
        src_p = staging / name
        if src_p.exists():
            dst_p = target / name
            shutil.copy2(src_p, dst_p)
            promoted.append(dst_p)

    (target / "__init__.py").write_text(
        f'"""Generated ANTLR parser for {spec.key}.\n\n'
        f"Vendored from Spark commit {spec.commit} and generated with ANTLR\n"
        f"{spec.antlr_version}. Do not edit; see sparkscreen.grammar.spec.\n"
        f'Original grammar files are Apache-2.0 licensed.\n"""\n'
    )

    # staging is disposable
    shutil.rmtree(staging, ignore_errors=True)
    return promoted


def all_specs() -> tuple[GrammarSpec, ...]:
    return SPECS
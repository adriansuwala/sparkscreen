"""Command-line interface.

    sparkscreen code.py
    sparkscreen --spark 3.5.1 --json code.py
    cat code.py | sparkscreen -
    sparkscreen --policy company.json code.py

Exit codes are the point of this tool, because it is meant to gate an automated
pipeline:

    0  ALLOW   -- fully analyzed, nothing dangerous
    1  DENY    -- a policy rule matched
    2  UNKNOWN -- could not analyze; needs a human

Exit 2 is deliberately distinct from 0. A CI step that treats "unparseable" as
"approved" is the exact failure mode this project exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .grammar.spec import SPECS, spec_for_spark_version
from .model import Verdict
from .policy import default_policy, load_policy, read_only_policy
from .screen import screen

EXIT_ALLOW = 0
EXIT_DENY = 1
EXIT_UNKNOWN = 2

#: Public grammar keys, e.g. "spark-4.0". Note the dots: these are CLI-facing names and
#: are deliberately not valid Python identifiers, so `--list-grammars` prints them while
#: the generated package directories use underscore names.
SPEC_KEYS = frozenset(s.key for s in SPECS)

_COLORS = {
    Verdict.ALLOW: "\033[32m",
    Verdict.DENY: "\033[31m",
    Verdict.UNKNOWN: "\033[33m",
}
_RESET = "\033[0m"


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text()


def _render(report, use_color: bool) -> str:
    out: list[str] = []
    verdict = report.verdict
    color = _COLORS[verdict] if use_color else ""
    reset = _RESET if use_color else ""
    out.append(f"{color}{report.summary()}{reset}  policy={report.policy} "
               f"grammar={report.grammar} lines={report.lines}")

    if not report.findings:
        out.append("  no SQL sinks found")
        return "\n".join(out)

    for f in report.findings:
        c = _COLORS[f.verdict] if use_color else ""
        r = _RESET if use_color else ""
        loc = f"line {f.line}" if f.line is not None else "code"
        out.append(f"  {c}{f.verdict.value.upper():7}{r} {loc}: {f.message}")
        out.append(f"          reason={f.reason.value} severity={f.severity.value}")
        if f.statement:
            out.append(f"          statement={f.statement}")
        if f.rule:
            out.append(f"          rule={f.rule}")
        if f.targets:
            out.append(f"          targets={', '.join(f.targets)}")
        if f.sql:
            snippet = f.sql if len(f.sql) <= 200 else f.sql[:197] + "..."
            out.append(f"          sql={snippet!r}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sparkscreen",
        description="Screen PySpark for dangerous operations using real parsers.",
    )
    ap.add_argument("path", help="Python file to screen, or '-' for stdin")
    ap.add_argument("--spark", default=None,
                    help="Spark version or grammar key "
                         f"(one of: {', '.join(s.key for s in SPECS)})")
    ap.add_argument("--policy", default=None, help="path to a JSON policy file")
    ap.add_argument("--read-only", action="store_true",
                    help="use the strict read-only policy")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="emit JSON instead of text")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    ap.add_argument("--list-grammars", action="store_true",
                    help="print pinned grammars and exit")
    ap.add_argument("--version", action="version", version=f"sparkscreen {__version__}")
    args = ap.parse_args(argv)

    if args.list_grammars:
        for s in SPECS:
            print(f"{s.key:14} spark={'|'.join(s.spark_versions):14} "
                  f"commit={s.commit} antlr={s.antlr_version}")
        return 0

    try:
        source = _read(args.path)
    except OSError as e:
        print(f"sparkscreen: cannot read {args.path}: {e}", file=sys.stderr)
        return EXIT_UNKNOWN

    # Accept either a grammar key ("spark-3.5.1") or a bare Spark version ("3.5.1").
    # The flag advertises both, and resolving the bare form is what
    # `spec_for_spark_version` exists for -- without this the CLI advertised a
    # convenience it did not provide, and passed the string straight through to
    # screen(), where an unrecognised value raises KeyError and degrades to UNKNOWN.
    if args.spark and args.spark not in SPEC_KEYS:
        try:
            args.spark = spec_for_spark_version(args.spark).key
        except KeyError:
            print(
                f"sparkscreen: unknown Spark version or grammar {args.spark!r}. "
                f"Known: {', '.join(sorted(SPEC_KEYS))}",
                file=sys.stderr,
            )
            return EXIT_UNKNOWN

    if args.policy:
        try:
            policy = load_policy(args.policy)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"sparkscreen: bad policy: {e}", file=sys.stderr)
            return EXIT_UNKNOWN
    elif args.read_only:
        policy = read_only_policy()
    else:
        policy = default_policy()

    report = screen(source, policy, spec=args.spark)

    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        use_color = not args.no_color and sys.stdout.isatty()
        print(_render(report, use_color))

    # Exit code from the report's verdict -- the same value that was printed above.
    #
    # Deriving it from findings' reasons instead was a fail-open bug: a rule may be
    # `verdict=UNKNOWN` with a non-UNKNOWN reason (OUTSIDE_ALLOWLIST, or
    # DESTRUCTIVE_STATEMENT for a row mutation), so the CLI printed UNKNOWN and exited
    # 0. A caller gating on the exit code would then treat unanalyzed code as approved,
    # which is exactly the false assurance this tool exists to prevent.
    return {
        Verdict.ALLOW: EXIT_ALLOW,
        Verdict.DENY: EXIT_DENY,
        Verdict.UNKNOWN: EXIT_UNKNOWN,
    }[report.verdict]


if __name__ == "__main__":
    raise SystemExit(main())
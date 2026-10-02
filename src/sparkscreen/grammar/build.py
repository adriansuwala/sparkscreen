"""Regenerate the pinned grammar parsers.

    python -m sparkscreen.grammar.build --list
    python -m sparkscreen.grammar.build --fetch
    python -m sparkscreen.grammar.build --generate
    python -m sparkscreen.grammar.build --fetch --generate

Generated parsers are committed to the repo and shipped in the wheel, so neither
installing nor running sparkscreen needs a JVM. This module is only for maintainers.
"""

from __future__ import annotations

import argparse
import sys

from .port import PortError
from .spec import SPECS, fetch, generate, get_spec, port_to_python


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sparkscreen.grammar.build")
    ap.add_argument("--list", action="store_true", help="list pinned grammars")
    ap.add_argument("--fetch", action="store_true", help="download pinned grammars")
    ap.add_argument("--generate", action="store_true",
                    help="port and generate Python parsers (needs java)")
    ap.add_argument("--key", action="append", default=None,
                    help="restrict to a grammar key (repeatable)")
    ap.add_argument("--force", action="store_true",
                    help="re-download even if vendored files exist")
    args = ap.parse_args(argv)

    if args.list or not (args.fetch or args.generate):
        for s in SPECS:
            print(f"{s.key:14} commit={s.commit:12} "
                  f"spark={'|'.join(s.spark_versions):12} antlr={s.antlr_version}")
        return 0

    specs = [get_spec(k) for k in args.key] if args.key else list(SPECS)
    failed = False
    for spec in specs:
        print(f"== {spec.key} (commit {spec.commit})", flush=True)
        try:
            if args.fetch:
                for p in fetch(spec, force=args.force):
                    print(f"   vendored {p}")
            if args.generate:
                staged = spec.generated_dir() / "_port"
                ported = port_to_python(spec, staged)
                print(f"   ported {len(ported)} grammars")
                produced = generate(spec)
                total = 0
                for p in produced:
                    total += p.stat().st_size
                    print(f"   generated {p.name} ({p.stat().st_size:,} bytes)")
                print(f"   {spec.key}: {total:,} bytes total")
        except PortError as e:
            print(f"   ERROR: {e}", file=sys.stderr)
            failed = True
        except Exception as e:  # network, java missing, etc.
            print(f"   ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
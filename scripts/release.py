#!/usr/bin/env python
"""Make a release: bump the version, write the changelog, tag, push.

A release here is a tag on master and a GitHub Release carrying the wheel -- not a
PyPI upload (roadmap.md, decided 2026-10-04). The version has exactly two homes:
`version` in pyproject.toml (the declared source of truth) and the source-checkout
fallback in src/sparkscreen/__init__.py, and tests/test_packaging.py exists because
a hand-edit drifted those two apart once. So this script is the only thing that
writes the version: it changes both files together, derives the changelog section
from the commit log, commits, tags, and pushes.

`.github/workflows/release.yml` is the manual trigger and calls this script after
the full CI gate (ci.yml, invoked as a reusable workflow), so the checks a release
must pass are one definition, not a second copy.

Usage:
    scripts/release.py --bump patch|minor|major    # compute the new version
    scripts/release.py --version 0.9.0             # or set it explicitly
    scripts/release.py --notes vX.Y.Z              # print that release's changelog
                                                   # section (used for release notes)

Flags:
    --dry-run     print the plan and write nothing (implies no gate run)
    --yes         do not ask for confirmation
    --skip-gate   do not run scripts/ci_checks.py --all first. The release workflow
                  passes this because its gate job has just run the full CI matrix
                  on this exact commit; re-running the subset would prove nothing.
    --no-push     commit and tag locally, do not push (review-before-push runs)
    --root DIR    operate on this repository instead of the checkout this script
                  lives in (used by tests; anything else, leave unset)

Every refusal happens BEFORE anything is written: a non-master branch, a dirty
tree, an unreachable remote, an existing tag, or a failed gate all abort with the
tree untouched. The gate itself runs after the refusals and before the first write,
so a red suite never leaves a half-bumped tree behind.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
DEFAULT_ROOT = SCRIPTS.parent

#: A release version, not a build or pre-release string. pyproject.toml's
#: [project] version is a plain X.Y.Z here; if that ever changes, the rewrite
#: patterns below change with it -- deliberately, in one file.
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

CHANGELOG_HEADER = "# Changelog"

#: The two homes of the version, written together. pyproject.toml is authoritative
#: (tomllib reads it); the fallback in __init__.py is what a contributor running
#: from a clone sees. test_packaging.py::TestVersionAgreement guards the pair.
_PYPROJECT_PATTERN = 'version = "{old}"'
_INIT_PATTERN = '__version__ = "{old}"'


class ReleaseRefusal(Exception):
    """The release cannot (or must not) proceed. Raised before any write."""


def _git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise ReleaseRefusal(
            f"git {' '.join(args)} failed (exit {proc.returncode}):\n"
            + "\n".join(f"  | {ln}" for ln in (proc.stdout + proc.stderr)
                        .strip().splitlines()[-10:])
        )
    return proc.stdout.strip()


def _read_version(root: Path) -> str:
    """The declared version, read from pyproject.toml -- not re-parsed elsewhere.

    roadmap.md is explicit that there is one number; this function is how the
    script honours that.
    """
    with open(root / "pyproject.toml", "rb") as fh:
        try:
            version = tomllib.load(fh)["project"]["version"]
        except (KeyError, tomllib.TOMLDecodeError) as exc:
            raise ReleaseRefusal(f"cannot read a version from pyproject.toml: {exc}")
    if not VERSION_RE.match(version):
        raise ReleaseRefusal(
            f"pyproject.toml version is {version!r}; expected X.Y.Z. A pre-release or "
            f"build suffix is not supported by the rewrite patterns -- extend "
            f"VERSION_RE and the patterns together, in one place."
        )
    return version


def _bump(old: str, level: str) -> str:
    major, minor, patch = (int(part) for part in old.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _rewrite_version(root: Path, old: str, new: str) -> list[Path]:
    """Rewrite both version homes, surgically, in place.

    A whole-file TOML rewrite would reorder or reformat comments that carry
    decisions (see the packaging comment block in pyproject.toml). Each pattern
    must match exactly once: zero matches means the layout drifted and the script
    must not guess; more than one means the version now lives in two places in a
    single file and that is a bug to fix, not a rewrite to muddle through.
    """
    touched = []
    for relpath, pattern in (
        ("pyproject.toml", _PYPROJECT_PATTERN),
        ("src/sparkscreen/__init__.py", _INIT_PATTERN),
    ):
        path = root / relpath
        text = path.read_text()
        needle = pattern.format(old=old)
        count = text.count(needle)
        if count != 1:
            raise ReleaseRefusal(
                f"{relpath}: expected exactly one occurrence of {needle!r}, "
                f"found {count}. The version's location has moved; update "
                f"_rewrite_version before releasing."
            )
        path.write_text(text.replace(needle, pattern.format(old=new)))
        touched.append(path)
    return touched


def _changelog_section(new: str, commits: list[tuple[str, str]]) -> str:
    lines = [f"## {new} — {date.today().isoformat()}", ""]
    for subject, short in commits:
        lines.append(f"- {subject} ({short})")
    if not commits:
        lines.append("- (no non-merge commits since the previous release)")
    return "\n".join(lines) + "\n"


def _prepend_changelog(root: Path, section: str) -> Path:
    path = root / "CHANGELOG.md"
    if path.exists():
        text = path.read_text()
        if text.lstrip().startswith(CHANGELOG_HEADER):
            end_of_header = text.index("\n") + 1
            text = text[:end_of_header] + "\n" + section + text[end_of_header:]
        else:
            text = CHANGELOG_HEADER + "\n\n" + section + text
    else:
        text = CHANGELOG_HEADER + "\n\n" + section
    path.write_text(text)
    return path


def _print_notes(root: Path, tag: str) -> None:
    """Print the changelog section for one release, for use as release notes."""
    version = tag[1:] if tag.startswith("v") else tag
    path = root / "CHANGELOG.md"
    if not path.exists():
        raise ReleaseRefusal(f"no changelog at {path}; a release writes it first")
    text = path.read_text()
    match = re.search(
        rf"^## {re.escape(version)} — \d{{4}}-\d{{2}}-\d{{2}}\n(.*?)(?=^## |\Z)",
        text, re.MULTILINE | re.DOTALL)
    if not match:
        raise ReleaseRefusal(f"no changelog section for {version}")
    print(match.group(0).rstrip())


def _commits_since_previous_release(root: Path) -> tuple[str | None, list[tuple[str, str]]]:
    """Everything non-merge since the most recent vX.Y.Z tag (or all of history)."""
    tags = _git(root, "tag", "--list", "v*", "--sort=-version:refname").splitlines()
    tag = tags[0] if tags else None
    if tag:
        # The tag must name a commit that is an ancestor of HEAD; if master was
        # rewound after a release this would produce an empty or nonsense range.
        if not _git(root, "merge-base", "--is-ancestor", tag, "HEAD", check=False):
            raise ReleaseRefusal(
                f"latest tag {tag} is not an ancestor of HEAD; the history was "
                f"rewritten after a release and this script refuses to guess the range."
            )
        range_spec = f"{tag}..HEAD"
    else:
        range_spec = "HEAD"
    out = _git(root, "log", "--no-merges", "--format=%s%x09%h", range_spec)
    commits = []
    for line in out.splitlines():
        if "\t" in line:
            subject, short = line.rsplit("\t", 1)
            commits.append((subject, short))
    return tag, commits


def _identity(root: Path) -> tuple[str, str]:
    """The committer identity for the release commit.

    Whatever the checkout is configured with -- nothing is hardcoded here. The
    fallback exists for CI, where no git identity is configured by default; a
    release commit authored by a bot identity is honest, and the attribution
    conventions for human/agent commits do not apply to it.
    """
    name = _git(root, "config", "user.name", check=False)
    email = _git(root, "config", "user.email", check=False)
    return (name or "github-actions[bot]",
            email or "41898282+github-actions[bot]@users.noreply.github.com")


def _run_gate(root: Path) -> None:
    """Run the same checks CI runs, by calling the same script."""
    checker = root / "scripts" / "ci_checks.py"
    if not checker.exists():
        raise ReleaseRefusal(
            "the gate is scripts/ci_checks.py --all, but it is missing. Pass "
            "--skip-gate only when something external has just verified the tree "
            "(the release workflow's gate job, for instance)."
        )
    proc = subprocess.run([sys.executable, str(checker), "--all"], cwd=root,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-20:]
        raise ReleaseRefusal(
            "the release gate failed; nothing was written:\n"
            + "\n".join(f"  | {ln}" for ln in tail)
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--bump", choices=["patch", "minor", "major"],
                        help="which version component to increment")
    source.add_argument("--version", help="the exact version to release (X.Y.Z)")
    source.add_argument("--notes", metavar="TAG",
                        help="print the changelog section for a release and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and write nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--skip-gate", action="store_true",
                    help="do not run scripts/ci_checks.py --all first")
    ap.add_argument("--no-push", action="store_true",
                    help="commit and tag locally; do not push")
    ap.add_argument("--root", help="repository root (default: this script's checkout)")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve() if args.root else DEFAULT_ROOT

    if args.notes:
        try:
            _print_notes(root, args.notes)
        except ReleaseRefusal as exc:
            print(f"refusing: {exc}", file=sys.stderr)
            return 1
        return 0

    if not (root / "pyproject.toml").exists():
        print(f"refusing: {root} does not look like the repository root "
              f"(no pyproject.toml)", file=sys.stderr)
        return 1

    try:
        # ---- refusals: every one of these fires before the first write ----
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != "master":
            raise ReleaseRefusal(
                f"on branch {branch!r}; releases are made from master (D14)."
            )
        if _git(root, "status", "--porcelain"):
            raise ReleaseRefusal(
                "the tree is dirty; commit or stash first so the release commit "
                "contains exactly the version bump and the changelog."
            )
        _git(root, "fetch", "--tags", "origin")
        old = _read_version(root)
        new = args.version or _bump(old, args.bump or "patch")
        if not VERSION_RE.match(new):
            raise ReleaseRefusal(f"--version must be X.Y.Z, not {new!r}")
        if new == old:
            raise ReleaseRefusal(f"the version is already {old}; nothing to release")
        if _git(root, "rev-parse", "-q", "--verify", f"refs/tags/v{new}",
                check=False):
            raise ReleaseRefusal(
                f"tag v{new} already exists. Bumping to an existing version would "
                f"re-point the release; pick the next number."
            )
        previous_tag, commits = _commits_since_previous_release(root)

        plan = [f"release {old} -> {new} (tag v{new})",
                f"previous tag: {previous_tag or '(none; changelog covers all history)'}",
                f"commits since then: {len(commits)}"]

        # ---- the gate: after refusals, before the first write ----
        if not args.dry_run and not args.skip_gate:
            print("running the release gate (scripts/ci_checks.py --all) ...",
                  flush=True)
            _run_gate(root)
            print("gate passed.")

        print("\n".join("plan: " + line for line in plan))
        if args.dry_run:
            print("dry run: nothing written.")
            return 0

        if not args.yes:
            if not sys.stdin.isatty():
                raise ReleaseRefusal(
                    "no tty to confirm with; pass --yes (the release workflow does)."
                )
            if input(f"release {new}? [y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted; nothing written.")
                return 1

        # ---- writes ----
        touched = _rewrite_version(root, old, new)
        section = _changelog_section(new, commits)
        touched.append(_prepend_changelog(root, section))
        if _read_version(root) != new:
            raise ReleaseRefusal("post-write check failed: pyproject.toml does not "
                                 "carry the new version")
        _git(root, "add", *(str(p.relative_to(root)) for p in touched))

        name, email = _identity(root)
        _git(root, "-c", f"user.name={name}", "-c", f"user.email={email}",
             "commit", "-m", f"Release {new}")
        _git(root, "tag", "-a", f"v{new}", "-m", f"sparkscreen {new}\n\n{section}")

        # ---- post-write verification: the tag must name HEAD ----
        described = _git(root, "describe", "--exact-match", "--tags", "HEAD")
        if described != f"v{new}":
            raise ReleaseRefusal(
                f"post-write check failed: HEAD describes as {described!r}, "
                f"expected v{new}"
            )

        if not args.no_push:
            _git(root, "push", "origin", "master")
            _git(root, "push", "origin", f"v{new}")
            print(f"released {new}: pushed master and tag v{new}.")
        else:
            print(f"released {new} locally: commit and tag v{new} made, not pushed.")
        return 0
    except ReleaseRefusal as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

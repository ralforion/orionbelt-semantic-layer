#!/usr/bin/env python3
"""Fail when the osi-orionbelt source changes without leaving the version publishable.

``pypi-publish.yml`` publishes ``osi-orionbelt`` with ``skip-existing: true``,
so that a release in which the converter genuinely did not change passes
instead of failing on "file already exists". That flag cannot tell such a
release apart from one where somebody edited the converter and forgot to bump
its version: both look like "this version is already on PyPI". In v2.24.0 it
was the second case, six converter changes uploaded nothing and the job went
green, so the published converter stayed at 0.1.2 while the app shipped code
written against a newer one.

The invariant this restores: *if the packaged converter source changed, the
version in its pyproject.toml must not already exist on PyPI.* That is exactly
what makes a publish a no-op, and it is deliberately not "the version changed
in this PR" - once a bump to an unpublished version has landed, further changes
to it are legitimate and must not demand a second bump.

Usage:
    python scripts/check_osi_version_bump.py --base-ref origin/main

Exit codes: 0 pass, 1 violation, 2 the check itself could not run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "packages" / "osi-orionbelt"
PYPROJECT = PACKAGE_DIR / "pyproject.toml"
# Only what lands in the wheel. Tests, README and mapping notes can change
# freely without a release.
WATCHED_PATHS = ("packages/osi-orionbelt/src",)
PYPI_URL = "https://pypi.org/pypi/osi-orionbelt/json"
PYPI_TIMEOUT_SECONDS = 15


class CheckError(RuntimeError):
    """The check could not run (bad git state, unreadable pyproject)."""


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise CheckError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def source_changed(base_ref: str) -> bool:
    """Did any packaged converter file change between base_ref and HEAD?"""
    merge_base = _git("merge-base", base_ref, "HEAD")
    changed = _git("diff", "--name-only", f"{merge_base}...HEAD", "--", *WATCHED_PATHS)
    return bool(changed)


def declared_version(ref: str | None = None) -> str:
    """Read the converter version, from the working tree or from a git ref."""
    if ref is None:
        raw = PYPROJECT.read_text()
    else:
        raw = _git("show", f"{ref}:packages/osi-orionbelt/pyproject.toml")
    try:
        return str(tomllib.loads(raw)["project"]["version"])
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise CheckError(f"cannot read the converter version from {PYPROJECT}: {exc}") from exc


def dunder_version() -> str:
    """The version ``osi_orionbelt.__version__`` reports."""
    text = (PACKAGE_DIR / "src" / "osi_orionbelt" / "__init__.py").read_text()
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise CheckError("no __version__ in packages/osi-orionbelt/src/osi_orionbelt/__init__.py")


def published_versions() -> set[str] | None:
    """Versions already on PyPI, or None when PyPI could not be reached."""
    try:
        with urllib.request.urlopen(PYPI_URL, timeout=PYPI_TIMEOUT_SECONDS) as response:
            return set(json.load(response)["releases"])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
        print(f"note: could not read {PYPI_URL} ({exc})", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="branch this change is compared against (default: origin/main)",
    )
    args = parser.parse_args()

    # Always checked, bump or no bump: a pyproject and an __init__ that
    # disagree mean the version a user reads at runtime is not the version
    # they installed, which is what made the 2.24.0 case hard to diagnose.
    version = declared_version()
    dunder = dunder_version()
    if version != dunder:
        print(
            f"FAIL: packages/osi-orionbelt/pyproject.toml says {version} but "
            f"src/osi_orionbelt/__init__.py says {dunder}. Bump both together.",
            file=sys.stderr,
        )
        return 1

    if not source_changed(args.base_ref):
        print("osi-orionbelt source unchanged - no version bump required.")
        return 0

    released = published_versions()

    if released is None:
        # PyPI is unreachable. Fall back to the weaker but offline question:
        # did this change touch the version at all? A pass here is reported as
        # unverified rather than clean, because an unbumped-but-unpublished
        # version is indistinguishable from a stale one without PyPI.
        base_version = declared_version(args.base_ref)
        if version == base_version:
            print(
                f"FAIL: osi-orionbelt source changed but its version is still {version}, "
                f"the same as {args.base_ref}, and PyPI could not be consulted to confirm "
                f"whether {version} is already published. Bump "
                f"packages/osi-orionbelt/pyproject.toml (and __init__.py) to be sure the "
                f"release publishes.",
                file=sys.stderr,
            )
            return 1
        print(f"osi-orionbelt moved {base_version} -> {version} (PyPI unreachable, unverified).")
        return 0

    if version in released:
        print(
            f"FAIL: osi-orionbelt source changed but its version is {version}, which is "
            f"already on PyPI. The publish step uses skip-existing, so the release would "
            f"upload nothing and still report success, leaving PyPI on the old converter "
            f"while the app ships against the new one. Bump the version in "
            f"packages/osi-orionbelt/pyproject.toml and src/osi_orionbelt/__init__.py.",
            file=sys.stderr,
        )
        return 1

    print(f"osi-orionbelt source changed and version {version} is unpublished - will publish.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CheckError as exc:
        print(f"check could not run: {exc}", file=sys.stderr)
        sys.exit(2)

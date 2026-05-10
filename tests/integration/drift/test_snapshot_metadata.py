"""Tier 2 metadata gate — every drift snapshot must point to a real Tier 1 test.

Plan §4.1 specifies that every execution-snapshot YAML carries a
``last_verified_by`` pointer to the Tier 1 test that ratified the captured
value. §2.2 then makes that pointer load-bearing: a PR that re-snaps a
drift artefact is only safe if the matching tier-1 still passes.

This file enforces the *referential integrity* of those pointers:

  * every ``drift/duckdb/*.yaml`` declares ``last_verified_by`` in the
    documented ``path/to/test.py::test_name[params]`` form;
  * the referenced test file exists on disk;
  * pytest can collect the referenced test (so a typo in the test name
    surfaces here, not at the next drift failure when an operator is
    already trying to debug something else).

A passing pytest run therefore guarantees the Tier 1 → drift gate is
intact: any failed tier-1 test in the same workflow fails CI overall, so
a green CI implies every snapshot is anchored to a green tier-1 check.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

DRIFT_DIR = Path(__file__).resolve().parent
DUCKDB_DIR = DRIFT_DIR / "duckdb"
REPO_ROOT = DRIFT_DIR.parents[2]

# Format: ``tests/integration/correctness/<file>.py::<test_name>[<params>]``
# Bracketed params are optional (parametrize markers).
_POINTER_RE = re.compile(r"^(?P<path>tests/[\w/.\-]+\.py)::(?P<name>[\w\[\]\-./, ]+)$")


def _iter_exec_snapshots() -> list[Path]:
    if not DUCKDB_DIR.is_dir():
        return []
    return sorted(p for p in DUCKDB_DIR.glob("*.yaml") if p.is_file())


def _load_snapshot(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# Collected once at module-load so the parametrize ids are stable.
_SNAPSHOTS = _iter_exec_snapshots()


@pytest.mark.skipif(not _SNAPSHOTS, reason="No exec snapshots present yet.")
@pytest.mark.parametrize("path", _SNAPSHOTS, ids=lambda p: p.stem)
def test_snapshot_has_valid_last_verified_by(path: Path) -> None:
    """Every snapshot must declare a parseable, file-existing pointer."""
    data = _load_snapshot(path)
    pointer = data.get("last_verified_by")
    rel = path.relative_to(REPO_ROOT)
    assert pointer, (
        f"{rel}: missing ``last_verified_by`` field. "
        f"Re-snap with UPDATE_SNAPSHOTS=1 after running the matching Tier 1 "
        f"test, and ensure the snapshot harness records its dotted path."
    )
    assert isinstance(pointer, str), (
        f"{rel}: ``last_verified_by`` must be a string, got {type(pointer).__name__}."
    )

    match = _POINTER_RE.match(pointer)
    assert match, (
        f"{rel}: ``last_verified_by`` ({pointer!r}) does not match "
        f"``tests/.../*.py::test_name[params]`` form."
    )
    test_path = REPO_ROOT / match.group("path")
    assert test_path.is_file(), (
        f"{rel}: ``last_verified_by`` references {match.group('path')} "
        f"which does not exist. The Tier 1 test was likely renamed or "
        f"deleted — re-snap to point at the new test."
    )


def test_all_pointers_collect_in_pytest() -> None:
    """Aggregate check: every distinct pointer must be a collectable test.

    Runs ``pytest --collect-only -q`` once over the union of all distinct
    pointer paths and confirms each pointer was collected. This catches:
      * a test renamed without re-snapping,
      * a parametrize id that drifted (e.g. case list reordered),
      * a pointer typo that ``test_snapshot_has_valid_last_verified_by``
        would not catch on its own.

    Cheap: collection is a fraction of a second; no test bodies run.
    """
    if not _SNAPSHOTS:
        pytest.skip("No exec snapshots present yet.")

    pointers: set[str] = set()
    for path in _SNAPSHOTS:
        data = _load_snapshot(path)
        ptr = data.get("last_verified_by")
        if isinstance(ptr, str):
            pointers.add(ptr)

    if not pointers:
        pytest.skip("No pointers to validate.")

    # Pass each pointer as its own positional arg; pytest accepts node IDs.
    # Use the active interpreter's pytest module rather than shelling out
    # to ``uv run`` so the gate works in any environment that runs the
    # test suite (CI containers, virtualenvs without ``uv`` on PATH, etc.).
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", *sorted(pointers)]
    result = subprocess.run(  # noqa: S603 — args are repo-internal, no shell.
        cmd,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"pytest --collect-only rejected one or more "
            f"``last_verified_by`` pointers (exit={result.returncode}). "
            f"This means a pointer references a test that no longer "
            f"exists or has been renamed. Either re-snap the affected "
            f"drift artefacts or restore the test.\n\n"
            f"stdout:\n{result.stdout[-2000:]}\n\n"
            f"stderr:\n{result.stderr[-2000:]}"
        )

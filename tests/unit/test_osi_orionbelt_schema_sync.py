"""Drift guard: the osi-orionbelt package vendors a snapshot of the canonical
OBML JSON schema so it can build and validate standalone (no repo-root
dependency). This test fails if that snapshot drifts from the source of truth
at ``schema/obml-schema.json``; refresh it by copying the canonical file over
the vendored one.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL = _REPO_ROOT / "schema" / "obml-schema.json"
_VENDORED = (
    _REPO_ROOT
    / "packages"
    / "osi-orionbelt"
    / "src"
    / "osi_orionbelt"
    / "schemas"
    / "obml-schema.json"
)


def test_vendored_obml_schema_matches_canonical() -> None:
    assert _CANONICAL.exists(), f"canonical schema missing at {_CANONICAL}"
    assert _VENDORED.exists(), f"vendored schema missing at {_VENDORED}"
    canonical = json.loads(_CANONICAL.read_text())
    vendored = json.loads(_VENDORED.read_text())
    vendored_rel = "packages/osi-orionbelt/src/osi_orionbelt/schemas/obml-schema.json"
    assert vendored == canonical, (
        f"{vendored_rel} has drifted from schema/obml-schema.json. "
        f"Refresh it: cp schema/obml-schema.json {vendored_rel}"
    )

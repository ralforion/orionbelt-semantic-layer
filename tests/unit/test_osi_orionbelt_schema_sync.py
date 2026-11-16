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


def test_vendored_function_table_matches_the_catalog() -> None:
    """The converter's copy of the catalog's arities and units (see
    ``scripts/refresh_osi_function_table.py``), which keeps it from writing a
    measure OBML refuses with WRONG_FUNCTION_ARITY or UNKNOWN_TIME_UNIT."""
    import importlib.util

    script = _REPO_ROOT / "scripts" / "refresh_osi_function_table.py"
    spec = importlib.util.spec_from_file_location("refresh_osi_function_table", script)
    assert spec is not None and spec.loader is not None
    refresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(refresh)
    assert refresh.TABLE.read_text() == refresh.render(refresh.function_table()), (
        "packages/osi-orionbelt/src/osi_orionbelt/schemas/obml-functions.json has drifted "
        "from models/functions.py. Refresh it: "
        "uv run python scripts/refresh_osi_function_table.py"
    )

"""Tier 2 drift — the portable function catalog rendered per dialect.

One golden file per (dialect, catalog group) holding the SQL every canonical
call compiles to, under ``drift/compile_only/<dialect>/functions/<group>.sql``
alongside the per-query snapshots. A rendering change — a renamed function, an
argument reordered, a rewrite dropped — shows up as a diff here rather than as
a wrong number on one vendor.

The golden proves what we *emit*; it cannot prove the engine agrees on the
answer. That is the execution matrix in
``drift/vendor_exec/test_function_exec.py``, which runs the same calls and
asserts the value the catalog documents.

Re-snap with::

    UPDATE_SNAPSHOTS=1 uv run pytest tests/integration/drift/test_drift_functions.py
"""

from __future__ import annotations

import pytest

import orionbelt.dialect  # noqa: F401  -- triggers dialect registrations
from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.functions import FUNCTION_CATALOG, FunctionSpec

from .conftest import assert_compile_only_snapshot

DIALECTS = sorted(DialectRegistry.available())
GROUPS = sorted({spec.group for spec in FUNCTION_CATALOG.values()})


def _specs_in(group: str) -> list[FunctionSpec]:
    return [spec for spec in FUNCTION_CATALOG.values() if spec.group == group]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("group", GROUPS)
def test_function_compile_matrix(group: str, dialect: str) -> None:
    """Render every canonical call in *group* for *dialect*; diff vs snapshot."""
    engine = DialectRegistry.get(dialect)
    lines: list[str] = [f"-- {dialect} · {group} functions"]
    for spec in _specs_in(group):
        lines.append("")
        lines.append(f"-- {spec.signature}")
        # An entry the dialect declares unsupported is recorded in the snapshot
        # rather than skipped: the declaration is itself a fact worth diffing,
        # so silently dropping an engine later shows up as drift.
        if spec.name.lower() in {f.lower() for f in engine.capabilities.unsupported_functions}:
            lines.append(f"--   unsupported on {dialect}")
            continue
        for example in spec.examples:
            ast = parse_expression(tokenize_metric_formula(example.call))
            lines.append(f"--   {example.call} = {example.expect!r}")
            lines.append(f"{engine.compile_expr(ast)};")
    assert_compile_only_snapshot(f"functions/{group}", dialect=dialect, sql="\n".join(lines))

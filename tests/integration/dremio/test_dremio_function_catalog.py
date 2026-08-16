"""Execute the portable function catalog against live Dremio.

Dremio was the one dialect the catalog's execution matrix could not reach:
the vendor-exec suites run on testcontainers and two live cloud warehouses,
and there is no Dremio among them, so its renderings came from the published
function reference rather than from a run. Everything the other seven engines
had asserted -- that ``concat`` propagates NULL, that ``trunc`` goes toward
zero, that a week starts where the model says it does -- was, for Dremio, a
reading of the documentation.

This closes that gap using the container the pgwire suite already spins up.
The catalog's examples take literal arguments only, so they need none of the
seeded commerce data: each one is a ``SELECT`` of a single expression, run
through Dremio's own SQL API.

Opt-in via the ``dremio`` marker, alongside the rest of this suite::

    tests/integration/dremio/run.sh
"""

from __future__ import annotations

import pytest

import orionbelt.dialect  # noqa: F401 -- triggers dialect registration
from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.functions import FUNCTION_CATALOG, FunctionSpec
from orionbelt.models.semantic import WeekStart
from tests.integration.drift.vendor_exec._catalog_values import matches

from .conftest import RunSql

pytestmark = pytest.mark.dremio

CATALOG = list(FUNCTION_CATALOG.values())


def _render(call: str, week_start: WeekStart = WeekStart.MONDAY) -> str:
    engine = DialectRegistry.get("dremio")
    engine.week_start = week_start
    return engine.compile_expr(parse_expression(tokenize_metric_formula(call)))


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
def test_dremio_function_exec(spec: FunctionSpec, run_dremio_sql: RunSql) -> None:
    """Every example of one entry, executed in a single round trip."""
    projections = [
        f"{_render(example.call)} AS c{index}" for index, example in enumerate(spec.examples)
    ]
    sql = "SELECT " + ", ".join(projections)

    values = run_dremio_sql(sql)[0]
    mismatches = [
        f"{example.call} -> {values[index]!r}, catalog says {example.expect!r}"
        for index, example in enumerate(spec.examples)
        if not matches(example.expect, values[index])
    ]
    assert not mismatches, (
        f"Dremio disagrees with the catalog on '{spec.name}':\n  "
        + "\n  ".join(mismatches)
        + f"\nSQL: {sql}"
    )


_WEEK_CASES: list[tuple[str, WeekStart, str | int]] = [
    # 2026-08-15 is a Saturday: its Monday is the 10th, its Sunday the 9th.
    ("date_trunc('week', DATE '2026-08-15')", WeekStart.MONDAY, "2026-08-10"),
    ("date_trunc('week', DATE '2026-08-15')", WeekStart.SUNDAY, "2026-08-09"),
    # One Monday separates that Sunday from that Saturday, and no Sunday does.
    ("date_diff('week', DATE '2026-08-09', DATE '2026-08-15')", WeekStart.MONDAY, 1),
    ("date_diff('week', DATE '2026-08-09', DATE '2026-08-15')", WeekStart.SUNDAY, 0),
    # A timestamp input: the start of a week is midnight, so a rewrite that
    # subtracts days from the value rather than from its day keeps 13:45.
    ("date_trunc('week', TIMESTAMP '2026-08-15 13:45:00')", WeekStart.MONDAY, "2026-08-10"),
    ("date_trunc('week', TIMESTAMP '2026-08-15 13:45:00')", WeekStart.SUNDAY, "2026-08-09"),
]


def test_dremio_week_start(run_dremio_sql: RunSql) -> None:
    """``settings.weekStart`` on the engine whose day-of-week numbering this
    repo could not otherwise check: Dremio's ``DAYOFWEEK`` is documented as
    numbering Sunday 1, and the Sunday rewrite depends on that being true.
    """
    failures = []
    for call, week_start, expected in _WEEK_CASES:
        sql = f"SELECT {_render(call, week_start)} AS c0"
        actual = run_dremio_sql(sql)[0][0]
        if not matches(expected, actual):
            failures.append(
                f"{call} under weekStart={week_start.value}: {actual!r} != {expected!r}"
            )
    assert not failures, "Dremio:\n  " + "\n  ".join(failures)


def test_dremio_query_timezone(run_dremio_sql: RunSql) -> None:
    """``settings.queryTimezone`` on the engine the vendor matrix cannot reach.

    The instant 2026-08-09 22:30 UTC is 00:30 on Monday the 10th in Zagreb, so
    a conversion that works moves the value across a day and a week boundary.
    """
    from orionbelt.ast.nodes import Cast, InTimeZone, Literal

    node = InTimeZone(
        expr=Cast(expr=Literal.string("2026-08-09 22:30:00"), type_name="timestamp"),
        zone="Europe/Zagreb",
        from_zone="UTC",
    )
    sql = f"SELECT {DialectRegistry.get('dremio').compile_expr(node)} AS c0"
    actual = run_dremio_sql(sql)[0][0]
    assert str(actual)[:19].replace("T", " ") == "2026-08-10 00:30:00", f"{actual!r}\nSQL: {sql}"

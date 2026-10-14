"""Reaggregate metrics with ``carryForward`` (DuckDB, commerce).

The commerce sales stand in for balance snapshots: a client's balance at a
month's end is the amount of its last sale up to then (ties to the greater
amount, as a ``last`` measure breaks them). A closing total is that summed
over the clients, carried through the months a client has no sale in. The
references are written straight against the tables.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.compiler.resolution import ResolutionError  # noqa: E402
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed  # noqa: E402

_LAST_SALE = {
    "columns": [{"dataObject": "Sales", "column": "Sales Amount"}],
    "aggregation": "last",
    "withinGroup": {"column": {"dataObject": "Sales", "column": "Sales Date"}},
}
_CLOSING = {
    "type": "reaggregate",
    "per": ["Client Name"],
    "measure": "Last Sale",
    "aggregation": "sum",
    "population": "dimension",
}

# Each (group..., month) of the months with sales: every client's last sale up
# to the month's end, summed. {group} adds columns to both the months and the
# sales they read.
_REFERENCE = """
WITH months AS (
    SELECT DISTINCT {group_cols}CAST(date_trunc('month', s.salesdate) AS DATE) AS m
    FROM orionbelt_1.sales s {joins}
    WHERE s.salesdate >= DATE '{since}'
),
balances AS (
    SELECT {group_mo}mo.m, s.salesclient,
           arg_max(s.salesamount, (s.salesdate, s.salesamount)) AS v
    FROM months mo
    JOIN orionbelt_1.sales s
      ON s.salesdate < mo.m + INTERVAL 1 MONTH AND s.salesamount IS NOT NULL
    {joins}
    {group_on}
    GROUP BY ALL
)
SELECT {group_out}m, SUM(v) FROM balances GROUP BY ALL
"""


def _reference(
    db: duckdb.DuckDBPyConnection, group: str | None = None, since: str = "1900-01-01"
) -> dict[Any, Decimal]:
    """Closing totals by month, or by product name and month."""
    joins, col = {
        None: ("", ""),
        "product": (
            "LEFT JOIN orionbelt_1.products p ON s.product = p.productid",
            "p.productname",
        ),
    }[group]
    sql = _REFERENCE.format(
        group_cols=f"{col} AS g, " if group else "",
        group_mo="mo.g, " if group else "",
        group_out="g, " if group else "",
        group_on=f"WHERE {col} IS NOT DISTINCT FROM mo.g" if group else "",
        joins=joins,
        since=since,
    )
    return {tuple(key): v for *key, v in db.sql(sql).fetchall()}


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    _require_seed()
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["measures"]["Last Sale"] = _LAST_SALE
    raw["metrics"]["Closing Total"] = {**_CLOSING, "carryForward": True}
    raw["metrics"]["Closing Total Uncarried"] = _CLOSING
    raw["dimensions"]["Calendar Day"] = {
        "dataObject": "Calendar",
        "column": "Date",
        "resultType": "date",
    }
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


@pytest.fixture(scope="module")
def run(
    model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection
) -> Callable[..., list[tuple[Any, ...]]]:
    def _run(dims: list[str], measures: list[str], **query: Any) -> list[tuple[Any, ...]]:
        q = QueryObject(select=QuerySelect(dimensions=dims, measures=measures), **query)
        return commerce_db.sql(CompilationPipeline().compile(q, model, "duckdb").sql).fetchall()

    return _run


def _close(got: Any, want: Any) -> bool:
    return abs(Decimal(str(got)) - Decimal(str(want))) < Decimal("0.01")


def test_by_month(run: Callable, commerce_db: duckdb.DuckDBPyConnection) -> None:
    """Every month's total carries the clients without a sale in it; without
    carrying they drop out, so carried is the greater after the first month,
    which has nothing earlier to carry."""
    want = _reference(commerce_db)
    rows = sorted(run(["Sales Month"], ["Closing Total", "Closing Total Uncarried"]))
    assert len(rows) == len(want) == 60
    for month, carried, _ in rows:
        assert _close(carried, want[(month,)]), month
    first, *later = rows
    assert _close(first[1], first[2])
    assert all(carried > uncarried for _, carried, uncarried in later)


def test_a_date_filter_reads_the_history_before_it(
    run: Callable, commerce_db: duckdb.DuckDBPyConnection
) -> None:
    """From 2023 on, January 2023 still carries the 2022 balances: the filter
    limits the months shown, not the snapshots read."""
    want = _reference(commerce_db)
    rows = run(
        ["Sales Month"],
        ["Closing Total"],
        where=[QueryFilter(field="Sales Date", op=">=", value="2023-01-01")],
    )
    assert len(rows) == 36
    for month, carried in rows:
        assert _close(carried, want[(month,)]), month


def test_by_country_and_month(run: Callable, commerce_db: duckdb.DuckDBPyConnection) -> None:
    """A dimension of the clients groups them. Every country with clients is
    a row in every month with sales, also one its clients bought nothing in:
    their balances carry. A country before its clients' first sale is NULL."""
    want = {
        (country, month): v
        for country, month, v in commerce_db.sql(
            """
            WITH months AS (
                SELECT DISTINCT CAST(date_trunc('month', salesdate) AS DATE) AS m
                FROM orionbelt_1.sales
            ),
            balances AS (
                SELECT cl.clientid, mo.m,
                       arg_max(s.salesamount, (s.salesdate, s.salesamount)) AS v
                FROM months mo
                JOIN orionbelt_1.sales s
                  ON s.salesdate < mo.m + INTERVAL 1 MONTH AND s.salesamount IS NOT NULL
                JOIN orionbelt_1.clients cl ON s.salesclient = cl.clientid
                GROUP BY ALL
            )
            SELECT co.countryname, mo.m, SUM(b.v)
            FROM orionbelt_1.clients cl
            LEFT JOIN orionbelt_1.countries co ON cl.clientcountryid = co.countryid
            CROSS JOIN months mo
            LEFT JOIN balances b ON b.clientid = cl.clientid AND b.m = mo.m
            GROUP BY ALL
            """
        ).fetchall()
    }
    rows = run(["Country Name", "Sales Month"], ["Closing Total"])
    assert len(rows) == len(want)
    for country, month, carried in rows:
        expected = want[(country, month)]
        assert (carried is None) == (expected is None), (country, month)
        if expected is not None:
            assert _close(carried, expected), (country, month)


def test_by_product_and_month(run: Callable, commerce_db: duckdb.DuckDBPyConnection) -> None:
    """A period off the date (a product name) keeps runs of its own: a
    client's balance in a product carries only that product's last sale."""
    want = _reference(commerce_db, "product")
    rows = run(["Product Name", "Sales Month"], ["Closing Total"])
    assert len(rows) == len(want)
    for product, month, carried in rows:
        assert _close(carried, want[(product, month)]), (product, month)


def test_two_grains_of_the_date(run: Callable, commerce_db: duckdb.DuckDBPyConnection) -> None:
    """The year and the month are one axis, ordered year first."""
    want = _reference(commerce_db)
    rows = run(["Sales Date:year", "Sales Date:month"], ["Closing Total"])
    assert len(rows) == 60
    for _, month, carried in rows:
        assert _close(carried, want[(month,)]), month


def test_without_a_date_nothing_is_carried(run: Callable) -> None:
    """By country alone there is one period, the whole range: carrying is the
    plain population."""
    rows = run(["Country Name"], ["Closing Total", "Closing Total Uncarried"])
    assert rows
    assert all(r[1] == r[2] for r in rows)


def test_two_dates_refused(model: SemanticModel) -> None:
    query = QueryObject(
        select=QuerySelect(
            dimensions=["Sales Month", "Calendar Day:year"], measures=["Closing Total"]
        )
    )
    with pytest.raises(ResolutionError, match="REAGGREGATE_CARRY_FORWARD_AXIS|more than one date"):
        CompilationPipeline().compile(query, model, "duckdb")


def _resolve(metric: dict[str, Any]) -> list[str]:
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["measures"]["Last Sale"] = _LAST_SALE
    raw["metrics"]["Probe"] = metric
    _, result = ReferenceResolver().resolve(raw, source_map)
    return [e.code for e in result.errors] + [e.message for e in result.errors]


def test_needs_the_dimension_population() -> None:
    errors = _resolve({**_CLOSING, "population": "facts", "carryForward": True})
    assert "REAGGREGATE_CARRY_FORWARD_POPULATION" in errors


def test_only_on_reaggregate_metrics() -> None:
    errors = _resolve(
        {
            "type": "cumulative",
            "measure": "Total Sales",
            "timeDimension": "Sales Date",
            "carryForward": True,
        }
    )
    assert any("carryForward" in e for e in errors), errors


_SNAPSHOTS_MODEL = """
version: "1.0"
dataObjects:
  Snapshots:
    code: snapshots
    schema: main
    columns:
      Account: {code: account, abstractType: string}
      Snapshot Date: {code: d, abstractType: date}
      Balance: {code: balance, abstractType: float}
    joins:
      - {joinType: many-to-one, joinTo: Accounts, columnsFrom: [Account], columnsTo: [Account]}
  Accounts:
    code: accounts
    schema: main
    columns:
      Account: {code: account, abstractType: string}
dimensions:
  Account: {dataObject: Accounts, column: Account, resultType: string}
  Snapshot Month:
    dataObject: Snapshots
    column: Snapshot Date
    resultType: date
    timeGrain: month
measures:
  Last Balance:
    columns: [{dataObject: Snapshots, column: Balance}]
    aggregation: last
    withinGroup: {column: {dataObject: Snapshots, column: Snapshot Date}}
metrics:
  Closing Balance:
    type: reaggregate
    measure: Last Balance
    per: [Account]
    aggregation: sum
    population: dimension
    carryForward: true
"""


@pytest.mark.parametrize(
    "dims", [["Snapshot Month"], ["Account", "Snapshot Month"]], ids=["by month", "by account"]
)
def test_selecting_the_per_dimension_still_carries(dims: list[str]) -> None:
    """A's February snapshot has no balance, so February carries January's
    10, also when the query groups by the account itself: the members are
    needed to carry, whether or not ``per`` adds a column."""
    db = duckdb.connect()
    db.execute("CREATE TABLE accounts AS SELECT 'A' AS account")
    db.execute(
        "CREATE TABLE snapshots AS SELECT * FROM (VALUES "
        "('A', DATE '2026-01-10', 10.0), ('A', DATE '2026-02-10', NULL)"
        ") AS t(account, d, balance)"
    )
    raw, source_map = TrackedLoader().load_string(_SNAPSHOTS_MODEL)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = QueryObject(select=QuerySelect(dimensions=dims, measures=["Closing Balance"]))
    rows = db.sql(CompilationPipeline().compile(query, model, "duckdb").sql).fetchall()
    assert sorted(r[-1] for r in rows) == [10, 10]

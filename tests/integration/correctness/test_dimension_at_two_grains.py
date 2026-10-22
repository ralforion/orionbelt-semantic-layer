"""One dimension selected at two grains (DuckDB, commerce).

``Sales Date:year`` beside ``Sales Date:month`` gives two columns, the second
named ``Sales Date:month``. Every wrapper joins and groups by a dimension's
column name, so with one name for both the month column showed the year and
the metrics computed beside them read the year twice. The month determines
the year, so each row has to equal the month-only query's.
"""

from __future__ import annotations

from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.compiler.resolution import ResolutionError  # noqa: E402
from orionbelt.models.query import QueryObject, QueryOrderBy, QuerySelect  # noqa: E402
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402
from orionbelt.service.result_schema import declared_result_schema  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed  # noqa: E402

_PER_CLIENT = {"type": "reaggregate", "per": ["Client Name"], "measure": "Sales Count"}


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    _require_seed()
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["metrics"].update(
        {
            "Running Sales": {
                "type": "cumulative",
                "measure": "Total Sales",
                "timeDimension": "Sales Date",
            },
            "Prior Sales": {
                "type": "window",
                "windowFunction": "lag",
                "measure": "Total Sales",
                "offset": 1,
                "timeDimension": "Sales Date",
            },
            "Avg Client Orders": {**_PER_CLIENT, "aggregation": "avg"},
            "Peak Day Client Orders": {
                "type": "reaggregate",
                "measure": "Avg Client Orders",
                "per": ["Sales Date:day"],
                "aggregation": "max",
            },
            "Last YTD Sales": {
                "type": "reaggregate",
                "measure": "YTD Sales",
                "per": ["Sales Month"],
                "aggregation": "last",
            },
            "Avg Running Sales": {
                "type": "reaggregate",
                "measure": "Running Sales",
                "per": ["Sales Date"],
                "aggregation": "avg",
            },
            "Avg Client Orders All": {
                **_PER_CLIENT,
                "aggregation": "avg",
                "population": "dimension",
            },
            "Sales Change": {
                "type": "period_over_period",
                "expression": "{[Total Sales]}",
                "periodOverPeriod": {
                    "timeDimension": "Sales Date",
                    "grain": "month",
                    "offset": -1,
                    "offsetGrain": "month",
                },
            },
        }
    )
    # A date with no declared grain: the column itself is ``Raw Date:value``
    # beside another grain of it.
    raw["dimensions"]["Raw Date"] = {
        "dataObject": "Sales",
        "column": "Sales Date",
        "resultType": "date",
    }
    total = dict(raw["measures"]["Total Sales"])
    raw["measures"].update(
        {
            "Sales All Dates": {**total, "grain": {"mode": "RELATIVE", "exclude": ["Sales Date"]}},
            "Sales Any Country": {
                **total,
                "filterContext": {"mode": "RELATIVE", "exclude": ["Country Name"]},
            },
        }
    )
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def _run(
    db: duckdb.DuckDBPyConnection, model: SemanticModel, dims: list[str], measure: str
) -> tuple[list[str], list[tuple[Any, ...]]]:
    query = QueryObject(select=QuerySelect(dimensions=dims, measures=[measure]))
    cursor = db.sql(CompilationPipeline().compile(query, model, "duckdb").sql)
    return cursor.columns, cursor.fetchall()


@pytest.mark.parametrize(
    "measure",
    [
        "Total Sales",
        "Unfiltered Sales",
        "Running Sales",
        "Prior Sales",
        "Avg Client Orders",
        "Avg Client Orders All",
        "Peak Day Client Orders",
        "Avg Running Sales",
        "Sales Any Country",
    ],
)
def test_each_row_is_its_month(
    model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection, measure: str
) -> None:
    _, by_month = _run(commerce_db, model, ["Sales Date:month"], measure)
    want = dict(by_month)
    columns, rows = _run(commerce_db, model, ["Sales Date:year", "Sales Date:month"], measure)
    assert columns == ["Sales Date", "Sales Date:month", measure]
    assert len(rows) == len(want) > 12
    for year, month, value in rows:
        assert year == month.replace(month=1), (year, month)
        assert value == want[month], (month, value, want[month])


@pytest.mark.parametrize(
    ("dims", "columns"),
    [
        (["Sales Date", "Sales Date:month"], ["Sales Date", "Sales Date:month"]),
        (["Sales Date:month", "Sales Date"], ["Sales Date", "Sales Date:day"]),
    ],
    ids=["declared grain first", "declared grain second"],
)
def test_column_names(
    model: SemanticModel,
    commerce_db: duckdb.DuckDBPyConnection,
    dims: list[str],
    columns: list[str],
) -> None:
    """The first keeps the dimension's name and a second grain is
    ``name:grain`` (``Sales Date`` declares ``timeGrain: day``); the declared
    schema names them alike."""
    got, _ = _run(commerce_db, model, dims, "Total Sales")
    assert got == [*columns, "Total Sales"]
    query = QueryObject(select=QuerySelect(dimensions=dims, measures=["Total Sales"]))
    assert declared_result_schema(query, model).names == got


def test_period_over_period_refused(model: SemanticModel) -> None:
    """A comparison buckets its time dimension at its own grain, from a date
    spine: there is no second grain of it to show."""
    query = QueryObject(
        select=QuerySelect(
            dimensions=["Sales Date:year", "Sales Date:month"], measures=["Sales Change"]
        )
    )
    with pytest.raises(ResolutionError, match="second grain"):
        CompilationPipeline().compile(query, model, "duckdb")


@pytest.mark.parametrize(
    "measure", ["Total Sales", "Running Sales", "Prior Sales", "Avg Client Orders All"]
)
def test_order_by_the_second_grain(
    model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection, measure: str
) -> None:
    """``orderBy`` names the second grain's column, not the date column the
    first grain is also read from."""
    query = QueryObject(
        select=QuerySelect(dimensions=["Sales Date:year", "Sales Date:month"], measures=[measure]),
        order_by=[QueryOrderBy(field="Sales Date:month", direction="desc")],
        limit=3,
    )
    rows = commerce_db.sql(CompilationPipeline().compile(query, model, "duckdb").sql).fetchall()
    months = [r[1] for r in rows]
    assert months == sorted(months, reverse=True)
    assert len(set(months)) == 3


def test_grain_exclude_takes_every_grain(
    model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection
) -> None:
    """``grain.exclude: [Sales Date]`` drops the dimension at both grains: the
    grand total on every row, not each month's."""
    _, rows = _run(commerce_db, model, ["Sales Date:year", "Sales Date:month"], "Sales All Dates")
    ((total,),) = commerce_db.sql(
        "SELECT CAST(SUM(salesamount) AS DECIMAL(18, 2)) FROM orionbelt_1.sales"
    ).fetchall()
    assert {r[-1] for r in rows} == {total}


def test_filter_context_at_a_written_grain(
    model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection
) -> None:
    """With no filter to drop, a filterContext measure is the plain one. Its
    scan names the dimensions as the query does: ``Sales Date:month`` alone
    was scanned at the declared day grain, ``Raw Date:value`` is no reference."""
    for dims in (["Sales Date:month"], ["Raw Date:month", "Raw Date"]):
        query = QueryObject(
            select=QuerySelect(dimensions=dims, measures=["Sales Any Country", "Total Sales"])
        )
        rows = commerce_db.sql(CompilationPipeline().compile(query, model, "duckdb").sql).fetchall()
        assert rows
        assert all(r[-2] == r[-1] for r in rows), dims


def test_type_maps_name_the_second_grain(model: SemanticModel) -> None:
    """The declared types reach the generated column too, so a result whose
    driver hands back ``date64`` is reconciled to the declared ``date32``."""
    import pyarrow as pa

    from orionbelt.api.query_cache import build_type_map
    from orionbelt.service.result_schema import declared_arrow_types, reconcile_to_declared

    query = QueryObject(
        select=QuerySelect(
            dimensions=["Sales Date:year", "Sales Date:month"], measures=["Total Sales"]
        )
    )
    declared = declared_arrow_types(model, query)
    assert declared["Sales Date:month"] == pa.date32()
    assert (
        build_type_map(model, query)["Sales Date:month"]
        == build_type_map(model, query)["Sales Date"]
    )
    table = pa.table(
        {
            "Sales Date": pa.array([0], pa.date64()),
            "Sales Date:month": pa.array([0], pa.date64()),
        }
    )
    reconciled, _ = reconcile_to_declared(table, declared)
    assert reconciled.schema.field("Sales Date:month").type == pa.date32()


@pytest.mark.parametrize(
    ("dims", "month"),
    [
        (["Sales Month:year", "Sales Month"], "Sales Month:month"),
        (["Sales Month", "Sales Month:year"], "Sales Month"),
    ],
    ids=["month second", "month first"],
)
def test_reaggregate_over_a_cumulative_keeps_its_axis_column(
    model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection, dims: list[str], month: str
) -> None:
    """The cumulative's own axis is planned under its name; selected after
    the year it is still the suffixed column, in either order each month's
    value."""
    _, by_month = _run(commerce_db, model, ["Sales Month"], "Last YTD Sales")
    want = dict(by_month)
    columns, rows = _run(commerce_db, model, dims, "Last YTD Sales")
    assert month in columns
    at = columns.index(month)
    assert len(rows) == len(want)
    for row in rows:
        assert row[-1] == want[row[at]], row


@pytest.mark.parametrize(
    "dims",
    [["Sales Date:month", "Sales Date:month"], ["Sales Date", "Sales Date:day"]],
    ids=["one grain twice", "the declared grain named"],
)
def test_one_grain_twice_is_refused(model: SemanticModel, dims: list[str]) -> None:
    """Two entries for one column named it twice, and a client reading the
    result by name saw one of them."""
    query = QueryObject(select=QuerySelect(dimensions=dims, measures=["Total Sales"]))
    with pytest.raises(ResolutionError) as exc:
        CompilationPipeline().compile(query, model, "duckdb")
    assert [e.code for e in exc.value.errors] == ["DUPLICATE_DIMENSION"]

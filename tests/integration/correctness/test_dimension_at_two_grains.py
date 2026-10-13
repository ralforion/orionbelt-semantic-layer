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
        (["Sales Date:month", "Sales Date:month"], ["Sales Date", "Sales Date"]),
    ],
    ids=["declared grain first", "declared grain second", "one grain twice"],
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

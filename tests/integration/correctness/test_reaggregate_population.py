"""Reaggregate metrics with ``population: dimension`` (DuckDB, commerce).

Stage 1 takes every client, not only the 471 of 500 with sales, and a client
without sales takes the measure's ``defaultValue`` (0 for a count). The
reference is computed straight from the tables, not through the compiler.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from orionbelt.compiler.pipeline import CompilationPipeline  # noqa: E402
from orionbelt.compiler.resolution import ResolutionError  # noqa: E402
from orionbelt.models.query import (  # noqa: E402
    FilterOperator,
    QueryFilter,
    QueryFilterGroup,
    QueryObject,
    QuerySelect,
    Subquery,
)
from orionbelt.models.semantic import SemanticModel  # noqa: E402
from orionbelt.parser.loader import TrackedLoader  # noqa: E402
from orionbelt.parser.resolver import ReferenceResolver  # noqa: E402

from .conftest import COMMERCE_MODEL_YAML, _require_seed, _rows_as_dicts  # noqa: E402

_PER_CLIENT = {"type": "reaggregate", "per": ["Client Name"]}
_METRICS: dict[str, dict[str, Any]] = {
    "Avg Client Sales All": {
        **_PER_CLIENT,
        "measure": "Total Sales Or Zero",
        "aggregation": "avg",
        "population": "dimension",
    },
    "Avg Client Sales Buyers": {
        **_PER_CLIENT,
        "measure": "Total Sales Or Zero",
        "aggregation": "avg",
    },
    "Avg Client Sales No Default": {
        **_PER_CLIENT,
        "measure": "Total Sales",
        "aggregation": "avg",
        "population": "dimension",
    },
    "Avg Client Orders All": {
        **_PER_CLIENT,
        "measure": "Sales Count",
        "aggregation": "avg",
        "population": "dimension",
    },
    "Min Client Orders All": {
        **_PER_CLIENT,
        "measure": "Sales Count",
        "aggregation": "min",
        "population": "dimension",
    },
    "Avg Orders per Client and Country": {
        "type": "reaggregate",
        "per": ["Client Name", "Country Name"],
        "measure": "Sales Count",
        "aggregation": "avg",
        "population": "dimension",
    },
    "Clients All": {
        **_PER_CLIENT,
        "measure": "Sales Count",
        "aggregation": "count",
        "population": "dimension",
    },
    "Buyers": {**_PER_CLIENT, "measure": "Sales Count", "aggregation": "count"},
    "Clients Plus Orders": {"expression": "{[Clients All]} + {[Sales Count]}"},
    "Clients Plus Sales": {"expression": "{[Clients All]} + {[Total Sales Or Zero]}"},
    "Orders Doubled": {"expression": "{[Sales Count]} * 2"},
    "Count Rank": {"type": "window", "windowFunction": "rank", "measure": "Sales Count"},
    "Rank Plus One": {"expression": "{[Count Rank]} + 1"},
    "One": {"expression": "1"},
    "Orders Plus One": {"expression": "{[Sales Count]} + {[One]}"},
    "Avg Client Orders Buyers": {
        **_PER_CLIENT,
        "measure": "Sales Count",
        "aggregation": "avg",
    },
    "Peak Month All": {
        "type": "reaggregate",
        "measure": "Avg Client Orders All",
        "per": ["Sales Date:month"],
        "aggregation": "max",
    },
    "Peak Month Buyers": {
        "type": "reaggregate",
        "measure": "Avg Client Orders Buyers",
        "per": ["Sales Date:month"],
        "aggregation": "max",
    },
}

# Each client's sales and order count from the tables, every client included.
_CLIENTS = """
SELECT co.countryname AS country, cl.clientgender AS gender,
       COALESCE(SUM(s.salesamount), 0) AS sales, COUNT(s.salesid) AS orders,
       MIN(s.salesdate) AS first_sale
FROM orionbelt_1.clients cl
LEFT JOIN orionbelt_1.countries co ON cl.clientcountryid = co.countryid
LEFT JOIN orionbelt_1.sales s ON s.salesclient = cl.clientid
GROUP BY cl.clientid, co.countryname, cl.clientgender
"""


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    _require_seed()
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["measures"]["Total Sales Or Zero"] = {
        **dict(raw["measures"]["Total Sales"]),
        "defaultValue": 0,
    }
    raw["metrics"].update(_METRICS)
    # The sales date with no declared grain, so a query can select the date
    # itself beside a grain of it.
    raw["dimensions"]["Raw Date"] = {
        "dataObject": "Sales",
        "column": "Sales Date",
        "resultType": "date",
    }
    raw["dimensions"]["Raw Date Alias"] = {**raw["dimensions"]["Raw Date"]}
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


@pytest.fixture(scope="module")
def run(
    model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection
) -> Callable[..., list[dict[str, Any]]]:
    pipeline = CompilationPipeline()

    def _run(dimensions: list[str], measures: list[str], **query: Any) -> list[dict[str, Any]]:
        q = QueryObject(select=QuerySelect(dimensions=dimensions, measures=measures), **query)
        return _rows_as_dicts(commerce_db, pipeline.compile(q, model, "duckdb").sql)

    return _run


@pytest.fixture(scope="module")
def clients(commerce_db: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    return _rows_as_dicts(commerce_db, _CLIENTS)


def _close(got: Any, want: float | Decimal) -> bool:
    return abs(Decimal(str(got)) - Decimal(str(want))) < Decimal("0.01")


def test_every_client_counts(run: Callable, clients: list[dict[str, Any]]) -> None:
    """By country: the average over every client, a non-buyer as 0, and the
    count of every client."""
    rows = run(
        ["Country Name"],
        ["Avg Client Sales All", "Avg Client Orders All", "Min Client Orders All", "Clients All"],
    )
    assert rows
    for r in rows:
        mine = [c for c in clients if c["country"] == r["Country Name"]]
        assert r["Clients All"] == len(mine), r
        assert _close(r["Avg Client Sales All"], sum(c["sales"] for c in mine) / len(mine)), r
        assert _close(r["Avg Client Orders All"], sum(c["orders"] for c in mine) / len(mine)), r
        assert r["Min Client Orders All"] == min(c["orders"] for c in mine), r


def test_facts_population_counts_only_buyers(run: Callable, clients: list[dict[str, Any]]) -> None:
    """The default population: the same measure averages over buyers only, so
    where a country has a non-buyer the two differ."""
    rows = run(["Country Name"], ["Avg Client Sales All", "Avg Client Sales Buyers"])
    differ = 0
    for r in rows:
        buyers = [c for c in clients if c["country"] == r["Country Name"] and c["orders"]]
        assert _close(r["Avg Client Sales Buyers"], sum(c["sales"] for c in buyers) / len(buyers))
        differ += r["Avg Client Sales All"] != r["Avg Client Sales Buyers"]
    assert differ > 0


def test_without_a_default_a_non_buyer_is_null(
    run: Callable, clients: list[dict[str, Any]]
) -> None:
    """``Total Sales`` has no ``defaultValue``: a non-buyer's value is NULL,
    which the average skips."""
    rows = run(["Country Name"], ["Avg Client Sales No Default", "Avg Client Sales Buyers"])
    for r in rows:
        assert r["Avg Client Sales No Default"] == r["Avg Client Sales Buyers"], r


def test_without_dimensions(run: Callable, clients: list[dict[str, Any]]) -> None:
    (row,) = run([], ["Avg Client Orders All", "Clients All"])
    assert row["Clients All"] == len(clients) == 500
    assert _close(row["Avg Client Orders All"], sum(c["orders"] for c in clients) / 500)


def test_member_filter_limits_the_members(run: Callable, clients: list[dict[str, Any]]) -> None:
    """A filter on the clients' own column limits who counts."""
    where = [QueryFilter(field="Client Gender", op="=", value="F")]
    (row,) = run([], ["Clients All", "Avg Client Orders All"], where=where)
    women = [c for c in clients if c["gender"] == "F"]
    assert row["Clients All"] == len(women)
    assert _close(row["Avg Client Orders All"], sum(c["orders"] for c in women) / len(women))


def test_fact_filter_limits_the_measure_not_the_members(
    run: Callable, commerce_db: duckdb.DuckDBPyConnection
) -> None:
    """A filter on the sales date limits the orders counted: every client still
    counts, one without orders in the range as 0."""
    where = [QueryFilter(field="Sales Date", op=">=", value="2022-01-01")]
    (row,) = run([], ["Clients All", "Avg Client Orders All"], where=where)
    (orders,) = commerce_db.sql(
        "SELECT COUNT(*) FROM orionbelt_1.sales WHERE salesdate >= DATE '2022-01-01'"
    ).fetchone()
    assert orders > 0
    assert row["Clients All"] == 500
    assert _close(row["Avg Client Orders All"], orders / 500)


# Each country's and year's order count, and each country's clients.
_YEARS = """
SELECT co.countryname AS country, year(s.salesdate) AS yr, COUNT(*) AS orders
FROM orionbelt_1.sales s
JOIN orionbelt_1.clients cl ON s.salesclient = cl.clientid
LEFT JOIN orionbelt_1.countries co ON cl.clientcountryid = co.countryid
GROUP BY ALL
"""


def test_every_client_in_every_year(
    run: Callable, clients: list[dict[str, Any]], commerce_db: duckdb.DuckDBPyConnection
) -> None:
    """A sales year reads no clients' data object: each client counts in every
    year, one without orders in it as 0. The rows are still the query's: the
    years a country has orders in."""
    years = _rows_as_dicts(commerce_db, _YEARS)
    rows = run(["Country Name", "Sales Year"], ["Clients All", "Avg Client Orders All"])
    assert len(rows) == len(years)
    for r in rows:
        (want,) = [
            y
            for y in years
            if y["country"] == r["Country Name"] and y["yr"] == r["Sales Year"].year
        ]
        mine = len([c for c in clients if c["country"] == r["Country Name"]])
        assert r["Clients All"] == mine, r
        assert _close(r["Avg Client Orders All"], Decimal(want["orders"]) / mine), r


def test_every_client_in_every_month(run: Callable, commerce_db: duckdb.DuckDBPyConnection) -> None:
    """By month alone, every client counts in each month; a filter on the
    sales date limits the months, not the clients."""
    where = [QueryFilter(field="Sales Date", op=">=", value="2022-01-01")]
    rows = run(["Sales Month"], ["Clients All", "Avg Client Orders All"], where=where)
    months = dict(
        commerce_db.sql(
            "SELECT CAST(date_trunc('month', salesdate) AS DATE), COUNT(*) FROM orionbelt_1.sales "
            "WHERE salesdate >= DATE '2022-01-01' GROUP BY ALL"
        ).fetchall()
    )
    assert len(rows) == len(months) > 1
    for r in rows:
        assert r["Clients All"] == 500, r
        assert _close(r["Avg Client Orders All"], Decimal(months[r["Sales Month"]]) / 500), r


@pytest.mark.parametrize(
    ("measure", "per_month"),
    [
        ("Peak Month All", "COUNT(*) / 500"),
        ("Peak Month Buyers", "COUNT(*) / COUNT(DISTINCT salesclient)"),
    ],
    ids=["every client", "buyers"],
)
def test_nested_month_under_a_year(
    run: Callable, commerce_db: duckdb.DuckDBPyConnection, measure: str, per_month: str
) -> None:
    """By year, the peak month of the average client's orders: the outer
    metric passes ``Sales Date:month`` down beside the query's
    ``Sales Date:year``, and the
    inner metric keeps the two apart, not joining and grouping the months by
    year."""
    want = dict(
        commerce_db.sql(
            f"""
            SELECT CAST(date_trunc('year', m) AS DATE), MAX(v) FROM (
                SELECT date_trunc('month', salesdate) AS m, {per_month} AS v
                FROM orionbelt_1.sales GROUP BY ALL
            ) GROUP BY ALL
            """
        ).fetchall()
    )
    rows = run(["Sales Date:year"], [measure])
    assert len(rows) == len(want)
    for r in rows:
        assert _close(r[measure], want[r["Sales Date"]]), r


def test_member_filter_with_a_period(run: Callable, clients: list[dict[str, Any]]) -> None:
    """A filter on the clients still limits who counts in each year."""
    where = [QueryFilter(field="Client Gender", op="=", value="F")]
    rows = run(["Sales Year"], ["Clients All"], where=where)
    women = len([c for c in clients if c["gender"] == "F"])
    assert rows
    assert all(r["Clients All"] == women for r in rows), rows


def test_role_grouping_refused(model: SemanticModel) -> None:
    """A role reaches the clients through the sales: it reads a member object,
    so it is no period, and a client without sales has no value for it."""
    query = QueryObject(
        select=QuerySelect(dimensions=["Sales Client Gender"], measures=["Avg Client Orders All"])
    )
    with pytest.raises(ResolutionError, match="REAGGREGATE_POPULATION_GROUPING|describe"):
        CompilationPipeline().compile(query, model, "duckdb")


def test_beside_a_facts_population_with_the_same_per(run: Callable) -> None:
    """Two populations of one measure and ``per`` keep their own first stages."""
    (row,) = run([], ["Avg Client Sales All", "Avg Client Sales Buyers"])
    assert row["Avg Client Sales All"] < row["Avg Client Sales Buyers"]


def _resolve_with(metric: dict[str, Any]) -> tuple[SemanticModel, Any]:
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["metrics"]["Probe"] = metric
    return ReferenceResolver().resolve(raw, source_map)


def test_role_through_a_fact_refused() -> None:
    """``Sales Client Name`` is the client reached through a sale (``via``):
    its members are the buyers, defined by the facts."""
    _require_seed()
    _model, result = _resolve_with(
        {
            "type": "reaggregate",
            "measure": "Total Sales",
            "per": ["Sales Client Name"],
            "aggregation": "avg",
            "population": "dimension",
        }
    )
    assert "REAGGREGATE_POPULATION_PER" in {e.code for e in result.errors}


def test_members_without_a_common_data_object_refused() -> None:
    """Clients and products meet only in the sales: no one table lists both."""
    _require_seed()
    probe_model, result = _resolve_with(
        {
            "type": "reaggregate",
            "measure": "Total Sales",
            "per": ["Client Name", "Product Name"],
            "aggregation": "avg",
            "population": "dimension",
        }
    )
    assert result.valid, result.errors
    query = QueryObject(select=QuerySelect(measures=["Probe"]))
    with pytest.raises(ResolutionError, match="no data object"):
        CompilationPipeline().compile(query, probe_model, "duckdb")


def test_member_part_of_an_and_group_applies(run: Callable, clients: list[dict[str, Any]]) -> None:
    """``Gender = F and Sales Date >= 2022`` in one group: the gender still
    limits the members, as the same two filters side by side do."""
    gender = QueryFilter(field="Client Gender", op="=", value="F")
    date = QueryFilter(field="Sales Date", op=">=", value="2022-01-01")
    flat = run([], ["Clients All", "Avg Client Orders All"], where=[gender, date])
    grouped = run(
        [],
        ["Clients All", "Avg Client Orders All"],
        where=[QueryFilterGroup(filters=[gender, date])],
    )
    assert grouped == flat
    assert flat[0]["Clients All"] == len([c for c in clients if c["gender"] == "F"])


def test_or_group_mixing_members_and_facts_is_not_split(run: Callable) -> None:
    """``Gender = F or Sales Date >= 2022`` is no condition on the members."""
    group = QueryFilterGroup(
        logic="or",
        filters=[
            QueryFilter(field="Client Gender", op="=", value="F"),
            QueryFilter(field="Sales Date", op=">=", value="2022-01-01"),
        ],
    )
    (row,) = run([], ["Clients All"], where=[group])
    assert row["Clients All"] == 500


def test_root_from_the_whole_per(run: Callable, clients: list[dict[str, Any]]) -> None:
    """``per: [Client Name, Country Name]`` by ``Client Name``: the query covers
    the client, and the members are still listed from the clients - every one
    a row, a client without orders at 0."""
    rows = run(["Client Name"], ["Avg Orders per Client and Country"])
    assert len(rows) == len(clients) == 500
    values = sorted(r["Avg Orders per Client and Country"] for r in rows)
    assert values.count(0) == len([c for c in clients if not c["orders"]]) == 29
    assert all(v >= 1 for v in values[29:])


def test_exists_on_the_facts_does_not_limit_the_members(run: Callable) -> None:
    """An ``exists`` whose subquery reads the sales tests the facts: it limits
    the measure, and the member query, which has no sales, never sees it."""
    has_sales = QueryFilter(
        field="Client Name",
        op=FilterOperator.EXISTS,
        subquery=Subquery(data_object="Sales"),
    )
    (row,) = run([], ["Clients All"], where=[has_sales])
    assert row["Clients All"] == 500


def test_declared_time_grain_refused() -> None:
    """``Sales Month`` declares a month grain: a time bucket, not a member."""
    _require_seed()
    _model, result = _resolve_with(
        {
            "type": "reaggregate",
            "measure": "Total Sales",
            "per": ["Sales Month"],
            "aggregation": "avg",
            "population": "dimension",
        }
    )
    assert "REAGGREGATE_POPULATION_PER" in {e.code for e in result.errors}


@pytest.mark.parametrize("field", ["Country Name", "Countries.Country Name"])
def test_exists_on_member_objects_limits_the_members(
    run: Callable, clients: list[dict[str, Any]], field: str
) -> None:
    """An ``exists`` into the clients' country names the members' objects only,
    however its filter names the column: it limits the members."""
    in_germany = QueryFilter(
        field="Client Name",
        op=FilterOperator.EXISTS,
        subquery=Subquery(
            data_object="Countries",
            filter=[QueryFilter(field=field, op="=", value="Germany")],
        ),
    )
    (row,) = run([], ["Clients All"], where=[in_germany])
    assert row["Clients All"] == len([c for c in clients if c["country"] == "Germany"])


@pytest.fixture(scope="module")
def sale_date_model() -> SemanticModel:
    """The clients with a column computed from the sales, and metrics over it."""
    _require_seed()
    raw, source_map = TrackedLoader().load(COMMERCE_MODEL_YAML)
    raw["dataObjects"]["Clients"]["columns"]["Client Sale Date"] = {
        "expression": "{[Sales].[Sales Date]}",
        "abstractType": "date",
    }
    raw["dimensions"]["Client Sale Date"] = {
        "dataObject": "Clients",
        "column": "Client Sale Date",
        "resultType": "date",
    }
    raw["metrics"]["Clients All"] = _METRICS["Clients All"]
    raw["metrics"]["Clients by Sale Date"] = {
        **_METRICS["Clients All"],
        "per": ["Client Name", "Client Sale Date"],
    }
    resolved, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return resolved


def test_computed_member_column_reading_the_facts(
    sale_date_model: SemanticModel, commerce_db: duckdb.DuckDBPyConnection
) -> None:
    """A clients column computed from the sales reads a fact: a filter on it
    limits the measure, and stays out of the member query, which has no sales."""
    query = QueryObject(
        select=QuerySelect(measures=["Clients All"]),
        where=[QueryFilter(field="Client Sale Date", op=">=", value="2022-01-01")],
    )
    sql = CompilationPipeline().compile(query, sale_date_model, "duckdb").sql
    ((count,),) = commerce_db.sql(sql).fetchall()
    assert count == 500


@pytest.mark.parametrize(
    ("dimensions", "measure"),
    [(["Client Sale Date"], "Clients All"), ([], "Clients by Sale Date")],
    ids=["grouped by it", "in per"],
)
def test_computed_dimension_reading_the_facts_refused(
    sale_date_model: SemanticModel, dimensions: list[str], measure: str
) -> None:
    """The same column as a dimension, of the query or in ``per``, is no
    property of a client without sales: refused, not compiled to SQL that reads
    a table the member query does not have."""
    query = QueryObject(select=QuerySelect(dimensions=dimensions, measures=[measure]))
    with pytest.raises(ResolutionError, match="Client Sale Date"):
        CompilationPipeline().compile(query, sale_date_model, "duckdb")


def test_every_member_is_a_row_beside_other_measures(
    run: Callable, clients: list[dict[str, Any]]
) -> None:
    """By client, the 29 without sales are rows too. A plain measure beside
    the metric reports what it reports over no rows there: a count 0, a sum
    without a defaultValue NULL."""
    rows = run(["Client Name"], ["Clients All", "Sales Count", "Total Sales"])
    assert len(rows) == 500
    idle = [r for r in rows if r["Sales Count"] == 0]
    assert len(idle) == len([c for c in clients if not c["orders"]]) == 29
    assert all(r["Total Sales"] is None and r["Clients All"] == 1 for r in idle)


@pytest.mark.parametrize(
    "having",
    [
        QueryFilter(field="Sales Count", op=">", value=0),
        QueryFilter(field="Total Sales", op=">", value=50000),
    ],
    ids=["count > 0", "sales > 50000"],
)
def test_having_sees_the_member_rows(
    run: Callable, clients: list[dict[str, Any]], having: QueryFilter
) -> None:
    """A HAVING on another measure filters the final rows, the members' too:
    evaluated before them, the members it removed came back."""
    rows = run(["Client Name"], ["Clients All", "Sales Count", "Total Sales"], having=[having])
    if having.field == "Sales Count":
        assert len(rows) == len([c for c in clients if c["orders"]]) == 471
    else:
        assert all(r["Total Sales"] > 50000 for r in rows)
        assert len(rows) == len([c for c in clients if c["sales"] > 50000]) > 0


def test_on_a_member_row_other_values_are_over_no_rows(run: Callable) -> None:
    """On the 29 clients without sales: a formula over the metric reads its
    other components as over no rows (a count 0, a sum's defaultValue 0), a
    formula of plain measures is its value over no rows, and a facts-population
    count counted no group."""
    rows = run(
        ["Client Name"],
        ["Sales Count", "Clients Plus Orders", "Clients Plus Sales", "Orders Doubled", "Buyers"],
    )
    idle = [r for r in rows if r["Sales Count"] == 0]
    assert len(idle) == 29
    for r in idle:
        assert r["Clients Plus Orders"] == 1, r
        assert r["Clients Plus Sales"] == 1, r
        assert r["Orders Doubled"] == 0, r
        assert r["Buyers"] == 0, r
    busy = [r for r in rows if r["Sales Count"]]
    assert all(r["Buyers"] == 1 for r in busy)
    assert all(r["Orders Doubled"] == 2 * r["Sales Count"] for r in busy)


def _idle_client(commerce_db: duckdb.DuckDBPyConnection) -> str:
    (name,) = commerce_db.sql(
        "SELECT clientname FROM orionbelt_1.clients "
        "WHERE clientid NOT IN (SELECT salesclient FROM orionbelt_1.sales) "
        "ORDER BY clientname LIMIT 1"
    ).fetchone()
    return str(name)


@pytest.mark.parametrize("field", ["Client Name", "Clients.Client Name"])
def test_having_on_a_qualified_column_sees_the_member_rows(
    run: Callable, commerce_db: duckdb.DuckDBPyConnection, field: str
) -> None:
    """The dimension and its qualified column are one predicate: both keep
    only the client named, a member row among the rest."""
    name = _idle_client(commerce_db)
    rows = run(
        ["Client Name"], ["Clients All"], having=[QueryFilter(field=field, op="=", value=name)]
    )
    assert [r["Client Name"] for r in rows] == [name]


@pytest.mark.parametrize(
    "measures",
    [["Count Rank"], ["Rank Plus One"], ["Count Rank", "Rank Plus One"]],
    ids=["rank", "formula over it", "both"],
)
def test_a_window_on_a_member_row_is_null(
    run: Callable, commerce_db: duckdb.DuckDBPyConnection, measures: list[str]
) -> None:
    """A window ranks the base's rows; a member row has no place among them,
    so the rank is NULL, and so is a formula over it, selected beside it or
    not - not the rank's components read as over no rows."""
    name = _idle_client(commerce_db)
    rows = run(["Client Name"], ["Clients All", *measures])
    (idle,) = [r for r in rows if r["Client Name"] == name]
    assert all(idle[m] is None for m in measures), idle


@pytest.mark.parametrize(
    "dims",
    [["Raw Date", "Raw Date:month"], ["Raw Date:month", "Raw Date"]],
    ids=["date first", "month first"],
)
def test_having_on_a_date_reads_the_date_not_a_grain_of_it(run: Callable, dims: list[str]) -> None:
    """``Raw Date = 2022-01-10`` beside ``Raw Date:month`` compares the dates,
    in either order: the predicate names the date, not its month."""
    rows = run(
        dims,
        ["Clients All", "Sales Count"],
        having=[QueryFilter(field="Raw Date", op="=", value="2022-01-10")],
    )
    assert len(rows) == 1
    assert rows[0]["Clients All"] == 500 and rows[0]["Sales Count"] > 0


@pytest.mark.parametrize(
    "measures",
    [["One"], ["Orders Plus One"], ["One", "Orders Plus One"]],
    ids=["constant", "formula", "both"],
)
def test_a_constant_on_a_member_row(
    run: Callable, commerce_db: duckdb.DuckDBPyConnection, measures: list[str]
) -> None:
    """A constant metric is its constant on a member row, and so is a formula
    reading it as over no rows, whether or not the constant is selected."""
    name = _idle_client(commerce_db)
    rows = run(["Client Name"], ["Clients All", *measures])
    (idle,) = [r for r in rows if r["Client Name"] == name]
    assert all(idle[m] == 1 for m in measures), idle


@pytest.mark.parametrize("field", ["Raw Date", "Sales.Sales Date"])
def test_having_on_a_date_reads_another_dimension_of_it(run: Callable, field: str) -> None:
    """``Raw Date:month`` beside ``Raw Date Alias``, a second dimension on the
    same date: ``Raw Date = 2022-01-10`` compares the alias's dates, the
    column's own value, not the named dimension's months."""
    rows = run(
        ["Raw Date:month", "Raw Date Alias"],
        ["Clients All", "Sales Count"],
        having=[QueryFilter(field=field, op="=", value="2022-01-10")],
    )
    assert len(rows) == 1
    assert str(rows[0]["Raw Date Alias"]) == "2022-01-10"

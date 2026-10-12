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


def test_grouping_outside_the_members_refused(model: SemanticModel) -> None:
    """An order month does not describe a client, so it has no members to list."""
    query = QueryObject(
        select=QuerySelect(dimensions=["Sales Month"], measures=["Avg Client Orders All"])
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
    the client, and the members are still listed from the clients."""
    rows = run(["Client Name"], ["Avg Orders per Client and Country"])
    assert len(rows) == len([c for c in clients if c["orders"]])
    assert all(r["Avg Orders per Client and Country"] >= 1 for r in rows)


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

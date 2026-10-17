"""A measure filter on another data object puts that object on the measure's row.

A measure filter is evaluated on the row of the value it gates
(``SUM(CASE WHEN <filter> THEN value END)``). Base selection used to read only
the measure's own columns, so a Customers measure filtered on an Orders column
was planned from Customers, and the SQL named ``"orders"`` in a FROM that never
joins it: the database refused it, after an ``OUT_OF_SCOPE_TABLE`` warning.
Now the filter's object is required on the row: a filter on an object the
measure's source reaches is joined as before, and one that replicates the
measure's rows is refused with a typed error before any SQL is emitted.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.compiler.grain_dedup import GrainDedupUnsupportedError
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
dataObjects:
  Regions:
    code: regions
    schema: main
    columns:
      Region ID: {code: id, abstractType: int, primaryKey: true}
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: int, primaryKey: true}
      Customer Region ID: {code: region_id, abstractType: int}
      Tier: {code: tier, abstractType: string}
      Credit: {code: credit, abstractType: float}
    # One join each for Customers and Orders: the "most joins" tie-break then
    # chose Customers, from which the filter's Orders cannot be reached.
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Customer Region ID]
        columnsTo: [Region ID]
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Customer ID: {code: customer_id, abstractType: int}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
dimensions:
  Tier: {dataObject: Customers, column: Tier, resultType: string}
measures:
  Credit With Orders:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
    filters:
      - column: {dataObject: Orders, column: Order ID}
        operator: set
  Gold Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
    filters:
      - column: {dataObject: Customers, column: Tier}
        operator: equals
        values: [{dataType: string, valueString: gold}]
"""


def _sql(query: dict) -> str:
    raw = YAML(typ="safe").load(MODEL_YAML)
    model: SemanticModel = ReferenceResolver().resolve(raw)[0]
    return CompilationPipeline().compile(QueryObject(**query), model, "duckdb").sql


@pytest.mark.parametrize("dimensions", [[], ["Tier"]], ids=["scalar", "by-tier"])
def test_a_filter_that_replicates_the_measure_is_refused(dimensions: list[str]) -> None:
    """Read through Orders, a customer's credit repeats per order, and the
    filter on Orders keeps it from being deduplicated: refused, not emitted."""
    with pytest.raises(GrainDedupUnsupportedError, match="filters clause references 'Orders'"):
        _sql({"select": {"dimensions": dimensions, "measures": ["Credit With Orders"]}})


def test_a_filter_the_measure_reaches_still_joins() -> None:
    """Orders reaches Customers, so the query is based on Orders and the tier
    filter joins Customers. Under the same tie it was based on Customers and
    the gated Orders amount named a table the FROM did not have."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE customers (id INTEGER, region_id INTEGER, tier VARCHAR, credit DOUBLE)"
    )
    con.execute("INSERT INTO customers VALUES (1, 1, 'gold', 10), (2, 1, 'silver', 20)")
    con.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, amount DOUBLE)")
    con.execute("INSERT INTO orders VALUES (1, 1, 100), (2, 1, 50), (3, 2, 70)")
    sql = _sql({"select": {"measures": ["Gold Revenue"]}})
    assert [float(v) for (v,) in con.execute(sql).fetchall()] == [150.0]

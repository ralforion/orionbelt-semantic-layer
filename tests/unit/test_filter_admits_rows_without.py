"""A WHERE a parent without child rows could satisfy is refused, not answered short.

Customer measures with a WHERE on an Orders column are answered from the order
rows, the customer measures deduplicated. A customer counts only through an
order the predicate accepts, so one without orders is left out: right for
``status = 'ok'``, silently wrong for ``tier = 'gold' OR status = 'ok'`` (Frank
is gold and has no orders) or ``order id IS NULL``.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.ast.nodes import BinaryOp, ColumnRef, FunctionCall, IsNull, Literal, UnaryOp
from orionbelt.compiler.null_rejection import can_hold_without
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.resolver import ReferenceResolver

MODEL_YAML = """
version: 1.0
dataObjects:
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: int, primaryKey: true}
      Tier: {code: tier, abstractType: string}
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Customer ID: {code: customer_id, abstractType: int}
      Status: {code: status, abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
dimensions:
  Tier: {dataObject: Customers, column: Tier, resultType: string}
  Status: {dataObject: Orders, column: Status, resultType: string}
measures:
  Customer Count:
    columns: [{dataObject: Customers, column: Customer ID}]
    aggregation: count
    resultType: int
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE customers (id INTEGER, tier VARCHAR)")
    # Frank (3) is gold and has no orders.
    con.execute("INSERT INTO customers VALUES (1, 'gold'), (2, 'silver'), (3, 'gold')")
    con.execute("CREATE TABLE orders (id INTEGER, customer_id INTEGER, status VARCHAR)")
    con.execute("INSERT INTO orders VALUES (1, 1, 'ok'), (2, 2, 'bad')")
    return con


def _model() -> SemanticModel:
    raw = YAML(typ="safe").load(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw)
    assert not result.errors, result.errors
    return model


def _sql(query: dict) -> str:
    return CompilationPipeline().compile(QueryObject(**query), _model(), "duckdb").sql


def _count(con: duckdb.DuckDBPyConnection, where: list[dict]) -> int:
    sql = _sql({"select": {"measures": ["Customer Count"]}, "where": where})
    return int(con.execute(sql).fetchone()[0])


STATUS_OK = {"field": "Status", "op": "=", "value": "ok"}
GOLD = {"field": "Tier", "op": "=", "value": "gold"}


@pytest.mark.parametrize(
    "where",
    [
        [{"logic": "or", "filters": [GOLD, STATUS_OK]}],
        [{"field": "Orders.Order ID", "op": "notset"}],
        [{"field": "Status", "op": "blank"}],
    ],
    ids=["or-with-parent-term", "child-is-null", "child-blank"],
)
def test_predicate_a_childless_parent_satisfies_is_refused(where: list[dict]) -> None:
    with pytest.raises(ResolutionError) as excinfo:
        _sql({"select": {"measures": ["Customer Count"]}, "where": where})
    errors = excinfo.value.errors
    assert [e.code for e in errors] == ["FILTER_ADMITS_ROWS_WITHOUT"]
    assert "exists" in (errors[0].hint or "")


def test_existential_predicate_still_answers(con: duckdb.DuckDBPyConnection) -> None:
    """No customer without orders has an ok order; only Alice counts."""
    assert _count(con, [STATUS_OK]) == 1


def test_negated_predicate_is_not_refused(con: duckdb.DuckDBPyConnection) -> None:
    """NOT (status = 'bad') is NULL, not TRUE, for a missing order."""
    where = [{"logic": "and", "negated": True, "filters": [{**STATUS_OK, "value": "bad"}]}]
    assert _count(con, where) == 1


def test_exists_form_counts_the_childless_parent(con: duckdb.DuckDBPyConnection) -> None:
    """The form the refusal points to: Alice (ok order) and Frank (gold)."""
    has_ok = {
        "field": "Customers.Customer ID",
        "op": "exists",
        "subquery": {"dataObject": "Orders", "filter": [STATUS_OK]},
    }
    assert _count(con, [{"logic": "or", "filters": [GOLD, has_ok]}]) == 2


def test_nonexists_form_counts_customers_without_orders(con: duckdb.DuckDBPyConnection) -> None:
    where = [
        {"field": "Customers.Customer ID", "op": "nonexists", "subquery": {"dataObject": "Orders"}}
    ]
    assert _count(con, where) == 1


def test_parent_only_filter_beside_a_child_grouping_is_not_refused() -> None:
    """Orders is reached through the grouping, not the filter."""
    _sql({"select": {"dimensions": ["Status"], "measures": ["Customer Count"]}, "where": [GOLD]})


class TestCanHoldWithout:
    """The three-valued check, with the Orders columns NULL."""

    STATUS = ColumnRef(name="status", table="Orders")
    TIER = ColumnRef(name="tier", table="Customers")

    def test_comparison_on_a_null_column_cannot_hold(self) -> None:
        assert not can_hold_without(BinaryOp(self.STATUS, "=", Literal.string("ok")), {"Orders"})

    def test_or_with_an_unknown_term_can_hold(self) -> None:
        expr = BinaryOp(
            BinaryOp(self.TIER, "=", Literal.string("gold")),
            "OR",
            BinaryOp(self.STATUS, "=", Literal.string("ok")),
        )
        assert can_hold_without(expr, {"Orders"})

    def test_is_null_holds(self) -> None:
        assert can_hold_without(IsNull(expr=self.STATUS), {"Orders"})
        assert not can_hold_without(IsNull(expr=self.STATUS, negated=True), {"Orders"})

    def test_not_of_null_stays_null(self) -> None:
        expr = UnaryOp("NOT", BinaryOp(self.STATUS, "=", Literal.string("bad")))
        assert not can_hold_without(expr, {"Orders"})

    def test_coalesce_can_turn_null_into_a_value(self) -> None:
        coalesced = FunctionCall(name="COALESCE", args=[self.STATUS, Literal.string("ok")])
        assert can_hold_without(BinaryOp(coalesced, "=", Literal.string("ok")), {"Orders"})

    def test_null_propagating_function_cannot_hold(self) -> None:
        upper = FunctionCall(name="UPPER", args=[self.STATUS])
        assert not can_hold_without(BinaryOp(upper, "=", Literal.string("OK")), {"Orders"})

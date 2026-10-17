"""A many-side WHERE keeps the parents without child rows it accepts.

Customer measures with a WHERE on an Orders column are answered from the order
rows, the customer measures deduplicated, so a customer counts only through an
order the predicate accepts. Right for ``status = 'ok'``; wrong for a predicate
a customer *without* orders satisfies (``tier = 'gold' OR status = 'ok'``,
``order id IS NULL``), who has no row to be counted through.

The answer meant is ``customers LEFT JOIN orders WHERE <predicate>``, each
customer once. When the predicate can hold for a customer without orders, the
plan adds those customers in a second leg beside the deduplicated ones: the two
sets do not overlap, and the new leg never joins the orders. Every result here
is compared with that LEFT JOIN, written by hand.
"""

from __future__ import annotations

import duckdb
import pytest
from ruamel.yaml import YAML

from orionbelt.ast.nodes import BinaryOp, ColumnRef, FunctionCall, IsNull, Literal, UnaryOp
from orionbelt.compiler.grain_dedup import GrainDedupUnsupportedError
from orionbelt.compiler.null_rejection import can_hold_without
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.dialect import DialectRegistry
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
      Region Name: {code: name, abstractType: string}
  Customers:
    code: customers
    schema: main
    columns:
      Customer ID: {code: id, abstractType: int, primaryKey: true}
      Customer Region ID: {code: region_id, abstractType: int}
      Tier: {code: tier, abstractType: string}
      Credit: {code: credit, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Regions
        columnsFrom: [Customer Region ID]
        columnsTo: [Region ID]
  Products:
    code: products
    schema: main
    columns:
      Product ID: {code: id, abstractType: int, primaryKey: true}
      Product Name: {code: name, abstractType: string}
      Product Or Missing: {expression: "COALESCE({Product Name}, 'missing')", abstractType: string}
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Order Product ID: {code: product_id, abstractType: int}
      Order Customer ID: {code: customer_id, abstractType: int}
      Billing Customer ID: {code: billing_id, abstractType: int}
      Status: {code: status, abstractType: string}
      Amount: {code: amount, abstractType: float}
      Status Or Missing: {expression: "COALESCE({Status}, 'missing')", abstractType: string}
      Status Check: {expression: "NULLIF('ok', {Status})", abstractType: string}
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Order Product ID]
        columnsTo: [Product ID]
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Billing Customer ID]
        columnsTo: [Customer ID]
        secondary: true
        pathName: billing
dimensions:
  Region: {dataObject: Regions, column: Region Name, resultType: string}
  Tier: {dataObject: Customers, column: Tier, resultType: string}
  Status: {dataObject: Orders, column: Status, resultType: string}
  Status Or Missing: {dataObject: Orders, column: Status Or Missing, resultType: string}
  Status Check: {dataObject: Orders, column: Status Check, resultType: string}
  Product Or Missing: {dataObject: Products, column: Product Or Missing, resultType: string}
measures:
  Customer Count:
    columns: [{dataObject: Customers, column: Customer ID}]
    aggregation: count
    resultType: int
  Total Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: sum
    resultType: float
  Max Credit:
    columns: [{dataObject: Customers, column: Credit}]
    aggregation: max
    resultType: float
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
  Order Count:
    columns: [{dataObject: Orders, column: Order ID}]
    aggregation: count
    resultType: int
  Revenue Or Zero:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
    defaultValue: 0
  All Customers:
    columns: [{dataObject: Customers, column: Customer ID}]
    aggregation: count
    resultType: int
    filterContext: {mode: FIXED}
"""


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE TABLE regions (id INTEGER, name VARCHAR)")
    con.execute("INSERT INTO regions VALUES (1, 'North'), (2, 'South')")
    con.execute(
        "CREATE TABLE customers (id INTEGER, region_id INTEGER, tier VARCHAR, credit DOUBLE)"
    )
    # Frank and Gina have no orders; Frank is gold. South has only them.
    con.execute(
        "INSERT INTO customers VALUES (1, 1, 'gold', 100), (2, 1, 'silver', 200),"
        " (3, 2, 'gold', 600), (4, 2, 'bronze', 50)"
    )
    con.execute("CREATE TABLE products (id INTEGER, name VARCHAR)")
    con.execute("INSERT INTO products VALUES (1, 'Pen')")
    con.execute(
        "CREATE TABLE orders (id INTEGER, product_id INTEGER, customer_id INTEGER,"
        " billing_id INTEGER, status VARCHAR, amount DOUBLE)"
    )
    # Alice's ok order is billed to Frank.
    con.execute("INSERT INTO orders VALUES (1, 1, 1, 3, 'ok', 10), (2, 1, 2, 2, 'bad', 20)")
    return con


def _model(yaml_text: str = MODEL_YAML) -> SemanticModel:
    raw = YAML(typ="safe").load(yaml_text)
    model, result = ReferenceResolver().resolve(raw)
    assert not result.errors, result.errors
    return model


def _sql(query: dict, yaml_text: str = MODEL_YAML, dialect: str = "duckdb") -> str:
    return CompilationPipeline().compile(QueryObject(**query), _model(yaml_text), dialect).sql


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[object, ...]]:
    def cell(value: object) -> object:
        return float(value) if isinstance(value, int | float) or hasattr(value, "is_nan") else value

    return sorted((tuple(cell(v) for v in row) for row in con.execute(sql).fetchall()), key=str)


GOLD = {"field": "Tier", "op": "=", "value": "gold"}
STATUS_OK = {"field": "Status", "op": "=", "value": "ok"}
GOLD_OR_OK = [{"logic": "or", "filters": [GOLD, STATUS_OK]}]

LEFT_JOIN = (
    "FROM customers c LEFT JOIN regions r ON r.id = c.region_id"
    " LEFT JOIN orders o ON o.customer_id = c.id"
)


@pytest.mark.parametrize(
    ("where", "condition"),
    [
        (GOLD_OR_OK, "c.tier = 'gold' OR o.status = 'ok'"),
        ([{"field": "Orders.Order ID", "op": "notset"}], "o.id IS NULL"),
        ([{"field": "Status", "op": "blank"}], "o.status IS NULL OR TRIM(o.status) = ''"),
    ],
    ids=["or-with-parent-term", "child-is-null", "child-blank"],
)
def test_parent_only_measures_match_the_left_join(
    con: duckdb.DuckDBPyConnection, where: list[dict], condition: str
) -> None:
    sql = _sql({"select": {"measures": ["Customer Count", "Total Credit"]}, "where": where})
    expected = (
        "SELECT COUNT(*), SUM(credit) FROM (SELECT DISTINCT c.id, c.credit "
        f"{LEFT_JOIN} WHERE {condition})"
    )
    assert _rows(con, sql) == _rows(con, expected)


def test_parent_and_child_measures_by_a_parent_dimension(con: duckdb.DuckDBPyConnection) -> None:
    """South holds only customers without orders: a group the order rows lack."""
    sql = _sql(
        {
            "select": {
                "dimensions": ["Region"],
                "measures": ["Customer Count", "Total Credit", "Revenue", "Order Count"],
            },
            "where": GOLD_OR_OK,
        }
    )
    expected = (
        "SELECT r.name, COUNT(DISTINCT c.id),"
        " (SELECT SUM(credit) FROM customers c2 JOIN regions r2 ON r2.id = c2.region_id"
        "  WHERE r2.name = r.name AND (c2.tier = 'gold' OR EXISTS (SELECT 1 FROM orders o2"
        "  WHERE o2.customer_id = c2.id AND o2.status = 'ok'))),"
        f" SUM(o.amount), COUNT(o.id) {LEFT_JOIN}"
        " WHERE c.tier = 'gold' OR o.status = 'ok' GROUP BY r.name"
    )
    assert _rows(con, sql) == _rows(con, expected)
    assert ("South", 1.0, 600.0, None, 0.0) in _rows(con, sql)


def test_a_child_dimension_puts_them_in_the_null_group(con: duckdb.DuckDBPyConnection) -> None:
    sql = _sql(
        {
            "select": {"dimensions": ["Status"], "measures": ["Customer Count", "Order Count"]},
            "where": GOLD_OR_OK,
        }
    )
    expected = (
        f"SELECT o.status, COUNT(DISTINCT c.id), COUNT(o.id) {LEFT_JOIN}"
        " WHERE c.tier = 'gold' OR o.status = 'ok' GROUP BY o.status"
    )
    assert _rows(con, sql) == _rows(con, expected)


def test_a_measure_duplication_would_not_change_is_split_too(
    con: duckdb.DuckDBPyConnection,
) -> None:
    """MAX is left alone over a replicating join; here it has to see Frank."""
    sql = _sql({"select": {"measures": ["Max Credit"]}, "where": GOLD_OR_OK})
    assert _rows(con, sql) == [(600.0,)]


def test_a_predicate_no_childless_parent_satisfies_has_no_second_leg() -> None:
    sql = _sql({"select": {"measures": ["Customer Count"]}, "where": [STATUS_OK]})
    assert "NOT EXISTS" not in sql


@pytest.mark.parametrize(
    ("query", "yaml_text"),
    [
        (
            {"select": {"measures": ["Customer Count"]}, "where": GOLD_OR_OK},
            MODEL_YAML.replace(
                "    aggregation: count\n    resultType: int\n  Total Credit:",
                "    aggregation: count\n    resultType: int\n    allowFanOut: true\n"
                "  Total Credit:",
            ),
        ),
    ],
    ids=["allow-fan-out"],
)
def test_a_shape_the_split_cannot_express_is_refused(query: dict, yaml_text: str) -> None:
    with pytest.raises(ResolutionError) as excinfo:
        _sql(query, yaml_text)
    errors = excinfo.value.errors
    assert [e.code for e in errors] == ["FILTER_ADMITS_ROWS_WITHOUT"]
    assert "exists" in (errors[0].hint or "")


def test_rollup_counts_the_childless_parent_in_every_set(con: duckdb.DuckDBPyConnection) -> None:
    """Refused under ROLLUP before: each grouping set is deduplicated on its
    own rows, which now include the customers without orders."""
    sql = _sql(
        {
            "select": {"dimensions": ["Tier"], "measures": ["Customer Count"]},
            "grouping": "rollup",
            "where": GOLD_OR_OK,
        }
    )
    expected = (
        "SELECT c.tier, COUNT(DISTINCT c.id), GROUPING(c.tier)"
        f" {LEFT_JOIN} WHERE c.tier = 'gold' OR o.status = 'ok' GROUP BY ROLLUP (c.tier)"
    )
    assert _rows(con, sql) == _rows(con, expected)


@pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
def test_the_split_compiles_on_every_dialect(dialect: str) -> None:
    sql = _sql(
        {
            "select": {"dimensions": ["Region"], "measures": ["Customer Count", "Revenue"]},
            "where": GOLD_OR_OK,
        },
        dialect=dialect,
    )
    assert "NOT EXISTS" in sql


def test_exists_form_counts_the_childless_parent(con: duckdb.DuckDBPyConnection) -> None:
    has_ok = {
        "field": "Customers.Customer ID",
        "op": "exists",
        "subquery": {"dataObject": "Orders", "filter": [STATUS_OK]},
    }
    sql = _sql(
        {
            "select": {"measures": ["Customer Count"]},
            "where": [{"logic": "or", "filters": [GOLD, has_ok]}],
        }
    )
    assert _rows(con, sql) == [(2.0,)]


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


class TestReviewFollowUps:
    """Each one a shape the second leg first got wrong."""

    def test_having_filters_the_finished_groups(self, con: duckdb.DuckDBPyConnection) -> None:
        """North reads 10 and South nothing: no group passes. Applied in
        ``main`` only, the deduplicated CTEs brought both groups back."""
        sql = _sql(
            {
                "select": {"dimensions": ["Region"], "measures": ["Customer Count", "Revenue"]},
                "where": GOLD_OR_OK,
                "having": [{"field": "Revenue", "op": ">", "value": 15}],
            }
        )
        assert _rows(con, sql) == []

    def test_the_anti_join_follows_the_query_path(self, con: duckdb.DuckDBPyConnection) -> None:
        """Through billing, Frank has the ok order and Alice none. An anti-join
        over the primary relationship counted Frank twice."""
        sql = _sql(
            {
                "select": {"measures": ["Customer Count", "Total Credit"]},
                "where": GOLD_OR_OK,
                "usePathNames": [
                    {"source": "Orders", "target": "Customers", "pathName": "billing"}
                ],
            }
        )
        expected = (
            "SELECT COUNT(*), SUM(credit) FROM (SELECT DISTINCT c.id, c.credit FROM customers c"
            " LEFT JOIN orders o ON o.billing_id = c.id"
            " WHERE c.tier = 'gold' OR o.status = 'ok')"
        )
        assert _rows(con, sql) == _rows(con, expected)

    def test_a_computed_child_dimension_is_evaluated(self, con: duckdb.DuckDBPyConnection) -> None:
        sql = _sql(
            {
                "select": {"dimensions": ["Status Or Missing"], "measures": ["Customer Count"]},
                "where": GOLD_OR_OK,
            }
        )
        expected = (
            f"SELECT COALESCE(o.status, 'missing'), COUNT(DISTINCT c.id) {LEFT_JOIN}"
            " WHERE c.tier = 'gold' OR o.status = 'ok' GROUP BY 1"
        )
        assert _rows(con, sql) == _rows(con, expected)
        assert ("missing", 1.0) in _rows(con, sql)

    def test_a_group_only_the_leg_has_takes_declared_defaults(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        sql = _sql(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Customer Count", "Revenue Or Zero"],
                },
                "where": GOLD_OR_OK,
            }
        )
        assert _rows(con, sql) == [("North", 1.0, 10.0), ("South", 1.0, 0.0)]

    def test_nullif_is_not_taken_for_null_propagating(self, con: duckdb.DuckDBPyConnection) -> None:
        """NULLIF('ok', status) is 'ok' for a NULL status."""
        sql = _sql(
            {
                "select": {"measures": ["Customer Count"]},
                "where": [{"field": "Status Check", "op": "=", "value": "ok"}],
            }
        )
        expected = f"SELECT COUNT(DISTINCT c.id) {LEFT_JOIN} WHERE NULLIF('ok', o.status) = 'ok'"
        assert _rows(con, sql) == _rows(con, expected)

    def test_a_fixed_filter_context_measure_reads_every_parent(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        """Its own scan drops the WHERE, so it needs no second leg."""
        sql = _sql(
            {
                "select": {"measures": ["All Customers"]},
                "where": [{"field": "Orders.Order ID", "op": "notset"}],
            }
        )
        assert _rows(con, sql) == [(4.0,)]

    @pytest.mark.parametrize("field", ["Tier", "Customers.Tier"])
    def test_having_on_a_selected_dimension_moves_with_it(
        self, con: duckdb.DuckDBPyConnection, field: str
    ) -> None:
        """However the predicate spells the dimension, it reads its column."""
        sql = _sql(
            {
                "select": {"dimensions": ["Tier"], "measures": ["Customer Count"]},
                "where": GOLD_OR_OK,
                "having": [{"field": field, "op": "like", "value": "g%"}],
            }
        )
        expected = (
            f"SELECT c.tier, COUNT(DISTINCT c.id) {LEFT_JOIN}"
            " WHERE c.tier = 'gold' OR o.status = 'ok' GROUP BY c.tier HAVING c.tier LIKE 'g%'"
        )
        assert _rows(con, sql) == _rows(con, expected)

    def test_a_dimension_beyond_the_child_reads_as_null(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        """Products is reached only through the orders, so for a customer
        without orders its name is NULL and the COALESCE gives 'missing'."""
        sql = _sql(
            {
                "select": {"dimensions": ["Product Or Missing"], "measures": ["Customer Count"]},
                "where": GOLD_OR_OK,
            }
        )
        expected = (
            "SELECT COALESCE(p.name, 'missing'), COUNT(DISTINCT c.id) FROM customers c"
            " LEFT JOIN orders o ON o.customer_id = c.id"
            " LEFT JOIN products p ON p.id = o.product_id"
            " WHERE c.tier = 'gold' OR o.status = 'ok' GROUP BY 1"
        )
        assert _rows(con, sql) == _rows(con, expected)
        assert ("missing", 1.0) in _rows(con, sql)

    def test_having_on_a_column_no_selected_dimension_reads_is_refused(self) -> None:
        with pytest.raises(GrainDedupUnsupportedError, match='"Customers"."credit"'):
            _sql(
                {
                    "select": {"dimensions": ["Tier"], "measures": ["Customer Count"]},
                    "where": GOLD_OR_OK,
                    "having": [{"field": "Customers.Credit", "op": ">", "value": 0}],
                }
            )

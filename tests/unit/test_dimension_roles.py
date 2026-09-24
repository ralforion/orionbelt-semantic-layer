"""Role-playing dimensions: ``via`` + ``pathName`` joins one aliased copy per role."""

from __future__ import annotations

import duckdb
import osi_orionbelt.converter as conv
import yaml
from rdflib import Literal

from orionbelt.compiler.composability import (
    resolve_composables_for_anchors,
    resolve_composables_for_query,
)
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import QueryFilter, QueryObject, QuerySelect, UsePathName
from orionbelt.models.roles import expand_role_objects
from orionbelt.models.semantic import SemanticModel
from orionbelt.obsl.exporter import OBSL, export_obsl
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

ROLE_MODEL_YAML = """\
version: 1.0

dataObjects:
  Orders:
    code: orders
    schema: main
    columns:
      Order ID:
        code: order_id
        abstractType: int
      Sales Employee ID:
        code: sales_employee_id
        abstractType: int
      Support Employee ID:
        code: support_employee_id
        abstractType: int
      Amount:
        code: amount
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Employees
        pathName: sales
        columnsFrom: [Sales Employee ID]
        columnsTo: [Employee ID]
      - joinType: many-to-one
        joinTo: Employees
        secondary: true
        pathName: support
        columnsFrom: [Support Employee ID]
        columnsTo: [Employee ID]

  Employees:
    code: employees
    schema: main
    columns:
      Employee ID:
        code: employee_id
        abstractType: int
      Name:
        code: name
        abstractType: string
      Display Name:
        expression: "upper({[Employees].[Name]})"
        abstractType: string

dimensions:
  Employee Name:
    dataObject: Employees
    column: Name
    resultType: string
  Sales Employee:
    dataObject: Employees
    column: Name
    resultType: string
    via: Orders
    pathName: sales
  Support Employee:
    dataObject: Employees
    column: Name
    resultType: string
    via: Orders
    pathName: support
  Support Employee ID:
    dataObject: Employees
    column: Employee ID
    resultType: int
    via: Orders
    pathName: support
  Support Employee Display:
    dataObject: Employees
    column: Display Name
    resultType: string
    via: Orders
    pathName: support

measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
"""


def _load(yaml_content: str = ROLE_MODEL_YAML) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml_content)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, [e.message for e in result.errors]
    return model


def _validate(yaml_content: str) -> list[SemanticError]:
    raw, source_map = TrackedLoader().load_string(yaml_content)
    model, _ = ReferenceResolver().resolve(raw, source_map)
    return SemanticValidator().validate(model)


def _compile(model: SemanticModel, dims: list[str], **kw: object) -> str:
    query = QueryObject(select=QuerySelect(dimensions=dims, measures=["Revenue"]), **kw)
    return CompilationPipeline().compile(query, model, "duckdb").sql


def _run(sql: str) -> list[tuple[object, ...]]:
    con = duckdb.connect()
    con.execute("CREATE TABLE employees (employee_id INTEGER, name VARCHAR)")
    con.execute("INSERT INTO employees VALUES (1, 'Ann'), (2, 'Bob'), (3, 'Cid')")
    con.execute(
        "CREATE TABLE orders (order_id INTEGER, sales_employee_id INTEGER,"
        " support_employee_id INTEGER, amount DOUBLE)"
    )
    con.execute("INSERT INTO orders VALUES (10, 1, 3, 100), (11, 1, 2, 50), (12, 2, 3, 25)")
    return sorted(con.execute(sql).fetchall(), key=str)


SALES = "Employees__Orders__sales"
SUPPORT = "Employees__Orders__support"


class TestCompilation:
    def test_two_roles_join_the_table_under_two_aliases(self) -> None:
        sql = _compile(_load(), ["Sales Employee", "Support Employee"])
        assert f'AS "{SALES}" ON "Orders"."sales_employee_id"' in sql
        assert f'AS "{SUPPORT}" ON "Orders"."support_employee_id"' in sql

    def test_two_roles_return_each_role_s_rows(self) -> None:
        rows = _run(_compile(_load(), ["Sales Employee", "Support Employee"]))
        assert rows == [("Ann", "Bob", 50), ("Ann", "Cid", 100), ("Bob", "Cid", 25)]

    def test_dimensions_of_one_role_share_one_join(self) -> None:
        sql = _compile(_load(), ["Support Employee", "Support Employee ID"])
        assert sql.count(f'AS "{SUPPORT}"') == 1

    def test_role_is_pinned_when_use_path_names_swaps_the_pair(self) -> None:
        """``usePathNames`` moves the unpinned dimension, never the pinned role."""
        sql = _compile(
            _load(),
            ["Sales Employee", "Employee Name"],
            use_path_names=[UsePathName(source="Orders", target="Employees", path_name="support")],
        )
        rows = _run(sql)
        assert rows == [("Ann", "Bob", 50), ("Ann", "Cid", 100), ("Bob", "Cid", 25)]

    def test_where_filter_on_a_role_dimension(self) -> None:
        sql = _compile(
            _load(),
            ["Sales Employee"],
            where=[QueryFilter(field="Support Employee", op="equals", value="Cid")],
        )
        assert _run(sql) == [("Ann", 100), ("Bob", 25)]

    def test_computed_column_reads_its_own_role(self) -> None:
        sql = _compile(_load(), ["Sales Employee", "Support Employee Display"])
        assert f'upper("{SUPPORT}"."name")' in sql.replace("UPPER", "upper")
        assert _run(sql) == [("Ann", "BOB", 50), ("Ann", "CID", 100), ("Bob", "CID", 25)]

    def test_roles_are_reached_through_an_intermediate_object(self) -> None:
        """A fact below ``via`` joins ``via`` once, then each role from it."""
        items = ROLE_MODEL_YAML.replace(
            "dataObjects:\n",
            """dataObjects:
  Items:
    code: items
    schema: main
    columns:
      Order ID: {code: order_id, abstractType: int}
      Qty: {code: qty, abstractType: int}
    joins:
      - {joinType: many-to-one, joinTo: Orders, columnsFrom: [Order ID], columnsTo: [Order ID]}
""",
            1,
        ).replace(
            "measures:\n",
            "measures:\n  Quantity:\n    columns: [{dataObject: Items, column: Qty}]\n"
            "    resultType: int\n    aggregation: sum\n",
        )
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Sales Employee", "Support Employee"], measures=["Quantity"]
            )
        )
        result = CompilationPipeline().compile(query, _load(items), "postgres")
        assert result.sql.count('JOIN "main"."orders"') == 1
        assert f'AS "{SALES}" ON "Orders"."sales_employee_id"' in result.sql
        assert f'AS "{SUPPORT}" ON "Orders"."support_employee_id"' in result.sql
        assert result.warnings == []

    def test_every_dialect_compiles_two_roles(self) -> None:
        model = _load()
        query = QueryObject(
            select=QuerySelect(
                dimensions=["Sales Employee", "Support Employee"], measures=["Revenue"]
            )
        )
        for dialect in (
            "bigquery",
            "clickhouse",
            "databricks",
            "dremio",
            "duckdb",
            "mysql",
            "postgres",
            "snowflake",
        ):
            result = CompilationPipeline().compile(query, model, dialect)
            assert "support_employee_id" in result.sql, dialect
            assert "sales_employee_id" in result.sql, dialect


class TestExpansion:
    def test_model_without_roles_is_returned_as_is(self) -> None:
        model = _load(ROLE_MODEL_YAML.split("  Sales Employee:")[0] + MEASURES)
        assert expand_role_objects(model) is model

    def test_authored_model_is_not_changed(self) -> None:
        model = _load()
        expanded = expand_role_objects(model)
        assert SUPPORT in expanded.data_objects
        assert SUPPORT not in model.data_objects
        assert model.dimensions["Support Employee"].view == "Employees"

    def test_role_object_is_a_leaf_without_a_count(self) -> None:
        role = expand_role_objects(_load()).data_objects[SUPPORT]
        assert role.joins == []
        assert not role.countable


class TestValidation:
    def test_valid_roles_raise_nothing(self) -> None:
        assert _validate(ROLE_MODEL_YAML) == []

    def test_path_name_needs_via(self) -> None:
        errors = _validate(
            ROLE_MODEL_YAML.replace(
                "    via: Orders\n    pathName: support\n", "    pathName: support\n", 1
            )
        )
        assert [e.code for e in errors] == ["INVALID_DIMENSION_PATH"]

    def test_unknown_path_name_lists_the_declared_ones(self) -> None:
        errors = _validate(
            ROLE_MODEL_YAML.replace(
                "pathName: support\n  Support Employee ID",
                "pathName: helpdesk\n  Support Employee ID",
            )
        )
        assert [e.code for e in errors] == ["INVALID_DIMENSION_PATH"]
        assert errors[0].suggestions == ["sales", "support"]

    def test_via_alone_over_several_joins_warns(self) -> None:
        errors = _validate(
            ROLE_MODEL_YAML.replace(
                "    via: Orders\n    pathName: support\n  Support Employee ID",
                "    via: Orders\n  Support Employee ID",
            )
        )
        warnings = [e for e in errors if e.code == "AMBIGUOUS_VIA"]
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"
        assert "'sales'" in warnings[0].message


COLLIDING_ROLES_YAML = """\
version: 1.0

dataObjects:
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: order_id, abstractType: int}
      First Employee ID: {code: first_employee_id, abstractType: int}
      Amount: {code: amount, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Employees
        pathName: sales__support
        columnsFrom: [First Employee ID]
        columnsTo: [Employee ID]
      - joinType: one-to-one
        joinTo: Orders__sales
        columnsFrom: [Order ID]
        columnsTo: [Order ID]
  Orders__sales:
    code: order_extras
    schema: main
    columns:
      Order ID: {code: order_id, abstractType: int}
      Second Employee ID: {code: second_employee_id, abstractType: int}
    joins:
      - joinType: many-to-one
        joinTo: Employees
        pathName: support
        columnsFrom: [Second Employee ID]
        columnsTo: [Employee ID]
  Employees:
    code: employees
    schema: main
    columns:
      Employee ID: {code: employee_id, abstractType: int}
      Name: {code: name, abstractType: string}

dimensions:
  First Employee:
    dataObject: Employees
    column: Name
    resultType: string
    via: Orders
    pathName: sales__support
  Second Employee:
    dataObject: Employees
    column: Name
    resultType: string
    via: Orders__sales
    pathName: support

measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
"""


class TestAliases:
    def test_roles_whose_names_would_collide_get_distinct_aliases(self) -> None:
        """``Employees__Orders__sales__support`` spells two different roles here."""
        sql = _compile(_load(COLLIDING_ROLES_YAML), ["First Employee", "Second Employee"])
        assert '"Employees__Orders__sales__support__2"' in sql
        con = duckdb.connect()
        con.execute("CREATE TABLE employees (employee_id INTEGER, name VARCHAR)")
        con.execute("INSERT INTO employees VALUES (1, 'Ann'), (2, 'Bob')")
        con.execute(
            "CREATE TABLE orders (order_id INTEGER, first_employee_id INTEGER, amount DOUBLE)"
        )
        con.execute("INSERT INTO orders VALUES (10, 1, 100)")
        con.execute("CREATE TABLE order_extras (order_id INTEGER, second_employee_id INTEGER)")
        con.execute("INSERT INTO order_extras VALUES (10, 2)")
        assert con.execute(sql).fetchall() == [("Ann", "Bob", 100)]

    def test_alias_taken_by_a_data_object_gets_a_suffix(self) -> None:
        taken = ROLE_MODEL_YAML.replace(
            "\ndimensions:",
            f"""  {SUPPORT}:
    code: other
    schema: main
    columns:
      X:
        code: x
        abstractType: int

dimensions:""",
        )
        assert _validate(taken) == []
        sql = _compile(_load(taken), ["Sales Employee", "Support Employee"])
        assert f'AS "{SUPPORT}__2" ON "Orders"."support_employee_id"' in sql
        assert _run(sql) == [("Ann", "Bob", 50), ("Ann", "Cid", 100), ("Bob", "Cid", 25)]

    def test_string_literals_in_a_computed_column_are_left_alone(self) -> None:
        literal = ROLE_MODEL_YAML.replace(
            'expression: "upper({[Employees].[Name]})"',
            "expression: \"concat('{[Employees].[Name]}: ', {[Employees].[Name]})\"",
        )
        sql = _compile(_load(literal), ["Sales Employee", "Support Employee Display"])
        assert "'{[Employees].[Name]}: '" in sql
        assert _run(sql) == [
            ("Ann", "{[Employees].[Name]}: Bob", 50),
            ("Ann", "{[Employees].[Name]}: Cid", 100),
            ("Bob", "{[Employees].[Name]}: Cid", 25),
        ]


SECONDARY_ONLY_YAML = ROLE_MODEL_YAML.replace(
    """      - joinType: many-to-one
        joinTo: Employees
        pathName: sales
        columnsFrom: [Sales Employee ID]
        columnsTo: [Employee ID]
""",
    "",
).replace(
    """  Sales Employee:
    dataObject: Employees
    column: Name
    resultType: string
    via: Orders
    pathName: sales
""",
    "",
)


class TestComposability:
    """Discovery sees roles the way the compiler does."""

    def test_measure_composes_with_a_role_behind_a_secondary_join(self) -> None:
        model = _load(SECONDARY_ONLY_YAML)
        assert "Support Employee" in resolve_composables_for_anchors(model, ["Revenue"]).dimensions
        # And the compiler agrees.
        assert _run(_compile(model, ["Support Employee"])) == [("Bob", 50), ("Cid", 125)]

    def test_role_anchor_reports_its_data_object(self) -> None:
        result = resolve_composables_for_anchors(_load(), ["Support Employee"])
        assert result.anchor_objects == ["Employees"]
        assert "Revenue" in result.measures

    def test_query_with_two_roles_keeps_its_measure(self) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Sales Employee", "Support Employee"], measures=[])
        )
        assert "Revenue" in resolve_composables_for_query(_load(), query).measures


class TestPropagation:
    def test_osi_round_trip_keeps_every_role(self) -> None:
        obml = yaml.safe_load(ROLE_MODEL_YAML)
        back = conv.OSItoOBML(conv.OBMLtoOSI(obml, model_name="s").convert()).convert()
        for name in ("Sales Employee", "Support Employee", "Support Employee Display"):
            assert back["dimensions"][name].get("pathName") == obml["dimensions"][name]["pathName"]

    def test_ontology_export_carries_the_path_name(self) -> None:
        graph = export_obsl(_load(), "m")
        assert set(graph.objects(None, OBSL.pathName)) >= {Literal("sales"), Literal("support")}
        dims_with_path = set(graph.subjects(OBSL.pathName, Literal("support")))
        assert len(dims_with_path) == 4  # the join and three dimensions


MEASURES = ROLE_MODEL_YAML[ROLE_MODEL_YAML.index("measures:") :]

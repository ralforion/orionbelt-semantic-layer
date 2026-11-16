"""``%``, ``||``, ``[NOT] ILIKE`` and ``IS [NOT] DISTINCT FROM`` in OBML expressions.

Standard SQL that Apache Ossie's expression language has and OBML's parser did
not: a measure or computed column using one stopped the whole model loading. Each
parses into nodes every dialect already renders, so none needs a rendering of its
own: ``%`` is the catalog's ``mod``, ``||`` its ``concat``, ``ILIKE`` the
case-insensitive match query filters use, and ``IS [NOT] DISTINCT FROM`` one
NULL-safe comparison (``<=>`` on MySQL). The execution matrix on all eight
engines is
``tests/integration/drift/vendor_exec/test_expression_operators_exec.py``.
"""

from __future__ import annotations

import duckdb
import pytest

import orionbelt.dialect  # noqa: F401  -- triggers dialect registrations
from orionbelt.ast.nodes import BinaryOp, FunctionCall, LikeMatch, Literal
from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_DIALECTS = [
    "bigquery",
    "clickhouse",
    "databricks",
    "dremio",
    "duckdb",
    "mysql",
    "postgres",
    "snowflake",
]


def _parse(text: str):  # type: ignore[no-untyped-def]
    return parse_expression(tokenize_metric_formula(text))


def _duckdb(text: str) -> object:
    sql = DialectRegistry.get("duckdb").compile_expr(_parse(text))
    return duckdb.connect().execute(f"SELECT {sql}").fetchone()[0]  # type: ignore[index]


class TestParse:
    def test_modulo_is_the_catalog_mod(self) -> None:
        assert _parse("7 % 3") == FunctionCall(name="mod", args=list(_parse("mod(7, 3)").args))

    def test_modulo_binds_like_multiplication(self) -> None:
        ast = _parse("1 + 7 % 3 * 2")
        assert isinstance(ast, BinaryOp) and ast.op == "+"
        assert isinstance(ast.right, BinaryOp) and ast.right.op == "*"
        assert isinstance(ast.right.left, FunctionCall) and ast.right.left.name == "mod"

    def test_concatenation_is_one_concat_call(self) -> None:
        ast = _parse("'a' || 'b' || 'c'")
        assert isinstance(ast, FunctionCall) and ast.name == "concat"
        assert len(ast.args) == 3

    def test_concatenation_binds_looser_than_addition(self) -> None:
        # 'n' || 1 + 2 is 'n' || (1 + 2), as in PostgreSQL.
        ast = _parse("'n' || 1 + 2 = 'n3'")
        assert isinstance(ast, BinaryOp) and ast.op == "="
        assert isinstance(ast.left, FunctionCall) and ast.left.name == "concat"
        assert isinstance(ast.left.args[1], BinaryOp) and ast.left.args[1].op == "+"

    @pytest.mark.parametrize(("text", "negated"), [("ILIKE", False), ("NOT ILIKE", True)])
    def test_ilike_is_a_case_insensitive_match(self, text: str, negated: bool) -> None:
        ast = _parse(f"'Done' {text} 'do%'")
        assert ast == LikeMatch(
            column=_parse("'Done'"), pattern="do%", negated=negated, case_insensitive=True
        )

    def test_ilike_takes_a_string_pattern(self) -> None:
        with pytest.raises(ValueError, match="ILIKE must be followed by a string pattern"):
            _parse("'a' ILIKE upper('b')")

    @pytest.mark.parametrize("op", ["IS DISTINCT FROM", "IS NOT DISTINCT FROM"])
    def test_is_distinct_from_is_one_comparison(self, op: str) -> None:
        assert _parse(f"1 {op} NULL") == BinaryOp(
            left=Literal.number(1), op=op, right=Literal.null()
        )

    @pytest.mark.parametrize("dialect", _DIALECTS)
    def test_is_distinct_from_reads_each_operand_once(self, dialect: str) -> None:
        sql = DialectRegistry.get(dialect).compile_expr(
            _parse("upper({[a]}) IS DISTINCT FROM lower({[b]})")
        )
        assert sql.upper().count("UPPER(") == 1, sql
        assert sql.upper().count("LOWER(") == 1, sql

    @pytest.mark.parametrize(
        ("dialect", "text", "expected"),
        [
            # Each binds looser than the IS NULL around it.
            (
                "dremio",
                "('foo' NOT ILIKE 'FOO') IS NULL",
                "(NOT ILIKE('foo', 'FOO', '\\')) IS NULL",
            ),
            ("mysql", "(1 IS DISTINCT FROM 2) IS NULL", "(NOT (1 <=> 2)) IS NULL"),
            ("mysql", "(1 > 0) IS NOT DISTINCT FROM (2 > 3)", "(1 > 0) <=> (2 > 3)"),
        ],
    )
    def test_a_negated_rendering_keeps_its_parentheses(
        self, dialect: str, text: str, expected: str
    ) -> None:
        assert DialectRegistry.get(dialect).compile_expr(_parse(text)) == expected

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            ("1 IS DISTINCT 2", "IS DISTINCT must be followed by FROM"),
            ("1 IS TRUE", "IS predicate must be followed by NULL"),
        ],
    )
    def test_malformed(self, text: str, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            _parse(text)

    def test_a_simple_case_refuses_an_ilike_condition(self) -> None:
        with pytest.raises(ValueError, match="takes a value, not a condition"):
            _parse("CASE 'a' WHEN 'b' ILIKE 'c' THEN 1 END")


class TestValues:
    """DuckDB answers as SQL defines each operator."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("7 % 3", 1),
            ("-7 % 3", -1),
            ("'a' || 'b' || 'c'", "abc"),
            ("'a' || NULL", None),
            ("'Done' ILIKE 'do%'", True),
            ("'Done' NOT ILIKE 'DO%'", False),
            ("1 IS DISTINCT FROM 2", True),
            ("1 IS DISTINCT FROM 1", False),
            ("1 IS DISTINCT FROM NULL", True),
            ("NULL IS DISTINCT FROM NULL", False),
            ("1 IS NOT DISTINCT FROM NULL", False),
            ("NULL IS NOT DISTINCT FROM NULL", True),
            # A comparison as an operand of IS NULL or BETWEEN keeps its
            # parentheses, bounds included.
            ("(1 IS DISTINCT FROM 2) IS NULL", False),
            ("(1 IS DISTINCT FROM 1) BETWEEN FALSE AND TRUE", True),
            ("FALSE BETWEEN TRUE AND (1 IS DISTINCT FROM 2)", False),
            ("TRUE BETWEEN (1 IS DISTINCT FROM 1) AND TRUE", True),
            ("TRUE NOT BETWEEN (1 IS DISTINCT FROM 1) AND (1 = 1)", False),
        ],
    )
    def test_value(self, text: str, expected: object) -> None:
        assert _duckdb(text) == expected

    def test_a_volatile_operand_is_evaluated_once(self) -> None:
        # Repeated, a random operand differs within the row and the result is
        # sometimes NULL; native IS DISTINCT FROM never is.
        sql = DialectRegistry.get("duckdb").compile_expr(
            _parse("(CASE WHEN random() < 0.5 THEN 1 END) IS DISTINCT FROM 1")
        )
        rows = (
            duckdb.connect()
            .execute(f"SELECT {sql} AS r, count(*) FROM range(2000) GROUP BY r")
            .fetchall()
        )
        assert {r for r, _ in rows} == {True, False}


_MODEL = """\
version: 1.0
dataObjects:
  Orders:
    code: orders
    columns:
      ID: {code: id, abstractType: int}
      Status: {code: status, abstractType: string}
      Previous: {code: previous_status, abstractType: string}
      Amount: {code: amount, abstractType: float, numClass: additive}
      Label:
        expression: "{Status} || '-' || {Previous}"
        abstractType: string
dimensions:
  Label: {dataObject: Orders, column: Label, resultType: string}
measures:
  Done:
    expression: "CASE WHEN {[Orders].[Status]} ILIKE 'do%' THEN {[Orders].[Amount]} END"
    aggregation: sum
    resultType: float
  Even:
    expression: "CASE WHEN {[Orders].[ID]} % 2 = 0 THEN {[Orders].[Amount]} END"
    aggregation: sum
    resultType: float
  Changed:
    expression: "CASE WHEN {[Orders].[Status]} IS DISTINCT FROM {[Orders].[Previous]} THEN 1 END"
    aggregation: count
    resultType: int
"""


class TestModel:
    def _model(self):  # type: ignore[no-untyped-def]
        raw, source_map = TrackedLoader().load_string(_MODEL)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        return model

    def test_the_model_loads(self) -> None:
        self._model()

    @pytest.mark.parametrize("dialect", _DIALECTS)
    def test_compiles_on_every_dialect(self, dialect: str) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Label"], measures=["Done", "Even", "Changed"])
        )
        sql = CompilationPipeline().compile(query, self._model(), dialect).sql
        assert "%" not in sql.replace("'do%'", "")
        if dialect == "mysql":
            assert "||" not in sql

    def test_runs_on_duckdb(self) -> None:
        query = QueryObject(
            select=QuerySelect(dimensions=["Label"], measures=["Done", "Even", "Changed"])
        )
        sql = CompilationPipeline().compile(query, self._model(), "duckdb").sql
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE orders AS SELECT * FROM (VALUES"
            " (1, 'Done', 'open', 10.0), (2, 'DONE', 'DONE', 20.0), (3, 'open', NULL, 5.0))"
            " t(id, status, previous_status, amount)"
        )
        rows = sorted(con.execute(sql).fetchall(), key=lambda r: str(r[0]))
        assert rows == [
            ("DONE-DONE", 20.0, 20.0, 0),
            ("Done-open", 10.0, None, 1),
            (None, None, None, 1),
        ]

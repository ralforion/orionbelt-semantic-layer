"""An unnest reaches the SQL through the AST, not only the dialect method.

``Select.joins`` holds joins and unnests in one list, because the order between
them matters: an unnest names its parent, so it has to follow whatever put that
parent in scope. Keeping two lists would make the planner interleave them again
at render time, from information it no longer has.

Compile-only on purpose, so CI runs it. The per-dialect *fragments* are executed
against real engines in
``tests/integration/drift/vendor_exec/test_unnest_render_exec.py``, which CI
does not run.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.builder import QueryBuilder
from orionbelt.ast.nodes import AliasedExpr, ColumnRef, FunctionCall, Unnest
from orionbelt.dialect.base import UnsupportedNestedAccessError
from orionbelt.dialect.registry import DialectRegistry

RENDERS = ["bigquery", "clickhouse", "databricks", "duckdb", "mysql", "postgres", "snowflake"]


def _ast(outer: bool = True, dialect: str = "duckdb"):
    # The field accessor is the dialect's, not a generic ColumnRef: on Snowflake
    # the alias is a row whose `value` holds the element, so `L."Key"` does not
    # compile at all. Building the AST with a hard-coded ColumnRef made this
    # test pass while the SQL it described was invalid there (review of #344).
    field = DialectRegistry.get(dialect).nested_field("L", "Key")
    return (
        QueryBuilder()
        .select(AliasedExpr(expr=field, alias="Label Key"))
        .select(
            AliasedExpr(
                expr=FunctionCall(name="SUM", args=[ColumnRef(name="cost", table="C")]),
                alias="Cost",
            )
        )
        .from_("charges", alias="C")
        .unnest(
            Unnest(
                parent_alias="C",
                column="x_Labels",
                alias="L",
                columns=(("Key", "VARCHAR(64)"),),
                outer=outer,
            )
        )
        .group_by(field)
        .build()
    )


@pytest.mark.parametrize("dialect", RENDERS)
def test_the_unnest_reaches_the_sql(dialect: str) -> None:
    sql = DialectRegistry.get(dialect).compile_select(_ast(dialect=dialect))
    assert "x_Labels" in sql, sql


@pytest.mark.parametrize("dialect", RENDERS)
def test_it_lands_after_the_from_it_names(dialect: str) -> None:
    """Ordering is the reason the two share a list. A fragment naming ``C``
    before ``FROM ... AS C`` is a syntax error on every engine.
    """
    sql = DialectRegistry.get(dialect).compile_select(_ast(dialect=dialect))
    assert sql.index("FROM") < sql.index("x_Labels"), sql


@pytest.mark.parametrize("dialect", RENDERS)
def test_both_forms_differ(dialect: str) -> None:
    """Inner drops a parent whose array is empty; outer keeps it. If a dialect
    rendered them identically, one of the two would be silently wrong.
    """
    outer = DialectRegistry.get(dialect).compile_select(_ast(outer=True, dialect=dialect))
    inner = DialectRegistry.get(dialect).compile_select(_ast(outer=False, dialect=dialect))
    assert outer != inner, f"{dialect} renders inner and outer the same: {outer}"


def test_dremio_refuses_rather_than_emitting_something_that_will_not_parse() -> None:
    """``FLATTEN`` is a projection function, so Dremio's unnest needs a derived
    table rather than a FROM-clause fragment. That is a query restructure and
    belongs with the planner; until then the error names the ``code`` fallback.
    """
    with pytest.raises(UnsupportedNestedAccessError, match="no FROM-clause unnest"):
        DialectRegistry.get("dremio").compile_select(_ast(dialect="duckdb"))


class TestAddressingAFieldOfTheElement:
    """``nested_field`` exists because one engine does not use column access.

    Both cases below were found in review of #344, and both are the kind that a
    string-shaped unit test passes while the SQL is invalid.
    """

    @pytest.mark.parametrize("dialect", [d for d in RENDERS if d != "snowflake"])
    def test_most_engines_read_it_as_a_column(self, dialect: str) -> None:
        dia = DialectRegistry.get(dialect)
        assert dia.compile_expr(dia.nested_field("L", "Key")).endswith(dia.quote_identifier("Key"))

    def test_snowflake_reads_a_variant_path_and_casts_it(self) -> None:
        """``L."Key"`` does not compile there - measured, "SQL compilation
        error" - and without the cast a string field comes back as ``"team"``
        with its JSON quotes still attached.
        """
        dia = DialectRegistry.get("snowflake")
        assert dia.compile_expr(dia.nested_field("L", "Key")) == '"L".value:"Key"::string'
        assert dia.compile_expr(dia.nested_field("L", "n", "number")) == '"L".value:"n"::number'

    def test_a_field_name_with_a_space_is_addressable_everywhere(self) -> None:
        for dialect in RENDERS:
            dia = DialectRegistry.get(dialect)
            assert "Label Key" in dia.compile_expr(dia.nested_field("L", "Label Key"))


class TestMySQLJsonPaths:
    """``JSON_TABLE`` paths are quoted unconditionally.

    Measured: ``$.Label Key`` raises "Invalid JSON path expression", and
    ``$.a.b`` **silently returns NULL** by reading a nested key rather than the
    literal one. The silent case is why this is not conditional on the name
    looking unsafe.
    """

    @pytest.mark.parametrize("code", ["Label Key", "a.b", "plain"])
    def test_the_member_is_quoted(self, code: str) -> None:  # noqa: D102
        node = Unnest(
            parent_alias="C", column="x", alias="L", columns=((code, "VARCHAR(64)"),), outer=True
        )
        assert f"""PATH '$."{code}"'""" in DialectRegistry.get("mysql").render_unnest(node)

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("plain", """'$."plain"'"""),
            ("has space", """'$."has space"'"""),
            ("a.b", """'$."a.b"'"""),
            # a double quote: escaped for JSON, then its backslash escaped again
            # for the SQL literal it sits inside
            ('q"t', """'$."q\\\\"t"'"""),
            # an apostrophe: invisible to the JSON layer, closes the SQL literal
            ("q't", """'$."q''t"'"""),
            # a backslash: escaped by both layers, so four in the output
            ("a\\b", """'$."a\\\\\\\\b"'"""),
        ],
    )
    def test_the_path_is_escaped_for_both_layers(self, code: str, expected: str) -> None:
        """The path is a JSON-path expression inside a SQL string literal.

        Escaping only the JSON layer looked right and failed three ways against
        MySQL 8 - an invalid path, a syntax error, and a silent NULL. Found in
        review of #344, after the first fix covered only spaces and dots.
        """
        node = Unnest(
            parent_alias="C", column="x", alias="L", columns=((code, "VARCHAR(64)"),), outer=True
        )
        assert f"PATH {expected}" in DialectRegistry.get("mysql").render_unnest(node)

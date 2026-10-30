"""How a string becomes SQL text, per dialect.

Two conventions, and each is *wrong* on the other side rather than merely
unnecessary - which is why this is a dialect property rather than one escape
applied everywhere:

* **Standard SQL** (DuckDB, PostgreSQL, Dremio): a quote is escaped by doubling
  it and a backslash is an ordinary character. Backslash-escaping here would
  double the backslash and break ``it's`` outright.
* **Backslash-escaping** (MySQL, ClickHouse, BigQuery, Snowflake, Databricks):
  a backslash starts an escape sequence, so it and the quote are both escaped
  with one. Doubling the quote here raises on BigQuery, which reads ``'it''s'``
  as two concatenated literals, and **silently returns ``its``** on Databricks.

All eight measured. Values are round-tripped through live engines in
``tests/integration/drift/vendor_exec/test_string_literal_exec.py``; what is
pinned here is which convention each dialect uses, so a new dialect has to
choose deliberately.
"""

from __future__ import annotations

import pytest

from orionbelt.ast.nodes import ColumnRef, FunctionCall, Literal
from orionbelt.dialect.base import UnsupportedAggregationError
from orionbelt.dialect.registry import DialectRegistry

STANDARD = ["duckdb", "postgres", "dremio"]
BACKSLASH = ["mysql", "clickhouse", "bigquery", "snowflake", "databricks"]


def test_every_dialect_has_chosen() -> None:
    assert sorted(STANDARD + BACKSLASH) == sorted(DialectRegistry.available())


@pytest.mark.parametrize("dialect", STANDARD)
def test_standard_sql_doubles_the_quote_and_leaves_the_backslash(dialect: str) -> None:
    dia = DialectRegistry.get(dialect)
    assert not dia.backslash_escapes_strings
    assert dia.quote_string_literal("it's") == "'it''s'"
    assert dia.quote_string_literal("a\\b") == "'a\\b'"


@pytest.mark.parametrize("dialect", BACKSLASH)
def test_backslash_dialects_escape_both(dialect: str) -> None:
    dia = DialectRegistry.get(dialect)
    assert dia.backslash_escapes_strings
    assert dia.quote_string_literal("it's") == "'it\\'s'"
    assert dia.quote_string_literal("a\\b") == "'a\\\\b'"


@pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
def test_the_literal_node_goes_through_it(dialect: str) -> None:
    """The bug was not the escaping rule but that several places had their own.

    A filter value, a LISTAGG separator and a time-zone name all built their own
    literal and all made the same wrong choice.
    """
    dia = DialectRegistry.get(dialect)
    assert dia.compile_expr(Literal.string("a\\b")) == dia.quote_string_literal("a\\b")


@pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
def test_an_ordinary_value_is_untouched(dialect: str) -> None:
    dia = DialectRegistry.get(dialect)
    assert dia.quote_string_literal("plain") == "'plain'"


class TestControlCharacters:
    """A real newline, not the two characters that spell one.

    The first round of this work tested ``"tab\\there"`` - a literal backslash
    and a t - so a genuine control character was never exercised. BigQuery is
    the one engine of the eight that minds: a quoted string cannot span lines
    there, and a raw newline fails the query with "Unclosed string literal".
    The other seven hand a newline, carriage return, tab, form feed or control
    byte straight back.
    """

    @pytest.mark.parametrize("dialect", BACKSLASH)
    def test_a_newline_is_written_as_an_escape(self, dialect: str) -> None:
        dia = DialectRegistry.get(dialect)
        assert dia.quote_string_literal("a\nb") == "'a\\nb'"
        assert dia.quote_string_literal("a\rb") == "'a\\rb'"

    @pytest.mark.parametrize("dialect", STANDARD)
    def test_standard_sql_carries_it_literally(self, dialect: str) -> None:
        """No escape sequences exist there, and a quoted string may span lines -
        measured on Postgres, DuckDB and Dremio.
        """
        dia = DialectRegistry.get(dialect)
        assert dia.quote_string_literal("a\nb") == "'a\nb'"

    @pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
    def test_a_tab_needs_no_escape_anywhere(self, dialect: str) -> None:
        """Measured raw on all eight. Only the line terminators are escaped, so
        the rule stays "escape what an engine cannot take" rather than a blanket
        control-character pass.
        """
        assert "\t" in DialectRegistry.get(dialect).quote_string_literal("a\tb")


class TestDremioOutsideLatin1:
    """Dremio types a plain literal as Latin-1, so other text goes through ``_UTF8``.

    Measured on 26.0: ``'İstanbul'`` fails to plan as a value of its own,
    ``_UTF8'İstanbul'`` fails compared with a column, and
    ``SUBSTR(_UTF8'İstanbul', 1)`` runs in both and is pushed down to the
    scan. Round-tripped live in ``test_string_literal_exec.py``.
    """

    def test_latin1_keeps_the_plain_literal(self) -> None:
        dia = DialectRegistry.get("dremio")
        assert dia.quote_string_literal("Straße café ÿ") == "'Straße café ÿ'"

    @pytest.mark.parametrize("value", ["İstanbul", "€", "東京", "😀"])
    def test_any_other_text_is_read_as_utf8(self, value: str) -> None:
        dia = DialectRegistry.get("dremio")
        assert dia.quote_string_literal(value) == f"SUBSTR(_UTF8'{value}', 1)"

    def test_the_quote_is_still_doubled(self) -> None:
        dia = DialectRegistry.get("dremio")
        assert dia.quote_string_literal("it's €") == "SUBSTR(_UTF8'it''s €', 1)"

    def test_the_other_dialects_write_it_plainly(self) -> None:
        for dialect in STANDARD:
            if dialect != "dremio":
                assert DialectRegistry.get(dialect).quote_string_literal("€") == "'€'"

    def test_a_latin1_listagg_separator_is_written(self) -> None:
        dia = DialectRegistry.get("dremio")
        sql = dia.compile_expr(
            FunctionCall(name="LISTAGG", args=[ColumnRef(name="city")], separator=" · ")
        )
        assert sql == "LISTAGG(\"city\", ' · ')"

    def test_a_separator_outside_latin1_is_refused(self) -> None:
        """Measured: Dremio returned ``' → '`` as bytes that are not UTF-8."""
        dia = DialectRegistry.get("dremio")
        call = FunctionCall(name="LISTAGG", args=[ColumnRef(name="city")], separator=" → ")
        with pytest.raises(UnsupportedAggregationError, match="outside Latin-1"):
            dia.compile_expr(call)


@pytest.mark.parametrize("dialect", DialectRegistry.available())
def test_decimal_literal_renders_exactly_in_plain_notation(dialect: str) -> None:
    """A filter value past a float's precision stays exact, never in E notation."""
    from decimal import Decimal

    compile_expr = DialectRegistry.get(dialect).compile_expr
    assert compile_expr(Literal(value=Decimal("9007199254740993.00"))) == "9007199254740993.00"
    assert compile_expr(Literal(value=Decimal("1E-10"))) == "0.0000000001"


def test_an_oversized_decimal_literal_is_not_rendered() -> None:
    from decimal import Decimal

    with pytest.raises(ValueError, match="out of range"):
        DialectRegistry.get("postgres").compile_expr(Literal(value=Decimal("1e100000")))

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

from orionbelt.ast.nodes import Literal
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

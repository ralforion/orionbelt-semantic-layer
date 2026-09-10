"""Splitting a simple-query message into statements.

Every case here is one a real client sends, and the ones that matter are the
semicolons that are *not* boundaries. A naive ``sql.split(";")`` passes the
first three tests below and corrupts the rest, which is the whole reason this
is a scanner.
"""

from __future__ import annotations

import pytest

from orionbelt.pgwire.statements import split_statements


class TestTheOrdinaryCases:
    def test_a_single_statement_is_unchanged(self) -> None:
        assert split_statements("SELECT 1") == ["SELECT 1"]

    def test_a_trailing_semicolon_does_not_make_an_empty_statement(self) -> None:
        assert split_statements("SELECT 1;") == ["SELECT 1"]

    def test_two_statements(self) -> None:
        assert split_statements("SELECT 1; SELECT 2") == ["SELECT 1", "SELECT 2"]

    def test_empty_statements_are_dropped(self) -> None:
        assert split_statements("SELECT 1;;; SELECT 2;") == ["SELECT 1", "SELECT 2"]

    def test_an_empty_message(self) -> None:
        assert split_statements("") == []
        assert split_statements("   \n  ") == []

    def test_the_batch_duckdb_actually_sends(self) -> None:
        """Verbatim in shape from the `postgres` extension's catalog probe."""
        sql = (
            "BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY; "
            "SELECT oid, nspname FROM pg_namespace ORDER BY oid; "
            "SELECT pg_namespace.oid AS namespace_id, relname FROM pg_class"
        )
        parts = split_statements(sql)
        assert len(parts) == 3
        assert parts[0].startswith("BEGIN")
        assert parts[2].endswith("pg_class")


class TestSemicolonsThatAreNotBoundaries:
    """Where a naive split corrupts the SQL instead of dividing it."""

    def test_inside_a_string_literal(self) -> None:
        assert split_statements("SELECT ';' AS x") == ["SELECT ';' AS x"]

    def test_a_doubled_quote_inside_a_literal(self) -> None:
        sql = "SELECT 'it''s; fine' AS x"
        assert split_statements(sql) == [sql]

    def test_inside_a_quoted_identifier(self) -> None:
        sql = 'SELECT "a;b" FROM t'
        assert split_statements(sql) == [sql]

    def test_a_doubled_quote_inside_an_identifier(self) -> None:
        sql = 'SELECT "a""b;c" FROM t'
        assert split_statements(sql) == [sql]

    def test_inside_an_escape_string(self) -> None:
        r"""``E'...'`` lets a backslash escape the quote, unlike ``'...'``."""
        sql = "SELECT E'a\\';b' AS x"
        assert split_statements(sql) == [sql]

    def test_inside_a_line_comment(self) -> None:
        sql = "SELECT 1 -- ; not a boundary\n"
        assert split_statements(sql) == ["SELECT 1 -- ; not a boundary"]

    def test_inside_a_block_comment(self) -> None:
        sql = "SELECT /* ; */ 1"
        assert split_statements(sql) == [sql]

    def test_inside_a_nested_block_comment(self) -> None:
        """Postgres nests these; most splitters stop at the first ``*/``.

        The comment stays attached to the statement that follows it, which is
        where it belongs - stripping comments is the parser's business, not
        the splitter's. What matters here is that none of the three
        semicolons inside it divided anything.
        """
        parts = split_statements("SELECT 1; /* ; /* ; */ ; */ SELECT 2")
        assert len(parts) == 2
        assert parts[0] == "SELECT 1"
        assert parts[1].endswith("SELECT 2")

    def test_inside_a_dollar_quoted_body(self) -> None:
        sql = "SELECT $$a;b$$ AS x"
        assert split_statements(sql) == [sql]

    def test_inside_a_tagged_dollar_quoted_body(self) -> None:
        sql = "SELECT $tag$a;b$tag$ AS x"
        assert split_statements(sql) == [sql]

    def test_a_dollar_placeholder_is_not_a_quote(self) -> None:
        """``$1`` is a parameter, and treating it as a quote opener would
        swallow the rest of the batch."""
        parts = split_statements("SELECT $1; SELECT $2")
        assert parts == ["SELECT $1", "SELECT $2"]


class TestUnterminatedInput:
    """Malformed SQL is the server's to reject, not this function's.

    Each of these returns the remainder as one statement so the error comes
    from the parser with a message about the actual problem, rather than from
    a splitter that silently divided a string in half.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 'unterminated; SELECT 2",
            'SELECT "unterminated; SELECT 2',
            "SELECT $$unterminated; SELECT 2",
            "SELECT /* unterminated; SELECT 2",
        ],
    )
    def test_the_remainder_stays_one_statement(self, sql: str) -> None:
        assert len(split_statements(sql)) == 1

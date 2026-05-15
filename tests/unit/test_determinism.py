"""Tests for cache.determinism — non-deterministic SQL detection.

The cache hashes on compiled SQL; same SQL must mean same result. SQL
that reads the clock, RNG, or samples violates that invariant. These
tests pin down the detector's recognition and false-positive resistance.
"""

from __future__ import annotations

import pytest

from orionbelt.cache.determinism import is_nondeterministic_sql


class TestRandomFunctions:
    @pytest.mark.parametrize(
        "fn",
        [
            "RAND",
            "RANDOM",
            "UUID",
            "UUID_STRING",
            "NEWID",
            "GEN_RANDOM_UUID",
            "GENERATEUUIDV4",
            "RANDOMBYTES",
            "RANDOM_BYTES",
        ],
    )
    def test_random_function_detected(self, fn: str) -> None:
        nondet, name = is_nondeterministic_sql(f"SELECT {fn}() FROM t")
        assert nondet
        assert name == fn


class TestClockFunctions:
    @pytest.mark.parametrize(
        "fn",
        [
            "NOW",
            "GETDATE",
            "GETUTCDATE",
            "SYSTIMESTAMP",
            "UNIX_TIMESTAMP",
        ],
    )
    def test_clock_function_call_detected(self, fn: str) -> None:
        nondet, name = is_nondeterministic_sql(f"SELECT {fn}() FROM t")
        assert nondet
        assert name == fn

    @pytest.mark.parametrize(
        "kw",
        [
            "CURRENT_DATE",
            "CURRENT_TIMESTAMP",
            "CURRENT_TIME",
            "LOCALTIME",
            "LOCALTIMESTAMP",
            "SYSDATE",
        ],
    )
    def test_bare_clock_keyword_detected(self, kw: str) -> None:
        nondet, name = is_nondeterministic_sql(f"SELECT {kw} FROM t")
        assert nondet
        assert name == kw

    def test_current_date_in_where_clause_detected(self) -> None:
        """The canonical trap: rolling window with CURRENT_DATE in WHERE."""
        sql = "SELECT SUM(amount) FROM sales WHERE date >= CURRENT_DATE - INTERVAL '7 days'"
        nondet, name = is_nondeterministic_sql(sql)
        assert nondet
        assert name == "CURRENT_DATE"

    def test_current_date_with_parens_detected(self) -> None:
        """Some dialects allow CURRENT_DATE()."""
        nondet, name = is_nondeterministic_sql("SELECT CURRENT_DATE()")
        assert nondet
        assert name == "CURRENT_DATE"


class TestSamplingClauses:
    def test_tablesample_detected(self) -> None:
        sql = "SELECT * FROM sales TABLESAMPLE BERNOULLI(10)"
        nondet, name = is_nondeterministic_sql(sql)
        assert nondet
        assert name == "TABLESAMPLE"


class TestCaseInsensitive:
    def test_lowercase_rand(self) -> None:
        nondet, name = is_nondeterministic_sql("SELECT rand() FROM t")
        assert nondet
        assert name == "RAND"

    def test_mixed_case_now(self) -> None:
        nondet, name = is_nondeterministic_sql("SELECT NoW() FROM t")
        assert nondet
        assert name == "NOW"

    def test_mixed_case_bare_keyword(self) -> None:
        nondet, name = is_nondeterministic_sql("SELECT cUrReNt_DaTe FROM t")
        assert nondet
        assert name == "CURRENT_DATE"


class TestFalsePositives:
    def test_aggregate_functions_pass(self) -> None:
        sql = "SELECT SUM(amount), AVG(price), COUNT(*), MIN(x), MAX(y) FROM sales"
        nondet, name = is_nondeterministic_sql(sql)
        assert not nondet
        assert name is None

    def test_string_literal_containing_rand(self) -> None:
        """A string literal 'RAND()' must not trigger."""
        sql = "SELECT 'RAND()' AS label, SUM(x) FROM t"
        nondet, name = is_nondeterministic_sql(sql)
        assert not nondet

    def test_string_literal_containing_current_date(self) -> None:
        sql = "SELECT 'as of CURRENT_DATE' AS note FROM t"
        nondet, name = is_nondeterministic_sql(sql)
        assert not nondet

    def test_quoted_identifier_named_now(self) -> None:
        """A column quoted as "NOW" must not match the NOW() pattern."""
        sql = 'SELECT "NOW" FROM t'
        nondet, name = is_nondeterministic_sql(sql)
        assert not nondet

    def test_quoted_identifier_named_rand(self) -> None:
        sql = 'SELECT "RAND", SUM(x) FROM t'
        nondet, name = is_nondeterministic_sql(sql)
        assert not nondet

    def test_doubled_quote_escape_in_string(self) -> None:
        """SQL string escape: 'it''s RAND' has 'RAND' inside a string."""
        sql = "SELECT 'it''s RAND()' AS msg FROM t"
        nondet, name = is_nondeterministic_sql(sql)
        assert not nondet

    def test_column_name_today_is_safe(self) -> None:
        """``today`` as a column reference (no parens, no keyword conflict).

        ``TODAY`` is in the blocklist as a function form. Bare ``today`` (no
        parens) does NOT match the bare-keyword regex (which only lists
        SQL-standard reserved keywords), so it correctly passes.
        """
        sql = "SELECT today FROM date_dim"
        nondet, name = is_nondeterministic_sql(sql)
        assert not nondet

    def test_function_named_random_value_passes_when_not_in_list(self) -> None:
        """A function not in the blocklist must pass even if names sound random."""
        sql = "SELECT pseudo_random_seed() FROM t"
        nondet, name = is_nondeterministic_sql(sql)
        assert not nondet


class TestEdgeCases:
    def test_empty_string(self) -> None:
        nondet, name = is_nondeterministic_sql("")
        assert not nondet
        assert name is None

    def test_whitespace_only(self) -> None:
        nondet, name = is_nondeterministic_sql("   \n\t  ")
        assert not nondet
        assert name is None

    def test_first_match_wins(self) -> None:
        """When multiple non-det functions appear, returns the first by position."""
        sql = "SELECT NOW(), RAND() FROM t"
        nondet, name = is_nondeterministic_sql(sql)
        assert nondet
        assert name == "NOW"

    def test_typical_deterministic_query(self) -> None:
        sql = (
            'SELECT "Region", SUM("amount") AS "Revenue" '
            'FROM "sales" GROUP BY "Region" ORDER BY "Region" LIMIT 100'
        )
        nondet, name = is_nondeterministic_sql(sql)
        assert not nondet

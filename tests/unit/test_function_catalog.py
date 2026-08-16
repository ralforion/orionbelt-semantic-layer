"""Portable scalar-function catalog — catalog integrity, validation, rendering.

The catalog (``models/functions.py``) pins what a function *means*; these tests
pin that the three surfaces agree with it: the scanner and validator that check
a call, the dialects that render one, and the examples that claim a value.

The full per-dialect emit matrix lives in
``tests/integration/drift/test_drift_functions.py`` (golden SQL) and the
executed values in ``tests/integration/drift/vendor_exec/test_function_exec.py``.
"""

from __future__ import annotations

import re

import pytest

import orionbelt.dialect  # noqa: F401 -- triggers dialect registration
from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
from orionbelt.dialect.base import DialectCapabilities, UnsupportedFunctionError
from orionbelt.dialect.duckdb import DuckDBDialect
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.errors import SemanticError
from orionbelt.models.expressions import (
    BOOLEAN_KEYWORDS,
    SQL_KEYWORDS,
    FunctionCallRef,
    find_function_calls,
)
from orionbelt.models.functions import (
    FUNCTION_CATALOG,
    FunctionSpec,
    catalog_names,
    lookup_function,
)
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

CATALOG = list(FUNCTION_CATALOG.values())
DIALECTS = sorted(DialectRegistry.available())


_CALL_START = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*\(")
_INDEX_SUFFIX = re.compile(r"^(\[[^\]]*\])?$")


def _is_atomic(sql: str) -> bool:
    """Whether *sql* can be dropped into a larger expression unbracketed.

    Three shapes qualify: a parenthesised expression, a single function call
    (optionally with an array index, as ClickHouse's ``split_part`` rewrite
    has), and a ``CASE ... END``, which is self-delimiting. Anything else
    reaches its parent as a bare infix expression.
    """
    text = sql.strip()
    if text.startswith("CASE ") and text.endswith(" END"):
        return True
    starts_call = bool(_CALL_START.match(text))
    if not text.startswith("(") and not starts_call:
        return False
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                tail = text[index + 1 :]
                return bool(_INDEX_SUFFIX.match(tail))
    return False


def _render(call: str, dialect: str) -> str:
    """Compile a canonical catalog call to *dialect* SQL."""
    ast = parse_expression(tokenize_metric_formula(call))
    return DialectRegistry.get(dialect).compile_expr(ast)


class TestCatalogIntegrity:
    """The catalog is data other code trusts; keep it internally consistent."""

    @pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
    def test_key_matches_name(self, spec: FunctionSpec) -> None:
        assert FUNCTION_CATALOG[spec.name] is spec
        assert spec.name == spec.name.lower()

    @pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
    def test_signature_starts_with_the_name(self, spec: FunctionSpec) -> None:
        assert spec.signature.startswith(f"{spec.name}(")

    @pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
    def test_arity_bounds_are_ordered(self, spec: FunctionSpec) -> None:
        assert spec.min_args >= 0
        if spec.max_args is not None:
            assert spec.max_args >= spec.min_args

    @pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
    def test_a_unit_argument_is_within_the_arity(self, spec: FunctionSpec) -> None:
        if spec.unit_argument is not None:
            assert spec.unit_argument < spec.min_args

    @pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
    def test_every_entry_carries_an_example(self, spec: FunctionSpec) -> None:
        """An entry without an example is a claim no test can check."""
        assert spec.examples

    @pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
    def test_examples_parse_and_match_the_declared_arity(self, spec: FunctionSpec) -> None:
        """An example calls its own entry, with an arity the entry accepts.

        Usually it is the whole expression; an entry with no constant value of
        its own (``current_date()``) is pinned by composition instead, so the
        call is looked for anywhere in the example rather than only at the top.
        """
        for example in spec.examples:
            calls = find_function_calls(example.call)
            own = [call for call in calls if call.name == spec.name]
            assert own, f"{example.call} never calls {spec.name}"
            assert all(spec.accepts(call.arg_count) for call in own)
            parse_expression(tokenize_metric_formula(example.call))

    def test_lookup_is_case_insensitive(self) -> None:
        assert lookup_function("SUBSTRING") is FUNCTION_CATALOG["substring"]
        assert lookup_function("Substring") is FUNCTION_CATALOG["substring"]
        assert lookup_function("no_such_function") is None

    def test_catalog_names_are_sorted(self) -> None:
        assert catalog_names() == sorted(FUNCTION_CATALOG)


class TestFunctionCallScanner:
    """``find_function_calls`` feeds the validator; it must not invent calls."""

    def test_reports_name_and_arguments(self) -> None:
        assert find_function_calls("substring({Zip}, 1, 5)") == [
            FunctionCallRef(name="substring", arguments=("{Zip}", "1", "5"))
        ]

    def test_argument_text_is_kept_for_the_unit_check(self) -> None:
        """The date entries take a literal unit the renderers switch on, so
        the validator needs the argument as written, not just how many.
        """
        call = find_function_calls("date_trunc('month', {Sales Date})")[0]
        assert call.arguments == ("'month'", "{Sales Date}")
        assert call.arg_count == 2

    def test_nested_calls_are_reported_innermost_first(self) -> None:
        names = [c.name for c in find_function_calls("upper(trim({X}))")]
        assert names == ["trim", "upper"]

    def test_empty_argument_list_counts_zero(self) -> None:
        assert find_function_calls("current_date()")[0].arg_count == 0

    def test_in_predicate_is_not_a_call(self) -> None:
        assert find_function_calls("{A} IN (1, 2, 3)") == []

    def test_commas_inside_literals_are_not_separators(self) -> None:
        assert find_function_calls("split_part({P}, ',', 2)")[0].arg_count == 3

    def test_commas_inside_a_reference_are_not_separators(self) -> None:
        assert find_function_calls("length({[Sales, Inc].[Amount]})")[0].arg_count == 1

    def test_grouping_parens_are_not_calls(self) -> None:
        assert find_function_calls("({A} + {B}) * 2") == []

    @pytest.mark.parametrize(
        "expression",
        [
            "CASE WHEN {A} > 1 THEN (2 + 3) ELSE (4) END",
            "{A} BETWEEN (1) AND (2)",
            "{A} LIKE ('x')",
            "NOT ({A} IS NULL)",
        ],
    )
    def test_no_grammar_keyword_is_read_as_a_call(self, expression: str) -> None:
        """A keyword followed by ``(`` is not a function, whichever keyword.

        The scanner and the tokenizer share one keyword set for this reason:
        while the scanner held its own shorter copy, ``THEN (2 + 3)`` was
        reported as a one-argument call to ``THEN``.
        """
        assert find_function_calls(expression) == []

    def test_the_scanner_and_the_tokenizer_agree_on_keywords(self) -> None:
        """Anything the tokenizer emits as an operator is not a call name."""
        for keyword in BOOLEAN_KEYWORDS | SQL_KEYWORDS:
            assert find_function_calls(f"{keyword} ({{A}})") == []
            tokens = tokenize_metric_formula(keyword)
            assert [t.kind for t in tokens] == ["op"]


_ARITY_MODEL_YAML = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Zip:
        code: ZIP
        abstractType: string
      Bad Zip:
        abstractType: string
        expression: "{EXPRESSION}"

dimensions:
  Bad Zip:
    dataObject: Orders
    column: Bad Zip
    resultType: string

measures:
  Order Count:
    columns: [{dataObject: Orders, column: Order ID}]
    resultType: int
    aggregation: count
"""


def _validate(expression: str) -> list[SemanticError]:
    """Semantic errors for a model whose computed column is *expression*."""
    yaml_text = _ARITY_MODEL_YAML.replace("{EXPRESSION}", expression)
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return SemanticValidator().validate(model)


def _errors_for(expression: str) -> list[str]:
    """Semantic error codes for a model whose computed column is *expression*."""
    return [e.code for e in _validate(expression)]


class TestArityValidation:
    """A catalog function called wrongly is a model error, not a runtime one."""

    def test_too_many_arguments_is_rejected(self) -> None:
        assert "WRONG_FUNCTION_ARITY" in _errors_for("substring({Zip}, 1, 5, 9)")

    def test_too_few_arguments_is_rejected(self) -> None:
        assert "WRONG_FUNCTION_ARITY" in _errors_for("substring({Zip})")

    def test_correct_arity_passes(self) -> None:
        assert _errors_for("substring({Zip}, 1, 5)") == []

    def test_optional_argument_may_be_omitted(self) -> None:
        assert _errors_for("substring({Zip}, 2)") == []

    def test_variadic_entry_accepts_more_arguments(self) -> None:
        assert _errors_for("concat({Zip}, {Zip}, {Zip}, {Zip})") == []

    def test_uncatalogued_function_is_not_checked(self) -> None:
        """The escape hatch: a vendor function's arity is not ours to know."""
        assert _errors_for("regexp_extract({Zip}, '[0-9]{5}', 1, 2, 3)") == []

    def test_error_names_the_signature(self) -> None:
        errors = _validate("length({Zip}, 2)")
        error = next(e for e in errors if e.code == "WRONG_FUNCTION_ARITY")
        assert "length(x)" in (error.hint or "")
        assert error.path == "dataObjects.Orders.columns.Bad Zip.expression"
        assert error.context == {
            "function": "length",
            "argCount": 2,
            "signature": "length(x)",
        }


class TestTimeUnits:
    """The date entries take a literal unit, and every dialect switches on it."""

    def test_a_misspelled_unit_is_rejected(self) -> None:
        assert "UNKNOWN_TIME_UNIT" in _errors_for("date_trunc('monht', {Zip})")

    def test_an_expression_unit_is_rejected(self) -> None:
        """It could not be compiled: the unit is a keyword on some engines and
        a whole different expression per unit on others.
        """
        assert "UNKNOWN_TIME_UNIT" in _errors_for("date_trunc({Zip}, {Zip})")

    def test_a_valid_unit_passes(self) -> None:
        assert _errors_for("date_trunc('month', {Zip})") == []

    def test_the_unit_is_case_insensitive(self) -> None:
        assert _errors_for("extract('WEEK', {Zip})") == []

    def test_a_rejected_unit_still_compiles_to_the_authors_call(self) -> None:
        """Codegen does not raise on what the validator reports, for the same
        reason a wrong arity does not: the database error stays recognisable.
        """
        assert _render("date_trunc('monht', {[S].[D]})", "duckdb") == (
            "date_trunc('monht', \"S].[D\")"
        )


class TestCompilerGeneratedCalls:
    """The planner builds date_trunc calls of its own, for time grains.

    Those now flow through the catalog like any other call, which is intended:
    one rendering for one function. What must not happen is the catalog
    *mangling* them, and BigQuery is where that would show, because its
    ``render_time_grain`` already emits the engine's own value-first order.
    """

    def test_a_time_grain_dimension_still_compiles_per_dialect(self) -> None:
        from orionbelt.ast.nodes import ColumnRef
        from orionbelt.models.semantic import TimeGrain

        column = ColumnRef(name="salesdate", table="Sales")
        rendered = {
            dialect: DialectRegistry.get(dialect).compile_expr(
                DialectRegistry.get(dialect).render_time_grain(column, TimeGrain.MONTH)
            )
            for dialect in DIALECTS
        }
        assert rendered["duckdb"] == 'DATE_TRUNC(\'month\', "Sales"."salesdate")'
        # BigQuery's own order is value-first, so the unit argument is not a
        # literal and the call is left exactly as the planner built it.
        assert rendered["bigquery"] == "DATE_TRUNC(`Sales`.`salesdate`, MONTH)"
        # ClickHouse and MySQL never went through date_trunc at all.
        assert rendered["clickhouse"] == 'toStartOfMonth("Sales"."salesdate")'
        assert "DATE_FORMAT" in rendered["mysql"]


class TestTypedLiterals:
    """``DATE '2026-08-15'`` — without it the date group could not be written."""

    def test_a_date_literal_parses_and_casts(self) -> None:
        assert _render("DATE '2026-08-15'", "duckdb") == "CAST('2026-08-15' AS DATE)"

    def test_a_timestamp_literal_parses(self) -> None:
        assert _render("TIMESTAMP '2026-08-15 13:45:00'", "duckdb") == (
            "CAST('2026-08-15 13:45:00' AS TIMESTAMP)"
        )

    def test_a_date_literal_composes_into_a_call(self) -> None:
        assert _render("date_trunc('month', DATE '2026-08-15')", "duckdb") == (
            "DATE_TRUNC('month', CAST('2026-08-15' AS DATE))"
        )


class TestPinnedSemantics:
    """The D3 rules: where engines disagree on the answer, the renderer bends.

    One assertion per rule, on the dialects that have to be bent — a plausible
    implementation of each of these silently returns a different value rather
    than failing.
    """

    @pytest.mark.parametrize("dialect", ["duckdb", "postgres"])
    def test_concat_becomes_an_operator_chain_where_concat_skips_nulls(self, dialect: str) -> None:
        assert _render("concat('a', NULL, 'c')", dialect) == "('a' || NULL || 'c')"

    def test_concat_is_null_guarded_on_dremio(self) -> None:
        sql = _render("concat('a', 'b')", "dremio")
        assert sql.startswith("CASE WHEN 'a' IS NULL OR 'b' IS NULL THEN NULL ELSE CONCAT(")

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [("clickhouse", "lengthUTF8('äbcd')"), ("mysql", "CHAR_LENGTH('äbcd')")],
    )
    def test_length_counts_characters_where_length_counts_bytes(
        self, dialect: str, expected: str
    ) -> None:
        assert _render("length('äbcd')", dialect) == expected

    def test_split_part_past_the_end_is_empty_not_the_last_field_on_mysql(self) -> None:
        sql = _render("split_part('a,b,c', ',', 9)", "mysql")
        assert sql.startswith("CASE WHEN 9 >")
        assert "THEN '' ELSE SUBSTRING_INDEX(" in sql

    def test_date_diff_counts_boundaries_where_the_engine_counts_whole_units(self) -> None:
        """MySQL's TIMESTAMPDIFF answers 1 month for 2026-01-31 to 2026-03-01
        where the catalog documents 2, so both ends are truncated first.
        """
        sql = _render("date_diff('month', {[S].[A]}, {[S].[B]})", "mysql")
        assert sql.startswith("TIMESTAMPDIFF(MONTH, CAST(DATE_FORMAT(")

    def test_week_is_iso_where_the_engine_numbers_from_sunday(self) -> None:
        """MySQL and BigQuery answer 32 for 2026-08-15; ISO week 33 is the rule."""
        assert _render("extract('week', {[S].[D]})", "mysql").startswith("WEEK(")
        assert _render("extract('week', {[S].[D]})", "mysql").endswith(", 3)")
        assert "ISOWEEK" in _render("extract('week', {[S].[D]})", "bigquery")

    def test_extract_is_an_integer_where_the_engine_returns_numeric(self) -> None:
        assert _render("extract('year', {[S].[D]})", "postgres").startswith("CAST(EXTRACT(")
        assert _render("extract('year', {[S].[D]})", "postgres").endswith("AS INTEGER)")

    def test_split_part_past_the_end_is_empty_not_null_on_bigquery(self) -> None:
        assert _render("split_part('a,b,c', ',', 9)", "bigquery") == (
            "IFNULL(SPLIT('a,b,c', ',')[SAFE_OFFSET(8)], '')"
        )

    def test_round_ties_go_away_from_zero_where_the_engine_rounds_to_even(self) -> None:
        """ClickHouse's ROUND is half-to-even, so 2.5 would come back as 2."""
        assert _render("round(2.5)", "clickhouse") == "(sign(2.5) * floor(abs(2.5) + 0.5))"
        assert _render("round(2.345, 2)", "clickhouse") == (
            "(sign(2.345) * floor(abs(2.345) * pow(10, 2) + 0.5) / pow(10, 2))"
        )
        # Every other engine already rounds away from zero.
        assert _render("round(2.5)", "duckdb") == "ROUND(2.5)"

    def test_trunc_goes_toward_zero_where_the_engine_has_no_truncation(self) -> None:
        """Databricks has no numeric trunc, and a plain FLOOR would give -2."""
        assert _render("trunc(-1.9)", "databricks") == "(SIGN(-1.9) * FLOOR(ABS(-1.9)))"

    def test_extremum_propagates_null_where_the_engine_skips_it(self) -> None:
        """MySQL, Snowflake and BigQuery already answer NULL; four do not."""
        for dialect in ("duckdb", "postgres", "clickhouse", "databricks"):
            sql = _render("greatest(1, NULL, 3)", dialect)
            assert sql.startswith("CASE WHEN 1 IS NULL OR NULL IS NULL OR 3 IS NULL THEN NULL")
        assert _render("greatest(1, NULL, 3)", "mysql") == "GREATEST(1, NULL, 3)"

    def test_the_single_argument_log_is_not_in_the_catalog(self) -> None:
        """It is base 10 on DuckDB and natural on ClickHouse: a silent factor
        of 2.3, so only the explicit two-argument form is admitted and the
        one-argument call falls through to pass-through.
        """
        assert lookup_function("log") is not None
        assert not FUNCTION_CATALOG["log"].accepts(1)
        assert _render("log(100)", "clickhouse") == "log(100)"


class TestArgumentRewrites:
    """D4: a catalog entry is not a rename table."""

    def test_position_is_needle_first_and_bigquery_reverses_it(self) -> None:
        assert _render("position('cd', 'abcd')", "duckdb") == "POSITION('cd' IN 'abcd')"
        assert _render("position('cd', 'abcd')", "bigquery") == "STRPOS('abcd', 'cd')"

    def test_clickhouse_split_part_is_delimiter_first_and_indexed(self) -> None:
        assert _render("split_part('a,b,c', ',', 2)", "clickhouse") == (
            "splitByString(',', 'a,b,c')[2]"
        )

    def test_prefix_and_suffix_tests_fall_back_to_comparison_on_mysql(self) -> None:
        assert _render("starts_with('abcd', 'ab')", "mysql") == (
            "(LEFT('abcd', CHAR_LENGTH('ab')) = 'ab')"
        )
        assert _render("ends_with('abcd', 'cd')", "mysql") == (
            "(RIGHT('abcd', CHAR_LENGTH('cd')) = 'cd')"
        )

    def test_postgres_has_starts_with_but_not_ends_with(self) -> None:
        assert _render("starts_with('abcd', 'ab')", "postgres") == "STARTS_WITH('abcd', 'ab')"
        assert _render("ends_with('abcd', 'cd')", "postgres") == (
            "(RIGHT('abcd', LENGTH('cd')) = 'cd')"
        )

    @pytest.mark.parametrize("dialect", ["snowflake", "databricks"])
    def test_underscoreless_spelling(self, dialect: str) -> None:
        assert _render("starts_with('abcd', 'ab')", dialect) == "STARTSWITH('abcd', 'ab')"
        assert _render("ends_with('abcd', 'cd')", dialect) == "ENDSWITH('abcd', 'cd')"

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [
            ("duckdb", "(-7 // 2)"),
            ("postgres", "DIV(-7, 2)"),
            ("mysql", "(-7 DIV 2)"),
            ("clickhouse", "intDiv(-7, 2)"),
            ("snowflake", "TRUNC(-7 / 2)"),
            ("bigquery", "DIV(-7, 2)"),
            ("databricks", "(-7 div 2)"),
        ],
    )
    def test_integer_division_is_spelled_differently_on_every_engine(
        self, dialect: str, expected: str
    ) -> None:
        """``div`` is an OBSL name no engine shares: a function on three, an
        operator on three, and a truncated quotient on Snowflake.
        """
        assert _render("div(-7, 2)", dialect) == expected

    def test_log_base_comes_first_and_bigquery_reverses_it(self) -> None:
        assert _render("log(10, 100)", "duckdb") == "LOG(10, 100)"
        assert _render("log(10, 100)", "bigquery") == "LOG(100, 10)"

    def test_clickhouse_log_changes_base_through_log10(self) -> None:
        """ClickHouse has no two-argument log, and its ``ln`` is a fast
        approximation: ``ln(100) / ln(10)`` returns 1.9999999996784485.
        """
        assert _render("log(10, 100)", "clickhouse") == "(log10(100) / log10(10))"

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [
            ("duckdb", "(\"S].[D\" + 5 * INTERVAL '1 day')"),
            ("postgres", "(\"S].[D\" + 5 * INTERVAL '1 day')"),
            ("mysql", "DATE_ADD(`S].[D`, INTERVAL 5 DAY)"),
            ("clickhouse", 'date_add(DAY, 5, "S].[D")'),
            ("snowflake", "DATEADD('day', 5, \"S].[D\")"),
            ("bigquery", "(`S].[D` + INTERVAL 5 DAY)"),
            ("databricks", "(`S].[D` + make_interval(0, 0, 0, 5, 0, 0, 0))"),
            ("dremio", 'TIMESTAMPADD(DAY, 5, "S].[D")'),
        ],
    )
    def test_date_add_is_a_different_shape_on_every_engine(
        self, dialect: str, expected: str
    ) -> None:
        """No engine accepts ``date_add(unit, n, x)``, so all eight render it.

        The interval is never a literal with *n* inside it: Postgres, DuckDB
        and Spark only accept a constant there, and *n* is an expression in a
        real model.
        """
        assert _render("date_add('day', 5, {[S].[D]})", dialect) == expected

    def test_postgres_builds_date_diff_out_of_arithmetic(self) -> None:
        """Postgres has no date_diff, datediff or TIMESTAMPDIFF in any form."""
        sql = _render("date_diff('day', {[S].[A]}, {[S].[B]})", "postgres")
        assert "EXTRACT(EPOCH FROM" in sql
        assert sql.startswith("CAST(TRUNC(")

    def test_postgres_builds_last_day_out_of_date_trunc(self) -> None:
        sql = _render("last_day({[S].[D]})", "postgres")
        assert "DATE_TRUNC('month'" in sql
        assert "INTERVAL '1 month' - INTERVAL '1 day'" in sql

    def test_current_date_drops_its_parens_on_postgres(self) -> None:
        assert _render("current_date()", "postgres") == "CURRENT_DATE"
        assert _render("current_date()", "duckdb") == "CURRENT_DATE()"

    @pytest.mark.parametrize("dialect", ["mysql", "dremio"])
    def test_truncate_takes_an_explicit_digit_count(self, dialect: str) -> None:
        """Both spell it TRUNCATE and require the second argument, which the
        catalog leaves optional.
        """
        assert _render("trunc(1.9)", dialect) == "TRUNCATE(1.9, 0)"


class TestRenderingInvariants:
    """Properties that must hold for every entry on every dialect."""

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_every_example_renders(self, dialect: str) -> None:
        for spec in CATALOG:
            for example in spec.examples:
                assert _render(example.call, dialect)

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_a_rewritten_call_still_composes_as_an_operand(self, dialect: str) -> None:
        """A renderer that emits an operator chain has to parenthesise it.

        ``concat(a, b) = 'x'`` must not become ``a || b = 'x'`` with the
        comparison binding tighter than the concatenation on some engine.
        """
        sql = _render("upper(concat('a', 'b'))", dialect)
        assert sql.startswith("UPPER(")
        assert sql.endswith(")")

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_every_rendering_is_atomic(self, dialect: str) -> None:
        """Every entry, on every dialect, must render as something the
        surrounding expression can treat as one operand.

        ``compile_expr`` hands a function call's rendering straight to its
        parent and never parenthesises it, so a rewrite that expands to
        ``a * b`` silently rebinds: ``10 / trunc(2.5)`` on Databricks became
        ``10 / SIGN(2.5) * FLOOR(ABS(2.5))``, which is 20 rather than 5. The
        property is checked for the whole catalog rather than the entries that
        happen to rewrite today, because the next group adds more of them.
        """
        offenders = [
            f"{example.call} -> {_render(example.call, dialect)}"
            for spec in CATALOG
            for example in spec.examples
            if not _is_atomic(_render(example.call, dialect))
        ]
        assert not offenders, (
            f"{dialect} renders these as bare infix expressions, which the "
            f"surrounding operators will bind into:\n  " + "\n  ".join(offenders)
        )

    @pytest.mark.parametrize(
        ("dialect", "call", "expected"),
        [
            ("databricks", "trunc(2.5)", "10 / (SIGN(2.5) * FLOOR(ABS(2.5)))"),
            ("dremio", "log(2, 8)", "10 / (LOG10(8) / LOG10(2))"),
            ("clickhouse", "round(2.5)", "(sign(2.5) * floor(abs(2.5) + 0.5))"),
        ],
    )
    def test_a_rewrite_used_as_a_divisor_keeps_its_parens(
        self, dialect: str, call: str, expected: str
    ) -> None:
        """The concrete shape of the bug: a rewritten call on the right of a
        division. ClickHouse wraps both operands in a decimal CAST of its own,
        so only the rewrite's own parens are asserted there.
        """
        assert expected in _render(f"10 / {call}", dialect)

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_reference_arguments_are_quoted_per_dialect(self, dialect: str) -> None:
        ast = parse_expression(tokenize_metric_formula("upper({[Total Sales]})"))
        dia = DialectRegistry.get(dialect)
        assert dia.quote_identifier("Total Sales") in dia.compile_expr(ast)


class TestEscapeHatch:
    """D6: a function outside the catalog is still emitted verbatim."""

    def test_unknown_function_passes_through(self) -> None:
        assert _render("regexp_extract('abc', '[a-z]')", "duckdb") == (
            "regexp_extract('abc', '[a-z]')"
        )

    def test_wrong_arity_falls_back_to_the_authors_call(self) -> None:
        """Codegen never crashes on an arity the validator would have caught."""
        assert _render("position('a')", "bigquery") == "position('a')"


class _NoSplitPartDialect(DuckDBDialect):
    """A dialect that declares a catalog function unsupported.

    No shipped dialect does, so the behaviour has no other way to be tested.
    """

    @property
    def name(self) -> str:
        return "duckdb_without_split_part"

    @property
    def capabilities(self) -> DialectCapabilities:
        return DialectCapabilities(unsupported_functions=["split_part"])


class TestUnsupportedFunction:
    """A catalog entry an engine cannot answer is a 422-shaped domain error."""

    def test_declared_unsupported_function_raises(self) -> None:
        ast = parse_expression(tokenize_metric_formula("split_part('a,b', ',', 1)"))
        with pytest.raises(UnsupportedFunctionError) as excinfo:
            _NoSplitPartDialect().compile_expr(ast)
        assert excinfo.value.function == "split_part"

    def test_other_functions_still_render(self) -> None:
        ast = parse_expression(tokenize_metric_formula("upper('a')"))
        assert _NoSplitPartDialect().compile_expr(ast) == "UPPER('a')"

    def test_no_dialect_declares_one_today(self) -> None:
        """The string group renders on all eight engines; nothing is dropped."""
        for dialect in DIALECTS:
            assert DialectRegistry.get(dialect).capabilities.unsupported_functions == []

    def test_the_api_answers_422_not_500(self) -> None:
        """Every surface that translates the sibling unsupported-* errors has
        to translate this one, or the first dialect to declare an unsupported
        function turns a modelling problem into a system error.

        Exercised through a temporarily registered dialect, since no shipped
        dialect declares one yet — which is exactly why the path would
        otherwise go untested until the numeric or date group needs it.
        """
        from fastapi import HTTPException

        from orionbelt.api.services.query_compilation import compile_query_or_raise
        from orionbelt.service.model_store import ModelStore

        yaml_text = _ARITY_MODEL_YAML.replace("{EXPRESSION}", "split_part({Zip}, '-', 1)")
        store = ModelStore()
        model_id = store.load_model(yaml_text).model_id
        query = QueryObject(select=QuerySelect(dimensions=["Bad Zip"], measures=["Order Count"]))

        registry = DialectRegistry._dialects
        registry[_NoSplitPartDialect().name] = _NoSplitPartDialect
        try:
            with pytest.raises(HTTPException) as excinfo:
                compile_query_or_raise(
                    store=store,
                    model_id=model_id,
                    query=query,
                    dialect=_NoSplitPartDialect().name,
                )
        finally:
            registry.pop(_NoSplitPartDialect().name, None)

        assert excinfo.value.status_code == 422
        assert excinfo.value.detail["function"] == "split_part"


def test_catalog_functions_survive_a_full_compile() -> None:
    """End to end: a computed column using catalog functions reaches the SQL."""
    yaml_text = _ARITY_MODEL_YAML.replace("{EXPRESSION}", "upper(substring({Zip}, 1, 5))")
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    assert isinstance(model, SemanticModel)

    from orionbelt.compiler.pipeline import CompilationPipeline

    query = QueryObject(select=QuerySelect(dimensions=["Bad Zip"], measures=["Order Count"]))
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    assert 'UPPER(SUBSTRING("Orders"."ZIP", 1, 5))' in sql

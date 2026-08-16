"""Portable scalar-function catalog — catalog integrity, validation, rendering.

The catalog (``models/functions.py``) pins what a function *means*; these tests
pin that the three surfaces agree with it: the scanner and validator that check
a call, the dialects that render one, and the examples that claim a value.

The full per-dialect emit matrix lives in
``tests/integration/drift/test_drift_functions.py`` (golden SQL) and the
executed values in ``tests/integration/drift/vendor_exec/test_function_exec.py``.
"""

from __future__ import annotations

import pytest

import orionbelt.dialect  # noqa: F401 -- triggers dialect registration
from orionbelt.compiler.expr_parser import parse_expression, tokenize_metric_formula
from orionbelt.dialect.base import DialectCapabilities, UnsupportedFunctionError
from orionbelt.dialect.duckdb import DuckDBDialect
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.errors import SemanticError
from orionbelt.models.expressions import FunctionCallRef, find_function_calls
from orionbelt.models.functions import (
    FUNCTION_CATALOG,
    FunctionSpec,
    catalog_names,
    lookup_function,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

CATALOG = list(FUNCTION_CATALOG.values())
DIALECTS = sorted(DialectRegistry.available())


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
        assert spec.min_args >= 1
        if spec.max_args is not None:
            assert spec.max_args >= spec.min_args

    @pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
    def test_every_entry_carries_an_example(self, spec: FunctionSpec) -> None:
        """An entry without an example is a claim no test can check."""
        assert spec.examples

    @pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.name)
    def test_examples_parse_and_match_the_declared_arity(self, spec: FunctionSpec) -> None:
        for example in spec.examples:
            calls = find_function_calls(example.call)
            outer = calls[-1]
            assert outer.name == spec.name
            assert spec.accepts(outer.arg_count)
            parse_expression(tokenize_metric_formula(example.call))

    def test_lookup_is_case_insensitive(self) -> None:
        assert lookup_function("SUBSTRING") is FUNCTION_CATALOG["substring"]
        assert lookup_function("Substring") is FUNCTION_CATALOG["substring"]
        assert lookup_function("no_such_function") is None

    def test_catalog_names_are_sorted(self) -> None:
        assert catalog_names() == sorted(FUNCTION_CATALOG)


class TestFunctionCallScanner:
    """``find_function_calls`` feeds the validator; it must not invent calls."""

    def test_reports_name_and_arity(self) -> None:
        assert find_function_calls("substring({Zip}, 1, 5)") == [
            FunctionCallRef(name="substring", arg_count=3)
        ]

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

    def test_split_part_past_the_end_is_empty_not_null_on_bigquery(self) -> None:
        assert _render("split_part('a,b,c', ',', 9)", "bigquery") == (
            "IFNULL(SPLIT('a,b,c', ',')[SAFE_OFFSET(8)], '')"
        )


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
    """A dialect that declares a catalog function unsupported."""

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


def test_catalog_functions_survive_a_full_compile() -> None:
    """End to end: a computed column using catalog functions reaches the SQL."""
    yaml_text = _ARITY_MODEL_YAML.replace("{EXPRESSION}", "upper(substring({Zip}, 1, 5))")
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    assert isinstance(model, SemanticModel)

    from orionbelt.compiler.pipeline import CompilationPipeline
    from orionbelt.models.query import QueryObject, QuerySelect

    query = QueryObject(select=QuerySelect(dimensions=["Bad Zip"], measures=["Order Count"]))
    sql = CompilationPipeline().compile(query, model, "duckdb").sql
    assert 'UPPER(SUBSTRING("Orders"."ZIP", 1, 5))' in sql

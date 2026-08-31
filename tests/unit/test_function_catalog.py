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
from orionbelt.ast.nodes import ColumnRef, FunctionCall, Literal
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
from orionbelt.models.semantic import SemanticModel, WeekStart
from orionbelt.models.types import parse_data_type
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

CATALOG = list(FUNCTION_CATALOG.values())
CATALOG_BY_NAME = dict(FUNCTION_CATALOG)
DIALECTS = sorted(DialectRegistry.available())


def _is_unsupported(function: str, dialect: str) -> bool:
    """Whether *dialect* declares *function* unsupported.

    The rendering invariants below hold for every entry an engine can answer;
    one it has declared it cannot is expected to raise, and skipping it here is
    what keeps that a deliberate declaration rather than a broken renderer.
    """
    declared = DialectRegistry.get(dialect).capabilities.unsupported_functions
    return function in {name.lower() for name in declared}


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

    def test_an_uncatalogued_function_keeps_its_own_arity(self) -> None:
        """A vendor function's arity is not ours to know, so it is not checked.

        It is still *reported* as non-portable (see ``TestExpressionMode``);
        what must not happen is OBSL inventing an arity rule for it.
        """
        assert _errors_for("regexp_extract({Zip}, '[0-9]{5}', 1, 2, 3)") == [
            "NON_PORTABLE_FUNCTION"
        ]

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


class TestJsonPaths:
    """``json_value`` takes a literal path, and the dialects take it apart."""

    def test_an_expression_path_is_rejected(self) -> None:
        """Without this the call compiles to verbatim SQL, which is worse than
        an error: it slips past ``expressionMode: portable`` and past a
        dialect's unsupported-function guard, so a model acquires an engine
        dependency exactly where it asked not to.
        """
        assert "INVALID_JSON_PATH" in _errors_for("json_value({Zip}, {Zip})")

    def test_a_wildcard_path_is_rejected(self) -> None:
        """Filters and wildcards are outside the accepted subset: the engines
        diverge on them and a catalog entry has to pin one meaning.
        """
        assert "INVALID_JSON_PATH" in _errors_for("json_value({Zip}, '$.*')")

    def test_a_path_without_a_root_is_rejected(self) -> None:
        assert "INVALID_JSON_PATH" in _errors_for("json_value({Zip}, 'a.b')")

    def test_the_bare_root_is_rejected(self) -> None:
        """``$`` is not a path to a scalar, and it does not render: Postgres has
        no zero-argument ``json_extract_path_text`` and rejects the call, and
        Snowflake would be handed an empty extraction path.
        """
        assert "INVALID_JSON_PATH" in _errors_for("json_value({Zip}, '$')")

    def test_member_and_subscript_paths_pass(self) -> None:
        assert _errors_for("json_value({Zip}, '$.a')") == []
        assert _errors_for("json_value({Zip}, '$.a.b')") == []
        assert _errors_for("json_value({Zip}, '$.arr[0]')") == []

    def test_the_object_array_rule_is_pinned_by_example(self) -> None:
        """The rule the engines disagree on is the one that needs examples:
        DuckDB, Postgres, Snowflake and MySQL return the serialized JSON for a
        non-scalar path unless guarded.
        """
        spec = CATALOG_BY_NAME["json_value"]
        non_scalar = [e for e in spec.examples if e.expect is None and "zz" not in e.call]
        assert len(non_scalar) >= 2, "object and array paths must both be pinned"


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


class TestWeekStart:
    """``settings.weekStart`` — the one catalog rule a model can change."""

    @staticmethod
    def _render_with(call: str, dialect: str, week_start: WeekStart) -> str:
        engine = DialectRegistry.get(dialect)
        engine.week_start = week_start
        return engine.compile_expr(parse_expression(tokenize_metric_formula(call)))

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_the_two_calendars_render_differently(self, dialect: str) -> None:
        call = "date_trunc('week', {[S].[D]})"
        assert self._render_with(call, dialect, WeekStart.MONDAY) != self._render_with(
            call, dialect, WeekStart.SUNDAY
        )

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [
            ("clickhouse", 'toStartOfWeek("S].[D", 0)'),
            ("bigquery", "DATE_TRUNC(`S].[D`, WEEK)"),
            ("mysql", "DATE(DATE_SUB(`S].[D`, INTERVAL DAYOFWEEK(`S].[D`) - 1 DAY))"),
            (
                "snowflake",
                "DATEADD('day', -(MOD(DAYOFWEEKISO(\"S].[D\"), 7)), DATE_TRUNC('day', \"S].[D\"))",
            ),
        ],
    )
    def test_sunday_uses_each_engines_own_day_numbering(self, dialect: str, expected: str) -> None:
        """The day-of-week numbering is the trap: MySQL's WEEKDAY starts at
        Monday and its DAYOFWEEK at Sunday, and Snowflake's DAYOFWEEK follows a
        session parameter, so the ISO variant is used there instead.
        """
        assert self._render_with("date_trunc('week', {[S].[D]})", dialect, WeekStart.SUNDAY) == (
            expected
        )

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_week_difference_never_delegates_to_the_engine(self, dialect: str) -> None:
        """The engines split on what a week difference means, so it is measured.

        From Sunday 2026-08-09 to Saturday 2026-08-15, ClickHouse, Snowflake and
        BigQuery count the Monday between them and answer 1; DuckDB and MySQL
        count whole seven-day spans and answer 0; Postgres has no such function.
        """
        sql = self._render_with(
            "date_diff('week', {[S].[A]}, {[S].[B]})", dialect, WeekStart.MONDAY
        )
        assert "7" in sql, "the week difference should be a day count divided by seven"

    def test_snowflake_never_consults_its_session_parameter(self) -> None:
        """Snowflake's DATE_TRUNC('week', ...) follows WEEK_START, so a session
        set to Sunday would override a model that says Monday. Both calendars
        are computed from DAYOFWEEKISO, which no session parameter moves.
        """
        for week_start in (WeekStart.MONDAY, WeekStart.SUNDAY):
            sql = self._render_with("date_trunc('week', {[S].[D]})", "snowflake", week_start)
            assert "DAYOFWEEKISO" in sql
            assert "DATE_TRUNC('week'" not in sql

    @pytest.mark.parametrize("dialect", DIALECTS)
    @pytest.mark.parametrize("week_start", [WeekStart.MONDAY, WeekStart.SUNDAY])
    def test_a_week_starts_at_midnight(self, dialect: str, week_start: WeekStart) -> None:
        """The start of a week is a day boundary, so a rewrite that subtracts
        days from a timestamp has to subtract them from its day: otherwise
        13:45 survives into the result. Snowflake and Dremio did that.
        """
        sql = self._render_with("date_trunc('week', {[S].[T]})", dialect, week_start)
        subtracts_days = any(
            token in sql for token in ("DATEADD", "TIMESTAMPADD", "DATE_SUB", "- ")
        )
        if subtracts_days:
            assert "DATE_TRUNC('day'" in sql or "DATE(" in sql, (
                f"{dialect} steps back from the value rather than from its day: {sql}"
            )

    def test_extract_week_is_iso_under_either_calendar(self) -> None:
        """A Sunday-start week *number* has no portable definition — MySQL
        alone offers eight numbering modes — so numbering stays ISO and says so.
        """
        for week_start in (WeekStart.MONDAY, WeekStart.SUNDAY):
            assert self._render_with("extract('week', {[S].[D]})", "mysql", week_start).endswith(
                ", 3)"
            )

    def test_the_default_is_iso(self) -> None:
        from orionbelt.models.semantic import ModelSettings

        assert ModelSettings().week_start is WeekStart.MONDAY
        assert DialectRegistry.get("duckdb").week_start is WeekStart.MONDAY

    @pytest.mark.parametrize(
        "setting",
        [
            "weekStart: Mondey",
            "defaultTimezone: Mars/Olympus",
            "defaultNumericDataType: banana",
            # Falsy values were dropped along with the whole settings block, so
            # a wrong value validated as though the model had said nothing.
            'weekStart: ""',
            "weekStart: false",
            "weekStart: 0",
            # An explicit null, and a key with nothing after it, which YAML
            # reads as the same thing: the field is a non-nullable enum, so
            # both are wrong values rather than an unset one.
            "weekStart: null",
            "weekStart:",
        ],
    )
    def test_a_rejected_setting_is_a_model_error_not_a_crash(self, setting: str) -> None:
        """A typo in settings used to escape as a raw pydantic ValidationError,
        which the API surfaced as a 500 where every other model mistake is a
        structured 422.
        """
        yaml_text = (
            "version: 1.0\nsettings:\n  "
            + setting
            + "\ndataObjects:\n  O:\n    code: o\n    columns:\n"
            "      A: {code: a, abstractType: string}\n"
        )
        raw, source_map = TrackedLoader().load_string(yaml_text)
        _model, result = ReferenceResolver().resolve(raw, source_map)
        assert not result.valid
        assert [e.code for e in result.errors] == ["INVALID_SETTING"]

    def test_the_pipeline_applies_the_model_setting(self) -> None:
        """A dialect is built per compile, so the calendar cannot leak."""
        from orionbelt.compiler.pipeline import CompilationPipeline

        yaml_text = _ARITY_MODEL_YAML.replace("{EXPRESSION}", "date_trunc('week', {Zip})").replace(
            "version: 1.0", "version: 1.0\nsettings:\n  weekStart: sunday"
        )
        raw, source_map = TrackedLoader().load_string(yaml_text)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        query = QueryObject(select=QuerySelect(dimensions=["Bad Zip"], measures=["Order Count"]))
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        assert "EXTRACT(DOW FROM" in sql
        # And the next compile of a Monday model is unaffected.
        assert DialectRegistry.get("duckdb").week_start is WeekStart.MONDAY


_TZ_MODEL_YAML = """\
version: 1.0
settings:
{settings}
dataObjects:
  Events:
    code: events
    columns:
      Event ID: {{code: id, abstractType: string}}
      Occurred At: {{code: occurred_at, abstractType: {ttype}}}
      Occurred On: {{code: occurred_on, abstractType: date}}
      Rounded At:
        abstractType: timestamp
        expression: "date_trunc('hour', {{Occurred At}})"

dimensions:
  Occurred At: {{dataObject: Events, column: Occurred At, resultType: timestamp}}
  Occurred On: {{dataObject: Events, column: Occurred On, resultType: date}}
  Rounded At: {{dataObject: Events, column: Rounded At, resultType: timestamp}}

measures:
  Latest Week:
    expression: "extract('week', {{[Events].[Occurred At]}})"
    aggregation: max
    resultType: int
  Event Count:
    columns: [{{dataObject: Events, column: Event ID}}]
    resultType: int
    aggregation: count
"""


def _tz_sql(settings: str, ttype: str, dimension: str, dialect: str = "duckdb") -> str:
    from orionbelt.compiler.pipeline import CompilationPipeline

    yaml_text = _TZ_MODEL_YAML.format(settings=settings, ttype=ttype)
    raw, source_map = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    query = QueryObject(select=QuerySelect(dimensions=[dimension], measures=["Event Count"]))
    return CompilationPipeline().compile(query, model, dialect).sql


class TestQueryTimezone:
    """``settings.queryTimezone`` — which zone a timestamp column is read in.

    Attached to the column rather than around the expressions that use it: a
    conversion applied twice moves the value twice, and an author's own
    conversion — catalog or opaque vendor SQL — would be that second
    application.
    """

    def test_nothing_changes_when_the_model_does_not_ask(self) -> None:
        sql = _tz_sql("  defaultDialect: duckdb", "timestamp_tz", "Occurred At")
        assert "AT TIME ZONE" not in sql

    def test_an_aware_column_is_read_in_the_query_zone(self) -> None:
        sql = _tz_sql("  queryTimezone: Europe/Zagreb", "timestamp_tz", "Occurred At")
        assert '("Events"."occurred_at" AT TIME ZONE \'Europe/Zagreb\')' in sql

    def test_a_naive_column_is_declared_before_it_is_converted(self) -> None:
        sql = _tz_sql(
            "  queryTimezone: Europe/Zagreb\n  defaultTimezone: UTC", "timestamp", "Occurred At"
        )
        assert "AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Zagreb'" in sql

    def test_a_naive_column_is_left_alone_when_undeclared(self) -> None:
        """The session's zone is a fact about the connection, not the data, so
        an undeclared column is not converted on a guess. The model validator
        warns rather than leaving it silent.
        """
        sql = _tz_sql("  queryTimezone: Europe/Zagreb", "timestamp", "Occurred At")
        assert "AT TIME ZONE" not in sql

    def test_a_computed_column_reaches_its_columns_in_the_query_zone(self) -> None:
        """A computed column is parsed from text, so its refs come from the
        tokenizer rather than from ``make_column_expr``: without applying the
        zone there too, one column meant two different instants depending on
        whether a dimension named it directly or an expression did.
        """
        sql = _tz_sql(
            "  queryTimezone: Europe/Zagreb\n  defaultTimezone: UTC", "timestamp", "Rounded At"
        )
        assert "DATE_TRUNC('hour', (\"Events\".\"occurred_at\" AT TIME ZONE 'UTC'" in sql

    def test_a_measure_expression_reaches_its_columns_in_the_query_zone(self) -> None:
        from orionbelt.compiler.pipeline import CompilationPipeline

        yaml_text = _TZ_MODEL_YAML.format(
            settings="  queryTimezone: Europe/Zagreb\n  defaultTimezone: UTC", ttype="timestamp"
        )
        raw, source_map = TrackedLoader().load_string(yaml_text)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        query = QueryObject(select=QuerySelect(measures=["Latest Week"]))
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        assert 'EXTRACT(WEEK FROM ("Events"."occurred_at" AT TIME ZONE \'UTC\'' in sql

    def test_a_column_is_never_converted_twice(self) -> None:
        """The pass is applied at more than one site, so it has to be
        idempotent: a doubled conversion moves the value twice.
        """
        from orionbelt.compiler.resolution import apply_query_timezone

        yaml_text = _TZ_MODEL_YAML.format(
            settings="  queryTimezone: Europe/Zagreb\n  defaultTimezone: UTC", ttype="timestamp"
        )
        raw, source_map = TrackedLoader().load_string(yaml_text)
        model, _ = ReferenceResolver().resolve(raw, source_map)
        once = apply_query_timezone(ColumnRef(name="occurred_at", table="Events"), model)
        assert once == apply_query_timezone(once, model)

    def test_a_join_key_is_not_converted(self) -> None:
        """A join asks whether two rows belong together, which no calendar
        changes: both sides would convert identically for the same answer, at
        the cost of wrapping a join key in a function, which is how an index or
        a partition stops being used.
        """
        from orionbelt.compiler.pipeline import CompilationPipeline

        yaml_text = """\
version: 1.0
settings:
  queryTimezone: Europe/Zagreb
  defaultTimezone: UTC
dataObjects:
  Events:
    code: events
    columns:
      Event ID: {code: id, abstractType: string}
      Occurred At: {code: occurred_at, abstractType: timestamp}
    joins:
      - joinType: many-to-one
        joinTo: Windows
        columnsFrom: [Occurred At]
        columnsTo: [Window At]
  Windows:
    code: windows
    columns:
      Window At: {code: window_at, abstractType: timestamp, primaryKey: true}
      Window Name: {code: window_name, abstractType: string}
dimensions:
  Window Name: {dataObject: Windows, column: Window Name, resultType: string}
  Occurred At: {dataObject: Events, column: Occurred At, resultType: timestamp}
measures:
  Event Count:
    columns: [{dataObject: Events, column: Event ID}]
    resultType: int
    aggregation: count
"""
        raw, source_map = TrackedLoader().load_string(yaml_text)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        query = QueryObject(
            select=QuerySelect(dimensions=["Window Name", "Occurred At"], measures=["Event Count"])
        )
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        on_clause = next(line for line in sql.splitlines() if line.startswith("LEFT JOIN"))
        assert "AT TIME ZONE" not in on_clause, on_clause
        # The same column, selected as a dimension, is still converted.
        assert '("Events"."occurred_at" AT TIME ZONE' in sql

    def test_a_computed_join_key_is_not_converted_either(self) -> None:
        """The opt-out has to reach the computed branch, not just the plain one.

        A computed key converted while the plain key it is compared against is
        not would be an asymmetric comparison: that changes which rows join,
        rather than merely costing an index.
        """
        from orionbelt.compiler.pipeline import CompilationPipeline

        yaml_text = """\
version: 1.0
settings:
  queryTimezone: Europe/Zagreb
  defaultTimezone: UTC
dataObjects:
  Events:
    code: events
    columns:
      Event ID: {code: id, abstractType: string}
      Raw At: {code: raw_at, abstractType: timestamp}
      Local At: {abstractType: timestamp, expression: "{Raw At}"}
    joins:
      - joinType: many-to-one
        joinTo: Windows
        columnsFrom: [Local At]
        columnsTo: [Window At]
  Windows:
    code: windows
    columns:
      Window At: {code: window_at, abstractType: timestamp, primaryKey: true}
      Window Name: {code: window_name, abstractType: string}
dimensions:
  Window Name: {dataObject: Windows, column: Window Name, resultType: string}
  Local At: {dataObject: Events, column: Local At, resultType: timestamp}
measures:
  Event Count:
    columns: [{dataObject: Events, column: Event ID}]
    resultType: int
    aggregation: count
"""
        raw, source_map = TrackedLoader().load_string(yaml_text)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        query = QueryObject(
            select=QuerySelect(dimensions=["Window Name", "Local At"], measures=["Event Count"])
        )
        sql = CompilationPipeline().compile(query, model, "duckdb").sql
        on_clause = next(line for line in sql.splitlines() if line.startswith("LEFT JOIN"))
        assert "AT TIME ZONE" not in on_clause, on_clause
        # Both sides bare, so the comparison stays symmetric.
        assert '"Events"."raw_at" = "Windows"."window_at"' in on_clause
        # And the same computed column, selected as a dimension, still converts.
        assert '("Events"."raw_at" AT TIME ZONE' in sql

    def test_a_date_column_is_never_converted(self) -> None:
        """A date has no instant to move between zones."""
        sql = _tz_sql("  queryTimezone: Europe/Zagreb", "timestamp_tz", "Occurred On")
        assert "AT TIME ZONE" not in sql

    def test_the_undeclared_column_is_warned_about(self) -> None:
        yaml_text = _TZ_MODEL_YAML.format(
            settings="  queryTimezone: Europe/Zagreb", ttype="timestamp"
        )
        raw, source_map = TrackedLoader().load_string(yaml_text)
        model, _ = ReferenceResolver().resolve(raw, source_map)
        warnings = [e for e in SemanticValidator().validate(model) if e.severity == "warning"]
        assert [w.code for w in warnings] == ["UNDECLARED_TIMESTAMP_ZONE"]
        assert "Events.Occurred At" in (warnings[0].context or {})["columns"]

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [
            ("duckdb", "AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Zagreb'"),
            ("postgres", "AT TIME ZONE 'UTC' AT TIME ZONE 'Europe/Zagreb'"),
            ("clickhouse", "toTimeZone(toDateTime("),
            ("mysql", "CONVERT_TZ("),
            ("snowflake", "CONVERT_TIMEZONE('UTC', 'Europe/Zagreb'"),
            # BigQuery maps the OBML timestamp type to its own TIMESTAMP, an
            # instant, so there is no source zone left to declare.
            ("bigquery", "DATETIME("),
            ("databricks", "from_utc_timestamp(to_utc_timestamp("),
            ("dremio", "CONVERT_TIMEZONE('UTC', 'Europe/Zagreb'"),
        ],
    )
    def test_every_dialect_has_its_own_conversion(self, dialect: str, expected: str) -> None:
        sql = _tz_sql(
            "  queryTimezone: Europe/Zagreb\n  defaultTimezone: UTC",
            "timestamp",
            "Occurred At",
            dialect,
        )
        assert expected in sql


class TestWeeklyGrainFollowsTheCalendar:
    """Every weekly path buckets the same way, not just the catalog function.

    A ``timeGrain: week`` dimension and a weekly period-over-period went
    through the dialect's own truncation, which hard-coded Monday on BigQuery
    (ISOWEEK), ClickHouse (``toMonday``) and MySQL (a ``%Y-%u`` label), and on
    Snowflake followed the WEEK_START session parameter. So the same model
    bucketed differently depending on whether a user selected the dimension or
    wrote the function.
    """

    @staticmethod
    def _grain_sql(dialect: str, week_start: WeekStart) -> str:
        from orionbelt.ast.nodes import ColumnRef as Ref
        from orionbelt.models.semantic import TimeGrain

        engine = DialectRegistry.get(dialect)
        engine.week_start = week_start
        return engine.compile_expr(
            engine.render_time_grain(Ref(name="occurred_at", table="Events"), TimeGrain.WEEK)
        )

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_the_dimension_grain_follows_the_setting(self, dialect: str) -> None:
        assert self._grain_sql(dialect, WeekStart.MONDAY) != self._grain_sql(
            dialect, WeekStart.SUNDAY
        )

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_the_dimension_grain_matches_the_catalog_function(self, dialect: str) -> None:
        """One implementation, so the two entry points cannot disagree."""
        for week_start in (WeekStart.MONDAY, WeekStart.SUNDAY):
            engine = DialectRegistry.get(dialect)
            engine.week_start = week_start
            catalog = engine.compile_expr(
                parse_expression(tokenize_metric_formula("date_trunc('week', {[Events].[X]})"))
            ).replace("Events].[X", "occurred_at")
            assert self._grain_sql(dialect, week_start).replace(
                '"Events"."occurred_at"', '"occurred_at"'
            ).replace("`Events`.`occurred_at`", "`occurred_at`") == catalog.replace(
                '"occurred_at"', '"occurred_at"'
            ).replace("`occurred_at`", "`occurred_at`")

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_the_period_over_period_spine_follows_it_too(self, dialect: str) -> None:
        engine = DialectRegistry.get(dialect)
        engine.week_start = WeekStart.SUNDAY
        sunday = engine.render_date_trunc_sql('"ts"', "week")
        engine.week_start = WeekStart.MONDAY
        assert sunday != engine.render_date_trunc_sql('"ts"', "week")

    def test_snowflake_weekly_grain_no_longer_reads_its_session(self) -> None:
        for week_start in (WeekStart.MONDAY, WeekStart.SUNDAY):
            assert "DAYOFWEEKISO" in self._grain_sql("snowflake", week_start)


class TestTimeZoneNodeIsWalkable:
    """A node with a child that the shared walker treats as a leaf loses it.

    ``collect_column_refs`` feeds CFL table ownership and reachability, so a
    timezone-wrapped ref that the walker cannot see is a column that vanishes
    from the plan rather than one that renders oddly.
    """

    def test_the_walker_sees_through_it(self) -> None:
        from orionbelt.ast.nodes import InTimeZone
        from orionbelt.compiler.expr_rewrite import collect_column_refs

        found: list[ColumnRef] = []
        collect_column_refs(
            InTimeZone(
                expr=ColumnRef(name="occurred_at", table="Events"),
                zone="Europe/Zagreb",
                from_zone="UTC",
            ),
            found,
        )
        assert found == [ColumnRef(name="occurred_at", table="Events")]

    def test_the_walker_rebuilds_it(self) -> None:
        from orionbelt.ast.nodes import InTimeZone
        from orionbelt.compiler.expr_rewrite import map_column_refs

        node = InTimeZone(expr=ColumnRef(name="occurred_at", table="Events"), zone="Europe/Zagreb")
        rewritten = map_column_refs(node, lambda ref: ColumnRef(name="other", table=ref.table))
        assert rewritten == InTimeZone(
            expr=ColumnRef(name="other", table="Events"), zone="Europe/Zagreb"
        )

    def test_the_visitor_rebuilds_it(self) -> None:
        from orionbelt.ast.nodes import InTimeZone
        from orionbelt.ast.visitor import ASTVisitor

        node = InTimeZone(expr=ColumnRef(name="occurred_at", table="Events"), zone="Europe/Zagreb")
        assert ASTVisitor().visit(node) == node


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
        """ClickHouse, PostgreSQL and MySQL all round ties to even for their
        float type, so 2.5 comes back as 2 on a double column. Each already
        rounds its *decimal* type away from zero, so only the float needs
        moving - without disturbing the decimal on the way.
        """
        assert _render("round(2.5)", "postgres") == "ROUND(CAST(2.5 AS numeric))"
        assert _render("round(2.5)", "mysql") == "TRUNCATE(2.5 + SIGN(2.5) * 0.5, 0)"
        assert _render("round(2.5)", "clickhouse") == (
            "truncate(2.5 + SIGN(2.5) * toDecimal256('0.5', 1), 0)"
        )
        # The five engines whose ROUND is already away from zero for both types.
        for dialect in ("duckdb", "bigquery", "snowflake", "databricks", "dremio"):
            assert _render("round(2.5)", dialect) == "ROUND(2.5)"
            assert _render("round(2.345, 2)", dialect) == "ROUND(2.345, 2)"

    def test_round_never_converts_the_value_on_mysql_or_clickhouse(self) -> None:
        """Neither engine has a decimal type that can hold anything, so a cast
        has to name a width and that width is a loss either way.

        MySQL's DECIMAL is 65 digits split between the sides, so ``CAST(1e50 AS
        DECIMAL(65, 18))`` saturates silently to 999...9. ClickHouse's
        conversion from Float64 scales by a power of ten in floating point, so
        ``round(toDecimal256(1e19, 18))`` is 9999999999999999539 where DuckDB
        says 1e19, and an infinity cannot be converted at all. Adding half of
        the last kept place and truncating needs no conversion.
        """
        for call in ("round(2.5)", "round(2.345, 2)", "round(2.345, -2)", "round(2.345, 19)"):
            assert "CAST(" not in _render(call, "mysql")
            assert "toDecimal256(2" not in _render(call, "clickhouse")

    def test_round_keeps_the_half_exact_so_a_decimal_stays_one(self) -> None:
        """The half is the only typed part, and typing it is the whole trick.

        A bare ``0.005`` already *is* a DECIMAL to MySQL. To ClickHouse it is a
        Float64, and ``Decimal + Float64`` is a Float64 - which is exactly how
        2.25.0 turned 12345678901234567.885 into 1.2345678901234568e16. Quoted
        and passed through ``toDecimal256`` it stays exact, and ClickHouse's own
        promotion then preserves whichever type the operand had.
        """
        assert _render("round(2.345, 2)", "mysql") == ("TRUNCATE(2.345 + SIGN(2.345) * 0.005, 2)")
        assert _render("round(2.345, 2)", "clickhouse") == (
            "truncate(2.345 + SIGN(2.345) * toDecimal256('0.005', 3), 2)"
        )
        # Nothing Float64 may creep back in on ClickHouse.
        assert "pow" not in _render("round(2.345, 2)", "clickhouse").lower()

    def test_round_half_matches_the_digit_count_it_is_rounding_to(self) -> None:
        """Half of the last kept place: 0.5 at 0 places, 0.005 at 2.

        A half that does not track the digit count is the bug the previous
        shape had in a different form - a fixed scale-18 cast dropped the 19th
        digit before ROUND ever ran.
        """
        assert "* 0.5," in _render("round(2.345)", "mysql")
        assert "* 0.005," in _render("round(2.345, 2)", "mysql")
        assert "* 0.00000000000000000005," in _render("round(2.345, 19)", "mysql")
        assert "toDecimal256('0.005', 3)" in _render("round(2.345, 2)", "clickhouse")

    def test_round_to_the_types_own_scale_or_more_is_the_identity(self) -> None:
        """Rounding to at least as many places as the decimal type carries
        cannot change a value it holds, so nothing is emitted.

        This is also the only correct answer at the ceiling: the half needs one
        place *more* than the count, which at the top is a scale the engine
        cannot express - and rounding a Decimal256(_, 76) to 76 places must
        return it unchanged rather than round it at 75.
        """
        for n in (30, 76, 5000):
            assert _render(f"round(2.345, {n})", "mysql") == "2.345"
        for n in (76, 5000):
            assert _render(f"round(2.345, {n})", "clickhouse") == "2.345"
        # One below the ceiling still rounds, and its half is expressible.
        assert "toDecimal256(" in _render("round(2.345, 75)", "clickhouse")
        assert _render("round(2.345, 29)", "mysql").endswith(", 29)")
        # PostgreSQL's numeric is unbounded, so it needs no ceiling at all.
        assert _render("round(2.345, 5000)", "postgres") == "ROUND(CAST(2.345 AS numeric), 5000)"

    def test_round_to_a_negative_digit_count_is_never_the_identity(self) -> None:
        """A large negative count is not the mirror of a large positive one.
        ``round(1e40, -5000)`` is 0, not 1e40, so the count cannot simply be
        clamped the way the positive end is.

        Truncating at a negative count also stops working once it passes the
        value's own magnitude: measured, ClickHouse leaves 1e40 alone at -41
        where DuckDB, the oracle, says 0. So these divide by the factor, round
        at zero places, and put the scale back, which is exact because the
        factor is an integer in both directions.
        """
        assert _render("round(2.345, -2)", "mysql") == (
            "(TRUNCATE(2.345 / 100 + SIGN(2.345) * 0.5, 0) * 100)"
        )
        assert _render("round(2.345, -2)", "clickhouse") == (
            "(truncate(2.345 / 100 + SIGN(2.345) * toDecimal256('0.5', 1), 0) * 100)"
        )
        # The factor has to out-scale the *value*, not the type. Bounding the
        # count instead left an ordinary DECIMAL(65) in the wrong place:
        # round(9e64, -5000) is 0, but a factor of 10**65 rounds 9e64 up to
        # 1e65. Past the largest finite double no factor is coarse enough, and
        # every representable number rounds to zero there.
        for dialect in ("mysql", "clickhouse"):
            assert f"/ {10**308} " in _render("round(2.345, -308)", dialect)
            assert _render("round(2.345, -309)", dialect) == "(SIGN(2.345) * 0)"
            assert _render("round(2.345, -5000)", dialect) == "(SIGN(2.345) * 0)"

    def test_round_falls_back_when_the_digit_count_is_computed(self) -> None:
        """A digit count that is not an integer literal cannot be spelled as a
        half, so both engines fall back to a float power of ten. That degrades
        a decimal operand, is documented, and is what 2.25.0 did as well.
        """
        for dialect in ("mysql", "clickhouse"):
            sql = _render("round(2.345, 1+1)", dialect)
            assert "POW(10, -(1 + 1))" in sql

    def test_round_does_not_read_a_boolean_as_a_digit_count(self) -> None:
        """``bool`` is a subclass of ``int``, so a naive isinstance check reads
        ``true`` as a digit count of 1 and calls it known.
        """
        call = FunctionCall(name="round", args=[Literal.number(2.345), Literal(value=True)])
        assert "POW(10, " in DialectRegistry.get("clickhouse").compile_expr(call)
        assert "POW(10, " in DialectRegistry.get("mysql").compile_expr(call)

    def test_round_supplies_the_two_argument_form_postgres_lacks(self) -> None:
        """PostgreSQL has no ``round(double precision, integer)`` at all, so a
        two-argument round over a float column raised UndefinedFunction rather
        than returning a wrong number. The numeric cast supplies the overload.
        """
        assert _render("round(2.345, 2)", "postgres") == "ROUND(CAST(2.345 AS numeric), 2)"

    def test_string_functions_read_a_clickhouse_fixedstring_by_value(self) -> None:
        """ClickHouse pads a ``FixedString`` to its width with NUL bytes that
        then count as content, so the padding leaks into the answer: measured,
        13 of the 15 string entries disagree with the same value held as a
        ``String``. ``toString`` strips it and is the identity on a ``String``.
        """
        assert _render("length({[S].[C]})", "clickhouse").startswith("lengthUTF8(toString(")
        assert _render("upper({[S].[C]})", "clickhouse").startswith("UPPER(toString(")
        assert "toString(" in _render("ends_with({[S].[C]}, 'ks')", "clickhouse")
        # No other engine has a fixed-width string type, so none is touched.
        for dialect in ("duckdb", "postgres", "mysql", "bigquery", "snowflake"):
            assert "toString(" not in _render("length({[S].[C]})", dialect)

    def test_only_the_text_arguments_are_coerced(self) -> None:
        """Wrapping a numeric argument would turn it into a string and change
        the call: ``substring(x, 2, 3)`` takes two integers, ``lpad`` takes a
        width. Only the positions the catalog marks as text are coerced.
        """
        sql = _render("substring({[S].[C]}, 2, 3)", "clickhouse")
        assert sql.count("toString(") == 1
        assert sql.endswith(", 2, 3)")
        sql = _render("lpad({[S].[C]}, 8, '*')", "clickhouse")
        assert sql.count("toString(") == 1  # the subject, not the width
        assert ", 8, '*')" in sql

    def test_a_literal_text_argument_is_left_alone(self) -> None:
        """A literal is already the engine's ordinary string type, so coercing
        it would only make the SQL harder to read.
        """
        sql = _render("replace({[S].[C]}, 'oo', 'XX')", "clickhouse")
        assert sql.count("toString(") == 1
        assert "'oo', 'XX'" in sql

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
            # The divisor is wrapped so a zero yields NULL (#321) - the
            # spelling of the division itself is what this test is about.
            ("duckdb", "(-7 // NULLIF(2, 0))"),
            ("postgres", "DIV(-7, NULLIF(2, 0))"),
            ("mysql", "(-7 DIV NULLIF(2, 0))"),
            ("clickhouse", "intDiv(-7, NULLIF(2, 0))"),
            ("snowflake", "TRUNC(-7 / NULLIF(2, 0))"),
            ("bigquery", "DIV(-7, NULLIF(2, 0))"),
            ("databricks", "(-7 div NULLIF(2, 0))"),
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
        # Asserted as a substring: the call now sits inside a domain guard
        # (#321), and the argument order is what this test is about.
        assert "LOG(10, 100)" in _render("log(10, 100)", "duckdb")
        assert "LOG(100, 10)" in _render("log(10, 100)", "bigquery")

    def test_clickhouse_log_changes_base_through_log10(self) -> None:
        """ClickHouse has no two-argument log, and its ``ln`` is a fast
        approximation: ``ln(100) / ln(10)`` returns 1.9999999996784485.
        """
        assert "(log10(100) / log10(10))" in _render("log(10, 100)", "clickhouse")

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

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_an_expression_count_survives_date_add(self, dialect: str) -> None:
        """The entry promises *n* may be an expression, and Databricks
        multiplies it by three for a quarter: a bare ``1 + 1`` there rendered
        as ``1 + 1 * 3``, four months rather than six.
        """
        sql = _render("date_add('quarter', 1 + 1, {[S].[D]})", dialect)
        assert "1 + 1 * 3" not in sql
        if "* 3" in sql or "* INTERVAL" in sql:
            assert "(1 + 1)" in sql, f"{dialect} leaves the count unbracketed: {sql}"

    def test_databricks_builds_a_quarter_from_months(self) -> None:
        assert _render("date_add('quarter', 1 + 1, {[S].[D]})", "databricks") == (
            "(`S].[D` + make_interval(0, (1 + 1) * 3, 0, 0, 0, 0, 0))"
        )

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
            if _is_unsupported(spec.name, dialect):
                continue
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
            if not _is_unsupported(spec.name, dialect)
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
            (
                "databricks",
                "trunc(2.5)",
                "10 / NULLIF((SIGN(2.5) * FLOOR(ABS(2.5))), 0)",
            ),
            (
                "dremio",
                "log(2, 8)",
                "10 / NULLIF((CASE WHEN 2 <= 0 OR 2 = 1 OR 8 <= 0 "
                "THEN NULL ELSE (LOG10(8) / LOG10(2)) END), 0)",
            ),
        ],
    )
    def test_a_rewrite_used_as_a_divisor_keeps_its_parens(
        self, dialect: str, call: str, expected: str
    ) -> None:
        """The concrete shape of the bug: a rewritten call on the right of a
        division.

        The divisor now sits inside a ``NULLIF`` guard (#319), which does not
        change what this test is for: the rewrite must still carry its own
        parens, or the surrounding operators bind into it.
        """
        assert expected in _render(f"10 / {call}", dialect)

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_reference_arguments_are_quoted_per_dialect(self, dialect: str) -> None:
        ast = parse_expression(tokenize_metric_formula("upper({[Total Sales]})"))
        dia = DialectRegistry.get(dialect)
        assert dia.quote_identifier("Total Sales") in dia.compile_expr(ast)


class TestExpressionMode:
    """The escape hatch, and the switch that closes it.

    A model may depend on one engine's SQL deliberately; what it should not do
    is acquire that dependency without noticing. So an uncatalogued call is
    reported either way, and the mode decides whether that stops the load.
    """

    _MODEL = """\
version: 1.0
{settings}dataObjects:
  Orders:
    code: o
    columns:
      Zip: {{code: zip, abstractType: string}}
      Zip 5:
        abstractType: string
        expression: "{expression}"
"""

    def _validate(self, expression: str, settings: str = "") -> list[SemanticError]:
        yaml_text = self._MODEL.format(settings=settings, expression=expression)
        raw, source_map = TrackedLoader().load_string(yaml_text)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        return SemanticValidator().validate(model)

    def test_a_catalog_call_says_nothing(self) -> None:
        assert self._validate("substring({Zip}, 1, 5)") == []

    def test_an_uncatalogued_call_warns_by_default(self) -> None:
        [reported] = self._validate("regexp_extract({Zip}, '[0-9]')")
        assert reported.code == "NON_PORTABLE_FUNCTION"
        assert reported.severity == "warning"
        assert reported.context == {"function": "regexp_extract"}
        assert "regexp_extract" in reported.message

    def test_portable_mode_rejects_it(self) -> None:
        [reported] = self._validate(
            "regexp_extract({Zip}, '[0-9]')",
            settings="settings:\n  expressionMode: portable\n",
        )
        assert reported.code == "NON_PORTABLE_FUNCTION"
        assert reported.severity == "error"

    def test_portable_mode_still_allows_the_catalog(self) -> None:
        assert (
            self._validate(
                "upper(substring({Zip}, 1, 5))",
                settings="settings:\n  expressionMode: portable\n",
            )
            == []
        )

    def test_one_report_per_function_per_expression(self) -> None:
        """Repeating a call is not a second problem."""
        reported = self._validate("concat(regexp_extract({Zip}, 'a'), regexp_extract({Zip}, 'b'))")
        assert len(reported) == 1

    def test_two_different_functions_are_two_reports(self) -> None:
        reported = self._validate("concat(regexp_extract({Zip}, 'a'), md5({Zip}))")
        assert sorted(r.context["function"] for r in reported if r.context) == [
            "md5",
            "regexp_extract",
        ]

    @pytest.mark.parametrize("mode", ["permissive", "portable"])
    def test_the_mode_does_not_change_the_sql(self, mode: str) -> None:
        """Portable mode refuses to load a model; it does not rewrite one."""
        assert _render("regexp_extract('abc', '[a-z]')", "duckdb") == (
            "regexp_extract('abc', '[a-z]')"
        )

    def test_a_rejected_mode_value_is_a_model_error(self) -> None:
        yaml_text = self._MODEL.format(
            settings="settings:\n  expressionMode: strict\n", expression="upper({Zip})"
        )
        raw, source_map = TrackedLoader().load_string(yaml_text)
        _model, result = ReferenceResolver().resolve(raw, source_map)
        assert [e.code for e in result.errors] == ["INVALID_SETTING"]


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

    def test_every_declared_unsupported_name_is_a_catalog_entry(self) -> None:
        """A declaration is only meaningful if it names a real entry.

        This replaces an assertion that no dialect declared one at all. The
        json group briefly made Dremio and Databricks declare ``json_value``
        before both turned out to have a cast that declines rather than fails,
        so the list is empty again - but the mechanism is real and the next
        group may use it. Checking the names against the catalog means a typo
        cannot silently un-drop a function.
        """
        declared = {
            dialect: DialectRegistry.get(dialect).capabilities.unsupported_functions
            for dialect in DIALECTS
        }
        for dialect, names in declared.items():
            unknown = [n for n in names if n not in CATALOG_BY_NAME]
            assert not unknown, f"{dialect} declares unknown function(s) {unknown}"
        # No dialect drops anything today, and that is the assertion. An
        # earlier version of this checked only isinstance(names, list), which
        # would have passed while a shipped dialect quietly dropped a working
        # entry.
        assert all(not names for names in declared.values()), (
            f"a dialect declares a catalog entry unsupported: "
            f"{ {d: n for d, n in declared.items() if n} }"
        )

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


class TestCastTargets:
    """``cast(x, 'type')`` takes the targets the catalog can pin, and no others.

    The engines do not merely spell a cast differently, they disagree about the
    value it produces, so the entry carries the two targets that agree and
    refuses the rest by name (#355). Every absence in ``CAST_TARGETS`` is a
    measurement, recorded in the entry's semantics.
    """

    ACCEPTED = ("double", "decimal(18, 2)", "decimal(38, 9)", "DECIMAL(18, 2)")
    REFUSED = ("integer", "bigint", "string", "date", "timestamp", "boolean", "time")

    @pytest.mark.parametrize("target", ACCEPTED)
    def test_an_accepted_target_validates(self, target: str) -> None:
        assert _errors_for(f"cast({{Zip}}, '{target}')") == []

    @pytest.mark.parametrize("target", REFUSED)
    def test_a_target_the_catalog_cannot_pin_is_refused(self, target: str) -> None:
        assert _errors_for(f"cast({{Zip}}, '{target}')") == ["UNSUPPORTED_CAST_TARGET"]

    @pytest.mark.parametrize("argument", ("{Zip}", "'not a type'", "'decimal(0, 2)'"))
    def test_a_target_that_is_not_a_type_literal_is_refused(self, argument: str) -> None:
        """A non-literal has nothing to render from: the type is needed to build the SQL."""
        assert _errors_for(f"cast({{Zip}}, {argument})") == ["UNSUPPORTED_CAST_TARGET"]

    @pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
    def test_every_dialect_renders_a_decimal_cast(self, dialect: str) -> None:
        """Through ``cast_to_obml_type``, so each engine's own overrides apply."""
        sql = _render("cast(x, 'decimal(18, 2)')", dialect)
        assert "18, 2" in sql.replace("38, 2", "18, 2") or "NUMERIC" in sql, sql

    def test_clickhouse_rounds_over_an_exact_decimal(self) -> None:
        """The one engine that does not round to the pinned rule on its own.

        Its ``round`` takes a *float's* ties to even - 2.5 at scale 0 came back
        2 where the other seven said 3 - and rounds a *Decimal's* away from
        zero, so converting first is the whole fix. Same move the ``round``
        entry makes on PostgreSQL: hand the engine the type it already rounds
        correctly.
        """
        sql = _render("cast(x, 'decimal(18, 2)')", "clickhouse")
        assert sql == (
            "CAST(round(toDecimal256OrNull(toString('x'), 3), 2) AS Nullable(Decimal(18, 2)))"
        )

    @pytest.mark.parametrize(
        ("target", "expected_scale"),
        [
            ("decimal(18, 2)", 3),
            ("decimal(38, 9)", 10),
            # The extra place comes out of the integer side, so it can only be
            # asked for while the target leaves a digit spare. These three have
            # none: 56, 1 and 0 integer digits against Decimal256's 76.
            ("decimal(76, 2)", 2),
            ("decimal(76, 20)", 20),
            ("decimal(76, 75)", 75),
            ("decimal(76, 76)", 76),
            # Above the ceiling the final type is clamped to Decimal(76, s), so
            # the intermediate has to be computed from the clamped width too.
            # From the raw request, decimal(77, 2) asked for scale 1 and lost a
            # cent, and decimal(100, 2) asked for scale -22, which is rejected.
            ("decimal(77, 2)", 2),
            ("decimal(100, 2)", 2),
        ],
    )
    def test_clickhouse_keeps_the_intermediate_within_decimal256(
        self, target: str, expected_scale: int
    ) -> None:
        """One place more than the target, and never wider than Decimal256.

        Decimal256 is 76 digits wherever the point sits, so the extra place is
        taken from the integer side rather than added to the type. Clamping on
        the scale alone was not enough, and the gap was reachable: an
        intermediate at scale 21 for ``decimal(76, 20)`` leaves 55 integer
        digits where the target holds 56, so a 56-digit value the target
        accepts raised ARGUMENT_OUT_OF_BOUND before reaching it. Measured on a
        live server, as was ``decimal(76, 75)``, which failed the same way on
        ``1.5`` while this test asserted the rendering that failed.

        Nothing is lost by clamping: a value needing every integer digit the
        target has cannot also carry a fractional place beyond it to round.
        """
        sql = _render(f"cast(x, '{target}')", "clickhouse")
        assert f"toDecimal256OrNull(toString('x'), {expected_scale})" in sql, sql

    def test_clickhouse_takes_a_text_argument(self) -> None:
        """``round('4.6', 2)`` raises ILLEGAL_TYPE_OF_ARGUMENT on this engine.

        Which made the motivating case - a number read out of JSON, where
        ``json_value`` is specified to return a string - compile to something
        the engine would not run. ``toString`` is the identity on a String and
        exact on a number, so one shape serves both.
        """
        sql = _render("cast(json_value(x, '$.rate'), 'decimal(18, 2)')", "clickhouse")
        assert "toDecimal256OrNull(toString(" in sql
        assert "SIGN(" not in sql

    def test_a_measure_data_type_cast_is_left_alone_on_clickhouse(self) -> None:
        """The rewrite is scoped to this entry, and the reason is measured.

        The same pre-round sits inside ``cast_to_obml_type``, where a declared
        ``dataType`` and the CFL union alignment reach it. The add-half shape
        names its operand twice - a windowed aggregate would be written out
        twice - and at ``decimal(76, 20)``, the width union alignment picks, the
        half overflows Decimal256 and wraps silently.
        """
        dialect = DialectRegistry.get("clickhouse")
        cast = dialect.cast_to_obml_type(ColumnRef(name="amt"), parse_data_type("decimal(18, 2)"))
        assert dialect.compile_expr(cast) == (
            'CAST(round(toDecimal256(toString("amt"), 3), 2) AS Nullable(Decimal(18, 2)))'
        )


class TestToNumber:
    """Text that does not name a number is NULL, on all eight dialects (#375).

    The other half of ``cast``: a cast over text is answered differently by
    every engine, and MySQL's answer is a silent 0. Every dialect tests the text
    against a numeral pattern first; the five with a safe cast keep it inside
    the test, for the magnitudes a pattern cannot speak about.
    """

    SAFE_CAST = {
        "duckdb": "TRY_CAST",
        "databricks": "TRY_CAST",
        "snowflake": "TRY_CAST",
        "bigquery": "SAFE_CAST",
    }

    @pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
    def test_every_dialect_tests_the_text_first(self, dialect: str) -> None:
        """The pattern is the definition of "names a number", everywhere.

        Which is what makes the entry's claim true: ``TRY_CAST('NaN' AS
        DOUBLE)`` is nan on DuckDB and ClickHouse, where the pattern matches
        neither ``NaN`` nor ``Infinity``, so testing on only the engines
        without a safe cast would have split the answer five against three.
        """
        sql = _render("to_number(x)", dialect)
        assert sql.startswith("CASE WHEN "), sql
        assert "[0-9]" in sql

    @pytest.mark.parametrize("dialect", sorted(SAFE_CAST))
    def test_an_engine_with_a_safe_cast_keeps_it_inside_the_test(self, dialect: str) -> None:
        """A pattern says whether the text is a numeral, not whether it fits."""
        assert f"THEN {self.SAFE_CAST[dialect]}(" in _render("to_number(x)", dialect)

    def test_clickhouse_uses_its_own_or_null_conversion(self) -> None:
        """No ``TRY_CAST`` here; the ``OrNull`` family is per target type."""
        assert "THEN toFloat64OrNull(" in _render("to_number(x)", "clickhouse")

    @pytest.mark.parametrize("dialect", ["postgres", "mysql", "dremio"])
    def test_an_engine_without_one_converts_plainly(self, dialect: str) -> None:
        """Nothing to be safe with, so the test carries the whole contract.

        Dremio is here because it was measured, not because its documentation
        said so: ``TRY_CAST`` does not parse there at all.
        """
        sql = _render("to_number(x)", dialect)
        assert "TRY_CAST" not in sql and "SAFE_CAST" not in sql, sql
        assert "THEN CAST(" in sql

    def test_postgres_converts_to_numeric_so_the_test_is_enough(self) -> None:
        """A pattern says whether text names a number, not whether it fits.

        ``'1e999'::double precision`` raises out of range where ``::numeric`` is
        exact, this engine's ``numeric`` being unbounded, so the target type is
        what makes a test sufficient here.
        """
        sql = _render("to_number(x)", "postgres")
        assert "AS NUMERIC)" in sql
        assert "DOUBLE PRECISION" not in sql

    @pytest.mark.parametrize("dialect", sorted(DialectRegistry.available()))
    def test_every_dialect_reads_the_argument_as_text(self, dialect: str) -> None:
        """``to_number(4.6)`` is an accepted call, and DuckDB has no trim(DECIMAL).

        Trimming the argument as it arrives failed to compile there. The round
        trip through the engine's own text form is exact, measured.
        """
        sql = _render("to_number(x)", dialect).upper()
        assert "TRIM" in sql, sql
        assert "CAST" in sql or "TOSTRING" in sql, sql


class TestClickHouseDecimalCeiling:
    """What the precision ceiling costs, pinned so it cannot drift unnoticed.

    The exactness rewrite converts through an intermediate one place wider than
    the target, and Decimal256 is 76 digits wherever the point sits. At
    precision 76 that place and the target's full integer width cannot both
    exist, so the conversion truncates instead of rounding. Every way of having
    both was measured and rejected: asking for the place anyway means
    ``decimal(76, 75)`` cannot take 1.5, a coalesce fallback wraps to a negative
    number, and no ClickHouse text conversion rounds.
    """

    def test_below_the_ceiling_the_extra_place_is_available(self) -> None:
        assert "toString('x'), 3)" in _render("cast(x, 'decimal(75, 2)')", "clickhouse")

    def test_at_the_ceiling_it_is_not(self) -> None:
        """Recorded rather than fixed: declaring 75 restores the rounding."""
        assert "toString('x'), 2)" in _render("cast(x, 'decimal(76, 2)')", "clickhouse")

    def test_a_request_above_the_ceiling_is_clamped_before_it_is_used(self) -> None:
        """Not scale 1, and never a negative scale."""
        for target in ("decimal(77, 2)", "decimal(100, 2)"):
            sql = _render(f"cast(x, '{target}')", "clickhouse")
            assert "toString('x'), 2)" in sql, sql
            assert ", -" not in sql, sql

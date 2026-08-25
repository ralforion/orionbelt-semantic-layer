"""Tests for CASE / IN / BETWEEN / IS NULL / LIKE in computed-column
expression parser (v2.7.3+, issue #77).

Pre-v2.7.3 the parser silently dropped tokens it couldn't handle, so
``CASE WHEN x THEN y END`` compiled to the string literal ``'CASE'``
with no error and ``SUM('CASE')`` ended up in the SQL.
"""

from __future__ import annotations

import duckdb
import pytest

from orionbelt.ast.nodes import (
    Between,
    BinaryOp,
    CaseExpr,
    InList,
    IsNull,
    Literal,
    UnaryOp,
)
from orionbelt.compiler.expr_parser import parse_expression, tokenize_measure_expression
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_MODEL_YAML = """\
version: 1.0
dataObjects:
  Financial:
    code: financial
    columns:
      Default Status:
        code: dflt_stts
        abstractType: string
      Outstanding Nominal Amount:
        code: otstndng_nmnl_amnt
        abstractType: float
        numClass: additive
      Off Balance Sheet Amount:
        code: off_blnc_sht_amnt
        abstractType: float
        numClass: additive
      Credit Exposure Amount:
        expression: '{Outstanding Nominal Amount} + {Off Balance Sheet Amount}'
        abstractType: float
        numClass: additive
      Defaulted Credit Exposure Base Amount:
        expression: >-
          CASE WHEN {Default Status} NOT IN ('11', '14')
          THEN {Credit Exposure Amount} ELSE 0 END
        abstractType: float
        numClass: additive
dimensions:
  Default Status:
    dataObject: Financial
    column: Default Status
    resultType: string
measures:
  Defaulted Credit Exposure Amount:
    columns:
      - dataObject: Financial
        column: Defaulted Credit Exposure Base Amount
    aggregation: sum
    resultType: float
"""


def _load_model():
    loader = TrackedLoader()
    raw, sm = loader.load_string(_MODEL_YAML)
    model, vr = ReferenceResolver().resolve(raw, sm)
    assert vr.valid, vr.errors
    return model


class TestCaseExpression:
    def test_case_compiles_to_case_expr(self):
        model = _load_model()
        # Use qualified refs so the tokenizer doesn't need the
        # ``{ColumnName}`` rewrite step (that happens during recursive
        # tokenisation, not direct parser calls).
        tokens = tokenize_measure_expression(
            "CASE WHEN {[Financial].[Default Status]} NOT IN ('11', '14') "
            "THEN {[Financial].[Outstanding Nominal Amount]} "
            "ELSE 0 END",
            model,
        )
        ast = parse_expression(tokens)
        assert isinstance(ast, CaseExpr)
        assert len(ast.when_clauses) == 1
        assert isinstance(ast.when_clauses[0][0], InList)
        assert ast.when_clauses[0][0].negated is True
        assert isinstance(ast.else_clause, Literal)

    def test_issue_77_repro_compiles_to_real_sql(self):
        """The exact #77 repro must produce SUM(CASE …), not SUM('CASE')."""
        from orionbelt.models.query import QueryObject, QuerySelect

        model = _load_model()
        q = QueryObject(
            select=QuerySelect(measures=["Defaulted Credit Exposure Amount"]),
        )
        result = CompilationPipeline().compile(q, model, "postgres")
        assert "'CASE'" not in result.sql, f"Silent-drop regression: {result.sql}"
        assert "CASE" in result.sql
        assert "WHEN" in result.sql
        assert "NOT IN" in result.sql
        assert "ELSE" in result.sql
        assert "END" in result.sql

    def test_case_with_else(self):
        model = _load_model()
        tokens = tokenize_measure_expression(
            "CASE WHEN {[Financial].[Default Status]} = '11' THEN 1 ELSE 0 END", model
        )
        ast = parse_expression(tokens)
        assert isinstance(ast, CaseExpr)
        assert ast.else_clause is not None

    def test_case_without_else(self):
        model = _load_model()
        tokens = tokenize_measure_expression(
            "CASE WHEN {[Financial].[Default Status]} = '11' THEN 1 END", model
        )
        ast = parse_expression(tokens)
        assert isinstance(ast, CaseExpr)
        assert ast.else_clause is None

    def test_case_multiple_whens(self):
        model = _load_model()
        tokens = tokenize_measure_expression(
            "CASE "
            "WHEN {[Financial].[Default Status]} = '11' THEN 1 "
            "WHEN {[Financial].[Default Status]} = '14' THEN 2 "
            "ELSE 0 END",
            model,
        )
        ast = parse_expression(tokens)
        assert isinstance(ast, CaseExpr)
        assert len(ast.when_clauses) == 2


class TestPostfixPredicates:
    def test_in_list(self):
        model = _load_model()
        tokens = tokenize_measure_expression(
            "{[Financial].[Default Status]} IN ('11', '14')", model
        )
        ast = parse_expression(tokens)
        assert isinstance(ast, InList)
        assert ast.negated is False
        assert len(ast.values) == 2

    def test_not_in(self):
        model = _load_model()
        tokens = tokenize_measure_expression(
            "{[Financial].[Default Status]} NOT IN ('11', '14')", model
        )
        ast = parse_expression(tokens)
        assert isinstance(ast, InList)
        assert ast.negated is True

    def test_between(self):
        model = _load_model()
        tokens = tokenize_measure_expression(
            "{[Financial].[Outstanding Nominal Amount]} BETWEEN 0 AND 100", model
        )
        ast = parse_expression(tokens)
        assert isinstance(ast, Between)
        assert ast.negated is False

    def test_not_between(self):
        model = _load_model()
        tokens = tokenize_measure_expression(
            "{[Financial].[Outstanding Nominal Amount]} NOT BETWEEN 0 AND 100", model
        )
        ast = parse_expression(tokens)
        assert isinstance(ast, Between)
        assert ast.negated is True

    def test_is_null(self):
        model = _load_model()
        tokens = tokenize_measure_expression("{[Financial].[Default Status]} IS NULL", model)
        ast = parse_expression(tokens)
        assert isinstance(ast, IsNull)
        assert ast.negated is False

    def test_is_not_null(self):
        model = _load_model()
        tokens = tokenize_measure_expression("{[Financial].[Default Status]} IS NOT NULL", model)
        ast = parse_expression(tokens)
        assert isinstance(ast, IsNull)
        assert ast.negated is True

    def test_like(self):
        model = _load_model()
        tokens = tokenize_measure_expression("{[Financial].[Default Status]} LIKE '1%'", model)
        ast = parse_expression(tokens)
        assert isinstance(ast, BinaryOp)
        assert ast.op == "LIKE"

    def test_not_like(self):
        model = _load_model()
        tokens = tokenize_measure_expression("{[Financial].[Default Status]} NOT LIKE '1%'", model)
        ast = parse_expression(tokens)
        assert isinstance(ast, BinaryOp)
        assert ast.op == "NOT LIKE"


class TestParserStrictness:
    """The pre-v2.7.3 parser silently dropped tokens it couldn't parse,
    producing garbage SQL. Now malformed expressions error loudly."""

    def test_dangling_tokens_raise(self):
        model = _load_model()
        # ``1 + 2 unexpected`` — the bare ident after ``2`` has no role.
        tokens = tokenize_measure_expression("1 + 2 garbage", model)
        # `garbage` becomes a bare-ident literal followed by no operator —
        # parser sees the literal as ``_parse_factor`` second factor of
        # nothing. Actually this parses OK as 1 + 2, then `garbage` is
        # leftover.
        with pytest.raises(ValueError, match="Unexpected token"):
            parse_expression(tokens)

    def test_unterminated_case_raises(self):
        model = _load_model()
        tokens = tokenize_measure_expression(
            "CASE WHEN {[Financial].[Default Status]} = '11' THEN 1", model
        )
        with pytest.raises(ValueError, match="Unterminated CASE"):
            parse_expression(tokens)

    def test_case_when_without_then_raises(self):
        model = _load_model()
        tokens = tokenize_measure_expression("CASE WHEN {[Financial].[Default Status]} END", model)
        with pytest.raises(ValueError, match="THEN"):
            parse_expression(tokens)

    def test_in_without_parens_raises(self):
        model = _load_model()
        tokens = tokenize_measure_expression("{[Financial].[Default Status]} IN '11'", model)
        with pytest.raises(ValueError, match="IN must be followed by"):
            parse_expression(tokens)

    def test_between_without_and_raises(self):
        model = _load_model()
        tokens = tokenize_measure_expression(
            "{[Financial].[Outstanding Nominal Amount]} BETWEEN 0 100", model
        )
        with pytest.raises(ValueError, match="BETWEEN"):
            parse_expression(tokens)

    def test_is_without_null_raises(self):
        model = _load_model()
        tokens = tokenize_measure_expression("{[Financial].[Default Status]} IS '11'", model)
        with pytest.raises(ValueError, match="IS predicate"):
            parse_expression(tokens)

    def test_missing_closing_paren_raises(self):
        model = _load_model()
        tokens = tokenize_measure_expression("(1 + 2", model)
        with pytest.raises(ValueError, match="closing"):
            parse_expression(tokens)


class TestDialectRendering:
    """The repro must compile to syntactically-plausible SQL on every dialect."""

    @pytest.mark.parametrize(
        "dialect",
        [
            "postgres",
            "mysql",
            "duckdb",
            "clickhouse",
            "snowflake",
            "bigquery",
            "databricks",
            "dremio",
        ],
    )
    def test_case_renders_on_all_dialects(self, dialect):
        from orionbelt.models.query import QueryObject, QuerySelect

        model = _load_model()
        q = QueryObject(
            select=QuerySelect(measures=["Defaulted Credit Exposure Amount"]),
        )
        result = CompilationPipeline().compile(q, model, dialect)
        sql = result.sql
        assert "'CASE'" not in sql, f"{dialect}: silent-drop regression"
        assert "CASE" in sql
        assert "WHEN" in sql
        assert "END" in sql


class TestUnarySign:
    """A leading ``-`` or ``+`` on an operand (v2.25+).

    The grammar had no unary sign at all, so ``round(-2.5)`` and
    ``{Amount} * -1`` both died on "Unexpected token '-' (op) in expression":
    ``-`` was only ever read as subtraction *between* two operands, which made
    a negative number impossible to write anywhere in an OBML expression.
    """

    @staticmethod
    def _parse(expression: str):
        model = _load_model()
        return parse_expression(tokenize_measure_expression(expression, model))

    def test_negative_literal_folds_into_the_number(self) -> None:
        """``- 2.5`` as SQL is the same value, but the constant the author
        wrote is what should appear.
        """
        assert self._parse("-2.5") == Literal(value=-2.5)

    def test_leading_plus_is_dropped(self) -> None:
        assert self._parse("+5") == Literal(value=5)

    def test_negation_of_an_expression_stays_an_operator(self) -> None:
        node = self._parse("-(1 + 2)")
        assert isinstance(node, UnaryOp)
        assert node.op == "-"

    def test_binds_tighter_than_multiplication(self) -> None:
        node = self._parse("2 * -1")
        assert isinstance(node, BinaryOp)
        assert node.op == "*"
        assert node.right == Literal(value=-1)

    def test_subtraction_of_a_negative_still_parses(self) -> None:
        node = self._parse("5 - -3")
        assert isinstance(node, BinaryOp)
        assert node.op == "-"
        assert node.right == Literal(value=-3)

    def test_negative_argument_renders_without_a_comment(self) -> None:
        """``5 - -3`` must keep the space: ``5 --3`` is a line comment in
        every dialect that supports ``--``.
        """
        from orionbelt.dialect.registry import DialectRegistry

        sql = DialectRegistry.get("duckdb").compile_expr(self._parse("5 - -3"))
        assert sql == "5 - -3"
        assert "--" not in sql


class TestUnbalancedCallParens:
    """A call needs its closing ``)``, the way a group always has (#364).

    Running out of tokens used to end the argument list as if the author had
    written one. That does not fail: it moves what the call wraps, so the
    expression stays valid, executes, and returns a different number.
    """

    #: Each pair is the same expression with and without the typo. Both parse
    #: today; only the second is what the author wrote.
    MEANING_CHANGED = [
        ("ROUND({[Financial].[Outstanding Nominal Amount]}, 2) * 100", "ROUND(x, 2) * 100"),
        ("ROUND({[Financial].[Outstanding Nominal Amount]}, 2 * 100", "ROUND(x, 2 * 100)"),
    ]

    def test_a_call_missing_its_closing_paren_raises(self) -> None:
        model = _load_model()
        tokens = tokenize_measure_expression("UPPER({[Financial].[Default Status]}", model)
        with pytest.raises(ValueError, match=r"Missing closing '\)' in call to 'UPPER'"):
            parse_expression(tokens)

    def test_the_outer_call_of_a_nested_pair_is_checked_too(self) -> None:
        """The inner ``)`` used to satisfy both."""
        model = _load_model()
        tokens = tokenize_measure_expression("UPPER(TRIM({[Financial].[Default Status]})", model)
        with pytest.raises(ValueError, match=r"Missing closing '\)' in call to 'UPPER'"):
            parse_expression(tokens)

    def test_an_in_list_is_checked_too(self) -> None:
        model = _load_model()
        tokens = tokenize_measure_expression("{[Financial].[Default Status]} IN ('11', '12'", model)
        with pytest.raises(ValueError, match=r"Missing closing '\)' in IN list"):
            parse_expression(tokens)

    def test_the_balanced_forms_are_untouched(self) -> None:
        model = _load_model()
        for expression in (
            "UPPER({[Financial].[Default Status]})",
            "UPPER(TRIM({[Financial].[Default Status]}))",
            "{[Financial].[Default Status]} IN ('11', '12')",
            "ROUND({[Financial].[Outstanding Nominal Amount]}, 2) * 100",
        ):
            parse_expression(tokenize_measure_expression(expression, model))

    def test_the_typo_that_moved_a_number_no_longer_parses(self) -> None:
        """``ROUND(x, 2 * 100`` compiled to ``ROUND(x, 200)``.

        Measured before the fix on ``-3.14159``: the intended
        ``ROUND(x, 2) * 100`` reads -314.00 and the typo reads -3.14159. Both
        ran, and nothing said which one the model meant.
        """
        model = _load_model()
        intended, typo = (expr for expr, _ in self.MEANING_CHANGED)
        parse_expression(tokenize_measure_expression(intended, model))
        with pytest.raises(ValueError, match=r"Missing closing '\)' in call to 'ROUND'"):
            parse_expression(tokenize_measure_expression(typo, model))

    def test_a_column_reference_reads_as_the_model_spells_it(self) -> None:
        """The message reaches the model's author now, so it says ``Object.column``.

        A ``colref`` token carries a NUL-separated payload, which used to reach
        the message verbatim: ``Unexpected token 'Financial\x00dflt_stts\x00string'``.
        """
        model = _load_model()
        tokens = tokenize_measure_expression("UPPER {[Financial].[Default Status]})", model)
        with pytest.raises(ValueError, match=r"'Financial\.dflt_stts'"):
            parse_expression(tokens)


class TestSimpleCaseForm:
    """``CASE <subject> WHEN <value> THEN ...`` desugars into the searched form (#360).

    Both are standard SQL (SQL:1999 6.11) and both run unmodified on all eight
    engines; only the searched one could be written in an OBML expression, so
    the natural way to map a code to a label - one subject, a list of values -
    was the one that did not parse.
    """

    MODEL_YAML = """\
version: 1.0
name: simple_case
dataObjects:
  Counterparty:
    code: counterparty
    columns:
      Country: {code: country, abstractType: string}
      Amount:  {code: amount, abstractType: float, numClass: additive}
      Currency Simple:
        expression: "CASE {Country} WHEN 'DE' THEN 'EUR' WHEN 'US' THEN 'USD' ELSE 'other' END"
        abstractType: string
      Currency Searched:
        expression: >-
          CASE WHEN {Country} = 'DE' THEN 'EUR'
               WHEN {Country} = 'US' THEN 'USD' ELSE 'other' END
        abstractType: string
      Null Match:
        expression: "CASE {Country} WHEN NULL THEN 'matched' ELSE 'no' END"
        abstractType: string

dimensions:
  Country:           {dataObject: Counterparty, column: Country, resultType: string}
  Currency Simple:   {dataObject: Counterparty, column: Currency Simple, resultType: string}
  Currency Searched: {dataObject: Counterparty, column: Currency Searched, resultType: string}
  Null Match:        {dataObject: Counterparty, column: Null Match, resultType: string}

measures:
  Total:
    columns: [{dataObject: Counterparty, column: Amount}]
    resultType: float
    aggregation: sum
"""

    def _model(self) -> SemanticModel:
        raw, source_map = TrackedLoader().load_string(self.MODEL_YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        return model

    def _rows(self) -> list[tuple]:
        query = QueryObject.model_validate(
            {
                "select": {
                    "dimensions": [
                        "Country",
                        "Currency Simple",
                        "Currency Searched",
                        "Null Match",
                    ],
                    "measures": ["Total"],
                }
            }
        )
        sql = CompilationPipeline().compile(query, self._model(), "duckdb").sql
        con = duckdb.connect()
        con.execute(
            "CREATE TABLE counterparty AS SELECT * FROM (VALUES"
            " ('DE', 10.0), ('US', 20.0), ('CH', 30.0), (NULL, 40.0)) t(country, amount)"
        )
        return sorted(con.execute(sql).fetchall(), key=lambda row: str(row[0]))

    def test_it_parses_at_all(self):
        model = _load_model()
        tokens = tokenize_measure_expression(
            "CASE {[Financial].[Default Status]} WHEN '11' THEN 1 ELSE 0 END", model
        )
        parsed = parse_expression(tokens)
        assert isinstance(parsed, CaseExpr)
        assert len(parsed.when_clauses) == 1

    def test_it_becomes_the_searched_form(self):
        """The subject is compared, so the node is the one the searched form builds."""
        model = _load_model()
        parsed = parse_expression(
            tokenize_measure_expression(
                "CASE {[Financial].[Default Status]} WHEN '11' THEN 1 END", model
            )
        )
        assert isinstance(parsed, CaseExpr)
        condition, _ = parsed.when_clauses[0]
        assert isinstance(condition, BinaryOp)
        assert condition.op == "="
        assert condition.right == Literal.string("11")

    def test_the_two_forms_agree_over_a_column(self):
        """Over a column and with a NULL row, because a literal exercises neither."""
        rows = self._rows()
        # Sorted by the country's text, so the NULL row sits under "None".
        assert [(row[0], row[1]) for row in rows] == [
            ("CH", "other"),
            ("DE", "EUR"),
            (None, "other"),
            ("US", "USD"),
        ]
        assert all(row[1] == row[2] for row in rows), rows

    def test_when_null_matches_nothing(self):
        """``x = NULL`` is unknown rather than false, in both forms.

        Including for the row whose subject *is* NULL, which is the case that
        looks like it should match and does not.
        """
        assert {row[3] for row in self._rows()} == {"no"}

    @pytest.mark.parametrize(
        "when", ["-1", "-{[Financial].[Outstanding Nominal Amount]}", "{[Financial].[Amount]} * 2"]
    )
    def test_a_negated_or_computed_value_is_still_a_value(self, when: str):
        """Unary *minus* is not a predicate, and arithmetic is not either.

        The check reads ``NOT`` on its own because it is the one predicate that
        arrives as a ``UnaryOp``; catching every ``UnaryOp`` would have taken
        ``WHEN -1`` with it.
        """
        model = _load_model()
        parsed = parse_expression(
            tokenize_measure_expression(
                f"CASE {{[Financial].[Default Status]}} WHEN {when} THEN 1 END", model
            )
        )
        assert isinstance(parsed, CaseExpr)

    def test_a_subject_with_no_when_is_refused(self):
        model = _load_model()
        with pytest.raises(ValueError, match="at least one WHEN"):
            parse_expression(
                tokenize_measure_expression("CASE {[Financial].[Default Status]} END", model)
            )

    @pytest.mark.parametrize(
        "when",
        [
            "{[Financial].[Outstanding Nominal Amount]} > 5",
            "{[Financial].[Default Status]} IS NULL",
            "{[Financial].[Default Status]} IN ('11', '12')",
            "{[Financial].[Default Status]} LIKE '1%'",
            "NOT ({[Financial].[Default Status]} = '11')",
            "NOT {[Financial].[Default Status]}",
        ],
    )
    def test_a_condition_in_the_value_position_is_refused(self, when: str):
        """``CASE x WHEN y > 5`` is legal SQL meaning ``x = (y > 5)``.

        Almost never what was meant, and the engines disagree about it: a type
        error on most, a silent coercion on MySQL. Saying so beats either.
        """
        model = _load_model()
        with pytest.raises(ValueError, match="takes a value, not a condition"):
            parse_expression(
                tokenize_measure_expression(
                    f"CASE {{[Financial].[Default Status]}} WHEN {when} THEN 1 END", model
                )
            )

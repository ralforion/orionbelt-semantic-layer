"""Tests for the narrowing-dataType warning (#356 follow-up)."""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.warnings import WarningCode
from orionbelt.parser import ReferenceResolver, TrackedLoader
from orionbelt.parser.validator import SemanticValidator

MODEL = """
version: "1.0"
name: narrowing
dataObjects:
  T:
    code: t
    columns:
      Day:  {code: day,  abstractType: date}
      Big:  {code: big,  abstractType: int}
      Amt:  {code: amt,  abstractType: float}
      Code: {code: code, abstractType: string}
      Calc: {abstractType: int, expression: "{[T].[Big]} + 1"}
dimensions:
  Day: {dataObject: T, column: Day}
measures:
  Narrow Max:   {columns: [{dataObject: T, column: Big}], aggregation: max, dataType: "integer"}
  Narrow Sum:   {columns: [{dataObject: T, column: Big}], aggregation: sum, dataType: "integer"}
  Wide Max:     {columns: [{dataObject: T, column: Big}], aggregation: max, dataType: "bigint"}
  Decimal Sum:
    columns: [{dataObject: T, column: Big}]
    aggregation: sum
    dataType: "decimal(18, 2)"
  Count Narrow: {columns: [{dataObject: T, column: Big}], aggregation: count, dataType: "integer"}
  Float Src:    {columns: [{dataObject: T, column: Amt}], aggregation: max, dataType: "integer"}
  No Declared:  {columns: [{dataObject: T, column: Big}], aggregation: max}
  Expr Max:     {expression: "{[T].[Big]}", aggregation: max, dataType: "integer"}
  Expr Twice:   {expression: "{[T].[Big]} + {[T].[Big]}", aggregation: sum, dataType: "integer"}
  Expr Wide:    {expression: "{[T].[Big]}", aggregation: max, dataType: "bigint"}
  Expr String:  {expression: "{[T].[Code]}", aggregation: max, dataType: "integer"}
  Calc Max:     {columns: [{dataObject: T, column: Calc}], aggregation: max, dataType: "integer"}
"""


def _narrowing_warnings() -> dict[str, str]:
    raw, source_map = TrackedLoader().load_string(MODEL)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert not result.errors, result.errors
    return {
        e.context["measure"]: e.message
        for e in SemanticValidator().validate(model)
        if e.code == WarningCode.NARROWING_DATA_TYPE
    }


def test_narrowing_integer_target_warns() -> None:
    """``integer`` is 32 bits; the column says it may hold 64."""
    warned = _narrowing_warnings()
    assert set(warned) == {"Narrow Max", "Narrow Sum", "Expr Max", "Expr Twice", "Calc Max"}
    assert "10 integer digits" in warned["Narrow Max"]
    assert "T.Big" in warned["Narrow Max"]


def test_the_expression_form_warns_too() -> None:
    """``expression:`` reads the same column ``columns:`` does.

    Both are supported ways to say which column a measure aggregates - it is
    ``Measure.source_objects`` that treats them as one - so a check that read
    only the structured form left the other declaration silent about the same
    narrowing.
    """
    warned = _narrowing_warnings()
    assert "T.Big" in warned["Expr Max"]


def test_a_column_named_twice_warns_once() -> None:
    """One narrowing is stated, however many times the expression spells it."""
    raw, source_map = TrackedLoader().load_string(MODEL)
    model, _ = ReferenceResolver().resolve(raw, source_map)
    found = [
        e
        for e in SemanticValidator().validate(model)
        if e.code == WarningCode.NARROWING_DATA_TYPE and e.context["measure"] == "Expr Twice"
    ]
    assert len(found) == 1, found


def test_wide_and_unrelated_targets_stay_quiet() -> None:
    """Everything that is not an unambiguous integer narrowing.

    ``Decimal Sum`` is the interesting one: ``decimal(18, 2)`` holds 16 integer
    digits and is narrower than ``int`` on the same arithmetic, but it is what
    ``defaultNumericDataType`` hands out and a quantity column does not reach
    10^16. Warning on it fired eight times on ``examples/tpcds.obml.yml``, all
    of them noise, which is why decimal targets are excluded.
    """
    warned = _narrowing_warnings()
    for quiet in (
        "Wide Max",
        "Decimal Sum",
        "Count Narrow",
        "Float Src",
        "No Declared",
        "Expr Wide",
        "Expr String",
    ):
        assert quiet not in warned, f"{quiet} should not warn: {warned.get(quiet)}"


def test_narrowing_is_a_warning_not_an_error() -> None:
    """A modeller who knows the range is allowed to narrow; the silence is what goes."""
    raw, source_map = TrackedLoader().load_string(MODEL)
    model, _ = ReferenceResolver().resolve(raw, source_map)
    found = [
        e for e in SemanticValidator().validate(model) if e.code == WarningCode.NARROWING_DATA_TYPE
    ]
    assert found
    assert {e.severity for e in found} == {"warning"}
    assert all(e.path.startswith("measures.") for e in found)


def test_bundled_models_are_quiet() -> None:
    """The rule has to be silent on models that are fine, or it trains people to ignore it."""
    import pathlib

    validator = SemanticValidator()
    noisy: list[str] = []
    for path in sorted(pathlib.Path("examples").rglob("*.obml.yml")):
        raw, source_map = TrackedLoader().load(path)
        model, result = ReferenceResolver().resolve(raw, source_map)
        if result.errors:
            continue
        for e in validator.validate(model):
            if e.code == WarningCode.NARROWING_DATA_TYPE:
                noisy.append(f"{path}: {e.message}")
    assert not noisy, "\n".join(noisy)


def _clickhouse_sql(measure: str) -> str:
    raw, source_map = TrackedLoader().load_string(MODEL)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    return (
        CompilationPipeline()
        .compile(
            QueryObject(select=QuerySelect(dimensions=["Day"], measures=[measure])),
            model,
            "clickhouse",
        )
        .sql
    )


@pytest.mark.parametrize("measure", ["Narrow Max", "Expr Max", "Calc Max"])
def test_clickhouse_guards_an_int_column_in_every_declaration_form(measure: str) -> None:
    """The guard follows the column, not the way the measure names it.

    ``columns:``, ``expression:`` and a computed column build the reference
    through two different paths - ``resolution.make_column_expr`` and the
    expression tokenizer - and only the first carried the declared type. The
    other two rendered ``CAST(MAX("t"."big") AS Nullable(Int32))``, which
    ClickHouse answers with -1294967296 for 3000000000 rather than refusing.
    """
    assert "accurateCast(trunc(" in _clickhouse_sql(measure)


def test_clickhouse_leaves_a_non_numeric_column_alone_in_the_expression_form() -> None:
    """``MAX(code)`` reads 42 for ``'42'`` today and ``trunc`` refuses it.

    The type travels so the dialect can tell a number from the rest, not so
    that every expression measure gets guarded.
    """
    sql = _clickhouse_sql("Expr String")
    assert "accurateCast" not in sql
    assert 'CAST(MAX("T"."code") AS Nullable(Int32))' in sql

"""Tests for the narrowing-dataType warning (#356 follow-up)."""

from __future__ import annotations

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
      Big:  {code: big,  abstractType: int}
      Amt:  {code: amt,  abstractType: float}
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
    assert set(warned) == {"Narrow Max", "Narrow Sum"}
    assert "10 integer digits" in warned["Narrow Max"]
    assert "T.Big" in warned["Narrow Max"]


def test_wide_and_unrelated_targets_stay_quiet() -> None:
    """Everything that is not an unambiguous integer narrowing.

    ``Decimal Sum`` is the interesting one: ``decimal(18, 2)`` holds 16 integer
    digits and is narrower than ``int`` on the same arithmetic, but it is what
    ``defaultNumericDataType`` hands out and a quantity column does not reach
    10^16. Warning on it fired eight times on ``examples/tpcds.obml.yml``, all
    of them noise, which is why decimal targets are excluded.
    """
    warned = _narrowing_warnings()
    for quiet in ("Wide Max", "Decimal Sum", "Count Narrow", "Float Src", "No Declared"):
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

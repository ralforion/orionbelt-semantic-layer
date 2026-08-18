"""Execute a measure whose value outgrows its type, per vendor.

The contract this pins is not "the same answer everywhere" - it cannot be,
because the engines legitimately hold different ranges. It is the weaker and
more important one: **no engine returns a plausible wrong number**. Every
outcome has to be the true value, NULL, or a raised error.

Seven of the eight satisfied it already, by raising or by holding the value.
MySQL did not: it saturates a cast that overflows and returns the largest value
the target type can express as an ordinary row, so a measure declared
``decimal(18, 2)`` over a true 100000000000000000 came back as
9999999999999999.99 (#336). Its measure casts are now widened to 38 digits, so
it returns the value.

Both target shapes are executed, because they saturate at different limits and
only one of them moved: a measure with a declared ``decimal(18, 2)`` is widened,
while a ``bigint`` measure still casts to ``SIGNED``, MySQL's only 64-bit
integer cast target. The second is the residue this fix leaves and the reason
it is executed here rather than argued about - a change either way shows up as
a diff in this file.

The rows come from ``VendorTarget.rows_of`` rather than the corpus seed, whose
values are far below any type's limit - the same reason ``test_exact_avg_exec``
spells its own (#330).
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser import ReferenceResolver, TrackedLoader

from .conftest import VendorTarget

pytestmark = pytest.mark.docker

MODEL_YAML = """
version: "1.0"
name: overflow_cast_exec
dataObjects:
  Charges:
    code: charges
    columns:
      Qty: {code: qty, abstractType: int}
measures:
  Qty Sum:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: sum
  Qty Sum Narrow:
    columns: [{dataObject: Charges, column: Qty}]
    resultType: int
    aggregation: sum
    dataType: "decimal(18, 2)"
"""

# (measure, rows, the true total). Two cases, because the two targets saturate
# at different limits: ``decimal(18, 2)`` runs out after 16 integer digits and
# ``SIGNED`` after 19.
OVERFLOWING = [
    ("Qty Sum Narrow", [50000000000000000, 50000000000000000], "100000000000000000"),
    ("Qty Sum", [9000000000000000000, 9000000000000000000], "18000000000000000000"),
]
# The same measures over values every type holds, so the guard is shown not to
# have cost the ordinary answer.
IN_RANGE = [1000, 2000]

# MySQL still answers with a number on the 64-bit case: it saturates its
# ``SIGNED`` cast, and its CAST vocabulary has no wider integer target. That
# residue is asserted by a test of its own rather than through the shared
# contract; see ``test_mysql_still_saturates_a_64_bit_integer_cast``.
#
# ClickHouse was here too, wrapping its Int64 accumulator to
# -446744073709551616 before any cast was reached. #338 casts the argument
# instead, so it now holds the value and the contract covers it.
#
# The decimal case, whose total stays well inside Int64, runs everywhere.
SATURATES_AT_64_BITS = {"mysql"}


def _projection(measure: str, dialect: str) -> str:
    """The measure expression OBSL emits, lifted out of its SELECT."""
    raw, sm = TrackedLoader().load_string(MODEL_YAML)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.valid, result.errors
    sql = (
        CompilationPipeline()
        .compile(QueryObject(select=QuerySelect(dimensions=[], measures=[measure])), model, dialect)
        .sql
    )
    match = re.search(r"SELECT (.+?)\s+FROM", sql, re.S)
    assert match, sql
    return match.group(1).strip()


def _outcome(target: VendorTarget, measure: str, values: list[int | None]) -> str | None:
    """``"raised"``, or the returned value as a string (``None`` for NULL)."""
    dia = DialectRegistry.get(target.dialect)
    source = target.rows_of(dia.quote_identifier("Charges"), dia.quote_identifier("qty"), values)
    try:
        rows = target.execute(f"SELECT {_projection(measure, target.dialect)} FROM {source}")
    except Exception:  # noqa: BLE001 - any refusal is a passing outcome here
        return "raised"
    got = next(iter(rows[0].values()))
    return None if got is None else str(got)


def _assert_no_wrong_number(target: VendorTarget) -> None:
    mismatches = []
    for measure, values, total in OVERFLOWING:
        if measure == "Qty Sum" and target.dialect in SATURATES_AT_64_BITS:
            continue
        got = _outcome(target, measure, values)
        if got not in ("raised", None) and Decimal(got) != Decimal(total):
            mismatches.append(
                f"{measure} over a true {total} returned {got}, "
                "which is neither the value, NULL, nor a refusal"
            )
    for measure, _, _ in OVERFLOWING:
        got = _outcome(target, measure, IN_RANGE)
        if got is None or got == "raised" or Decimal(got) != Decimal(3000):
            mismatches.append(f"{measure} over values that fit returned {got}, expected 3000")
    assert not mismatches, f"{target.name}:\n  " + "\n  ".join(mismatches)


def test_duckdb_no_wrong_number(vendor_duckdb: VendorTarget) -> None:
    _assert_no_wrong_number(vendor_duckdb)


def test_postgres_no_wrong_number(vendor_postgres: VendorTarget) -> None:
    _assert_no_wrong_number(vendor_postgres)


def test_mysql_no_wrong_number(vendor_mysql: VendorTarget) -> None:
    _assert_no_wrong_number(vendor_mysql)


def test_clickhouse_no_wrong_number(vendor_clickhouse: VendorTarget) -> None:
    _assert_no_wrong_number(vendor_clickhouse)


def test_snowflake_no_wrong_number(vendor_snowflake: VendorTarget) -> None:
    _assert_no_wrong_number(vendor_snowflake)


def test_bigquery_no_wrong_number(vendor_bigquery: VendorTarget) -> None:
    _assert_no_wrong_number(vendor_bigquery)


def test_databricks_no_wrong_number(vendor_databricks: VendorTarget) -> None:
    _assert_no_wrong_number(vendor_databricks)


def test_mysql_holds_the_value_rather_than_saturating(vendor_mysql: VendorTarget) -> None:
    """The engine this issue is about, pinned to the specific answer.

    ``_assert_no_wrong_number`` accepts a refusal, which is what most of the
    others do. MySQL cannot refuse - no ``sql_mode`` makes a SELECT-time cast
    raise, measured across ``STRICT_ALL_TABLES``, ``STRICT_TRANS_TABLES`` and
    ``TRADITIONAL`` - so holding the value is the whole of its contract on the
    decimal path, and a regression would show up as the saturated number rather
    than as an error.
    """
    measure, values, total = OVERFLOWING[0]
    got = _outcome(vendor_mysql, measure, values)
    assert got not in (None, "raised") and Decimal(got) == Decimal(total), f"{measure}: {got}"


def test_mysql_still_saturates_a_64_bit_integer_cast(vendor_mysql: VendorTarget) -> None:
    """The residue, recorded rather than left to be rediscovered.

    ``SIGNED`` is MySQL's only 64-bit integer cast target and its CAST
    vocabulary has no wider one, so a ``SUM`` over a bigint column past
    9223372036854775807 still comes back saturated. Widening it would mean
    casting every count to DECIMAL, changing the type family of the most common
    measure in the model to reach a value no real count has.

    Asserted so that the day it changes - deliberately or not - this file says
    so. It is the one place a wrong number is still possible, on the one engine
    that produces them.
    """
    got = _outcome(vendor_mysql, "Qty Sum", OVERFLOWING[1][1])
    assert got not in (None, "raised") and Decimal(got) == Decimal(2**63 - 1), got

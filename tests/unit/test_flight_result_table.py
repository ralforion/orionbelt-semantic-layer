"""The Flight executor takes the driver's Arrow schema, not one it guesses.

``ob_flight.server_execution`` used to build every result by reading rows out
of a PEP 249 cursor and inferring Arrow types from the values in the first
batch. That is the inference class behind the empty-column-typed-``null``
cache bug and the governed-decimal narrowing of #136 - both of which
``align_cached_table`` then repairs downstream.

These run a real DuckDB cursor through the real ``ob_duckdb`` driver, because
the point is what an actual driver reports, and compare the two paths on the
same query.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

pa = pytest.importorskip("pyarrow")
duckdb = pytest.importorskip("duckdb")
pytest.importorskip("ob_flight")

from ob_flight.converters import rows_to_batch, schema_from_description  # noqa: E402
from ob_flight.server_execution import _result_table  # noqa: E402


class _Server:
    """Only the attribute ``_result_table`` reads."""

    _batch_size = 1024


def _cursor(sql: str) -> Any:
    """A real ob_duckdb cursor over an in-memory database."""
    ob_duckdb = pytest.importorskip("ob_duckdb", reason="ob-duckdb not installed")
    connection = ob_duckdb.connect(database=":memory:")
    cursor = connection.cursor()
    cursor.execute(sql)
    return cursor


def _inferred(sql: str) -> pa.Table:
    """What the row path would have produced for the same query."""
    cursor = _cursor(sql)
    first = cursor.fetchmany(1024)
    schema = schema_from_description(cursor.description, sample_rows=first)
    batches = [rows_to_batch(first, schema)] if first else [rows_to_batch([], schema)]
    return pa.Table.from_batches(batches)


class TestDecimalWidth:
    QUERY = "SELECT CAST(1.50 AS DECIMAL(18,2)) AS amount"

    def test_the_declared_width_survives(self) -> None:
        table = _result_table(_Server(), _cursor(self.QUERY))
        assert table.schema.field("amount").type == pa.decimal128(18, 2)
        assert table.column("amount").to_pylist() == [Decimal("1.50")]

    def test_and_the_row_path_would_have_narrowed_it(self) -> None:
        """Pins the reason this change exists rather than asserting it.

        If this ever stops narrowing, the inference improved and the fallback
        matters less - but the fix above still stands, because a driver that
        was told the width should not have to be guessed at.
        """
        narrowed = _inferred(self.QUERY).schema.field("amount").type
        assert narrowed != pa.decimal128(18, 2), (
            f"row-path inference produced {narrowed}; the two paths now agree"
        )


class TestEmptyResult:
    QUERY = "SELECT CAST(1 AS BIGINT) AS n, 'x' AS s WHERE 1=0"

    def test_an_empty_result_keeps_its_column_types(self) -> None:
        """The shape that made a cache hit stream ``null``-typed columns."""
        table = _result_table(_Server(), _cursor(self.QUERY))
        assert table.num_rows == 0
        assert table.schema.field("n").type == pa.int64()
        assert pa.types.is_string(table.schema.field("s").type)


class TestNullFirstBatch:
    QUERY = """
        SELECT CAST(NULL AS BIGINT) AS n UNION ALL SELECT CAST(7 AS BIGINT) ORDER BY n NULLS FIRST
    """

    def test_a_leading_null_does_not_decide_the_type(self) -> None:
        """The CFL pad case the row path scans extra rows to work around.

        Taking the driver's schema removes the need to scan at all.
        """
        table = _result_table(_Server(), _cursor(self.QUERY))
        assert table.schema.field("n").type == pa.int64()
        assert table.column("n").to_pylist() == [None, 7]


class TestFallback:
    def test_a_cursor_without_arrow_still_works(self) -> None:
        """MySQL aside this is now the exception, but it has to keep working."""

        class RowOnlyCursor:
            description = (("name", None, None, None, None, None, None),)

            def __init__(self) -> None:
                self._chunks = [[("a",), ("b",)], []]

            def fetchmany(self, size: int) -> list[tuple[Any, ...]]:
                return self._chunks.pop(0) if self._chunks else []

        table = _result_table(_Server(), RowOnlyCursor())
        assert table.column("name").to_pylist() == ["a", "b"]

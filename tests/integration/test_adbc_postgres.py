"""Live ADBC-postgres regression tests.

Off by default (need a real Postgres). Run with::

    OB_PG_URI=postgresql://user:pass@host:5432/db pytest -m adbc

Default URI is ``postgresql://postgres:postgres@localhost:5432/postgres``.

These exercise the path that flipped the live deploy on its head:
ADBC delivers ``NUMERIC`` columns as Arrow string-extension types, so the
cell arrives at the executor as a Python ``str`` rather than ``Decimal``.
``_arrow_to_rows`` now parses them to ``Decimal`` once at the executor
layer; format_row keeps a defensive string-parse fallback.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.adbc


def _connect():  # type: ignore[no-untyped-def]
    """Open an ADBC postgres connection or skip the test if unreachable."""
    from adbc_driver_postgresql import dbapi

    uri = os.environ.get("OB_PG_URI", "postgresql://postgres:postgres@localhost:5432/postgres")
    try:
        return dbapi.connect(uri)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"ADBC postgres unreachable at {uri}: {exc}")


def test_adbc_postgres_numeric_is_string_in_pydict() -> None:
    """Pin the assumption: ADBC postgres NUMERIC → Arrow opaque[string].

    If a future ADBC release switches to ``decimal128`` we want this to
    fail loudly so we can re-evaluate the executor-side parse.
    """
    import pyarrow as pa

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT CAST(2045134942.09 AS NUMERIC(18, 2)) AS revenue")
        table = cur.fetch_arrow_table()
        field = table.schema.field("revenue")
        assert pa.types.is_extension_type(field.type), f"Expected extension type, got {field.type}"
        cell = table.to_pydict()["revenue"][0]
        assert isinstance(cell, str), f"Expected str cell, got {type(cell).__name__}"
        cur.close()
    finally:
        conn.close()


def test_executor_parses_adbc_numeric_string_to_decimal() -> None:
    """``_arrow_to_rows`` normalises the ADBC string back to Decimal so
    every downstream consumer (format_row, JSON, TSV) sees a numeric.
    """
    from orionbelt.service.db_executor import _arrow_to_rows

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT CAST(2045134942.09 AS NUMERIC(18, 2)) AS revenue")
        table = cur.fetch_arrow_table()
        rows = _arrow_to_rows(table)
        assert len(rows) == 1
        cell = rows[0][0]
        # _serialize_value(Decimal) returns float, so the executor surfaces a
        # plain Python number — readable by every downstream consumer.
        assert isinstance(cell, float)
        assert cell == pytest.approx(2045134942.09)
        cur.close()
    finally:
        conn.close()


def test_format_row_renders_locale_for_adbc_numeric() -> None:
    """End-to-end: ADBC NUMERIC → executor → format_row → ``"2.045.134.942,09"``."""
    from orionbelt.service.db_executor import _arrow_to_rows
    from orionbelt.service.value_formatting import format_row

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT CAST(2045134942.09 AS NUMERIC(18, 2)) AS revenue")
        table = cur.fetch_arrow_table()
        rows = _arrow_to_rows(table)
        out = format_row(
            row=rows[0],
            column_names=["revenue"],
            fmt_map={"revenue": "#,##0.00"},
            type_map={"revenue": "decimal(18, 2)"},
            locale="de",
        )
        assert out == ["2.045.134.942,09"]
        cur.close()
    finally:
        conn.close()


def test_format_row_handles_high_precision_string_directly() -> None:
    """Sanity: format_row on a raw string still works (defensive fallback).

    Uses a value that exceeds float64 precision to ensure the Decimal path
    is exercised inside format_row even if the executor change ever regresses.
    """
    from orionbelt.service.value_formatting import format_row

    out = format_row(
        row=["123456789012345678.1234567890"],
        column_names=["x"],
        fmt_map={"x": "#,##0.0000"},
        type_map={"x": "decimal(38, 10)"},
        locale="en",
    )
    # Exact full-precision render through Decimal → float64 would lose digits;
    # since format_row receives the string and parses to Decimal directly,
    # the result is precise.
    assert out == ["123,456,789,012,345,678.1235"]

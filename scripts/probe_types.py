"""Probe what Arrow type each driver returns for a declared OBML type.

A semantic layer's promise is that a measure declared ``decimal(18, 2)``
arrives as an exact fixed-point number. Whether it does is a property of the
driver, not of the SQL: seven of the eight ``ob-*`` drivers already hand back
an Arrow table, so the open question is not "is it Arrow" but "is the Arrow
faithful" -- does the column come back as ``decimal128(18, 2)``, or as a
float, or as a string?

Each case is rendered through the engine's own ``cast_to_obml_type``, so what
is measured is what OBSL emits rather than a hand-spelled approximation of it.
Execution goes through ``ob_flight.db_router`` -- the same path
``service/db_executor`` uses -- so the result is the driver's real
``fetch_arrow_table()`` output.

This is the type-fidelity companion to ``probe_functions.py``: that one asks
what an engine *computes*, this one asks what its driver *returns*.

Usage::

    set -a && source .env && set +a
    uv run python scripts/probe_types.py duckdb          # local, no credentials
    uv run python scripts/probe_types.py postgres snowflake
    uv run python scripts/probe_types.py all             # every reachable engine

Each row prints a verdict, the Arrow type and the round-tripped value:

* ``EXACT``   -- the declared type came back unchanged.
* ``WIDENED`` -- still fixed-point, but a wider precision or scale. An engine
  widening the result of a SUM is doing the right thing; a widened *cast* is
  the engine's own decimal rules and is recorded, not judged.
* ``FAMILY``  -- the right family, a different width (an int8 where the model
  said integer). Value-dependent widths are worth knowing about: two pages of
  one column can disagree.
* ``LOSSY``   -- the fixed-point type became a float or a string. This is the
  one that silently costs money.
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

import pyarrow as pa

#: ``(label, SQL literal spliced into the cast, OBML type, wrap in SUM)``.
#: The aggregate cases are here because an aggregate is where an engine
#: actually decides a result precision, and it is the shape every OBSL
#: measure has.
CASES: list[tuple[str, str, str, bool]] = [
    ("decimal(18,2)", "2.55", "decimal(18, 2)", False),
    ("decimal(38,9)", "2.123456789", "decimal(38, 9)", False),
    ("decimal(18,2) big", "12345678901234567.89", "decimal(19, 2)", False),
    ("SUM decimal(18,2)", "2.55", "decimal(18, 2)", True),
    ("integer", "42", "integer", False),
    ("bigint", "9007199254740993", "bigint", False),
    ("double", "2.5", "double", False),
    ("string", "'x'", "string", False),
    ("boolean", "1", "boolean", False),
    ("date", "'2026-08-15'", "date", False),
    ("timestamp", "'2026-08-15 13:45:00'", "timestamp", False),
]

#: The Arrow type a faithful driver hands back for each non-decimal OBML type.
#: ``timestamp`` is deliberately absent: any unit counts, but a zone does not,
#: which :func:`_timestamp_verdict` checks separately.
EXPECTED: dict[str, pa.DataType] = {
    "integer": pa.int32(),
    "bigint": pa.int64(),
    "double": pa.float64(),
    "string": pa.string(),
    "boolean": pa.bool_(),
    "date": pa.date32(),
}

#: Engines whose ``SELECT`` needs a FROM clause for a bare scalar expression.
FROM_SUFFIX: dict[str, str] = {
    "dremio": " FROM (VALUES (1))",
}

#: Engines that reject an aggregate over a constant with no FROM clause.
NO_BARE_AGGREGATE = frozenset({"bigquery"})

ENGINES = (
    "duckdb",
    "postgres",
    "mysql",
    "clickhouse",
    "snowflake",
    "bigquery",
    "databricks",
    "dremio",
)


def _family(t: pa.DataType) -> str:
    for name, test in (
        ("decimal", pa.types.is_decimal),
        ("integer", pa.types.is_integer),
        ("floating", pa.types.is_floating),
        ("boolean", pa.types.is_boolean),
        ("date", pa.types.is_date),
        ("timestamp", pa.types.is_timestamp),
        ("null", pa.types.is_null),
    ):
        if test(t):
            return name
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "string"
    # ADBC wraps PostgreSQL NUMERIC as an opaque string extension type rather
    # than narrowing it to decimal128, so the storage type is what matters.
    storage = getattr(t, "storage_type", None)
    if storage is not None and pa.types.is_string(storage):
        return "string"
    return str(t)


def _decimal_verdict(obml: str, got: pa.DataType) -> str:
    precision, scale = (int(x) for x in obml[obml.index("(") + 1 : -1].split(","))
    if not pa.types.is_decimal(got):
        return f"LOSSY   {_family(got)}"
    if (got.precision, got.scale) == (precision, scale):
        return "EXACT"
    return f"WIDENED {got}"


def _timestamp_verdict(got: pa.DataType) -> str:
    if not pa.types.is_timestamp(got):
        return f"LOSSY   {_family(got)}"
    # OBML's cast vocabulary has one timestamp and it is the naive one, so a
    # zone here is a zone the model never declared.
    return "EXACT" if got.tz is None else f"ZONED   tz={got.tz}"


def verdict(obml: str, got: pa.DataType) -> str:
    """Judge the returned Arrow type against what the model declared."""
    if obml.startswith("decimal"):
        return _decimal_verdict(obml, got)
    if obml == "timestamp":
        return _timestamp_verdict(got)
    want = {"integer": "integer", "bigint": "integer", "double": "floating"}.get(obml, obml)
    if _family(got) != want:
        return f"LOSSY   {_family(got)}"
    expected = EXPECTED[obml]
    return "EXACT" if got == expected else f"FAMILY  {got}"


def render(engine: str) -> list[tuple[str, str, str]]:
    """:data:`CASES` as *engine* would emit them: (label, OBML type, SQL)."""
    from orionbelt.ast.nodes import FunctionCall, RawSQL
    from orionbelt.dialect.registry import DialectRegistry
    from orionbelt.models.types import parse_data_type

    dialect = DialectRegistry.get(engine)
    out: list[tuple[str, str, str]] = []
    for label, literal, obml_type, aggregate in CASES:
        if aggregate and engine in NO_BARE_AGGREGATE:
            continue
        expr: Any = dialect.cast_to_obml_type(RawSQL(sql=literal), parse_data_type(obml_type))
        if aggregate:
            expr = FunctionCall(name="SUM", args=[expr])
        out.append((label, obml_type, dialect.compile_expr(expr)))
    return out


def fetch(engine: str, sql: str) -> pa.Table:
    from ob_flight.db_router import get_connection

    with get_connection(engine) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(sql + FROM_SUFFIX.get(engine, ""))
            return cursor.fetch_arrow_table()
        finally:
            cursor.close()


def probe(engine: str) -> None:
    print(f"\n===== {engine}")
    cases = render(engine)
    tables: dict[int, pa.Table] = {}
    try:
        combined = "SELECT " + ", ".join(f"{sql} AS c{i}" for i, (_, _, sql) in enumerate(cases))
        table = fetch(engine, combined)
        tables = {i: table.select([i]) for i in range(len(cases))}
    except Exception:
        # One unsupported cast must not blank the whole engine: retry per case
        # so the rest of the matrix still gets filled in.
        for i, (label, _, sql) in enumerate(cases):
            try:
                tables[i] = fetch(engine, f"SELECT {sql} AS c{i}")
            except Exception as exc:  # noqa: BLE001 — a failure is a result here
                detail = str(exc).splitlines()[0][:64] if str(exc).strip() else type(exc).__name__
                print(f"  {label:19} ERR     {detail}")
    for i, (label, obml_type, _) in enumerate(cases):
        table = tables.get(i)
        if table is None:
            continue
        arrow_type = table.schema.field(0).type
        value = table.column(0)[0].as_py()
        print(f"  {label:19} {verdict(obml_type, arrow_type):24} {str(arrow_type):28} {value!r}")


def main(argv: list[str]) -> int:
    requested = argv[1:]
    if not requested:
        print(f"usage: probe_types.py <{' | '.join(ENGINES)} | all>", file=sys.stderr)
        return 2
    engines = list(ENGINES) if requested == ["all"] else requested
    unknown = [e for e in engines if e not in ENGINES]
    if unknown:
        print(f"unknown engine(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    for engine in engines:
        try:
            probe(engine)
        except Exception:
            # Unreachable is the normal state for an engine with no live
            # target; print enough to tell that apart from a probe bug.
            print(f"\n===== {engine}\n  UNREACHABLE")
            traceback.print_exc(limit=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

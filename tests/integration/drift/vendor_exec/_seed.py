"""Vendor-execution seed loader.

Extracts the bundled commerce DuckDB seed once at session start and
provides per-vendor loaders that materialise the same data inside a
testcontainer (Postgres / MySQL / ClickHouse) or an in-memory DuckDB
under the ``orionbelt_1`` schema/database. The OBSL-emitted SQL
references ``orionbelt_1.<table>`` directly, so the schema name must
match the model's ``schema:`` field.

Type fidelity is intentionally loose at the source side — the bundled
DuckDB stores numeric columns as ``DOUBLE`` and the OBSL compiler
applies ``CAST(... AS DECIMAL(p, s))`` at query time per measure. The
seed mirrors that: ``DOUBLE`` everywhere, casts happen in the
generated SQL.

Every seeder builds its full statement list *before* touching the
connection and mirrors it to ``seed_sql/<vendor>/{01_schema,02_data}.sql``
(gitignored). Those dumps are exactly what ran, so they double as a
backup and as standalone scripts anyone can replay against their own
database. Generate them without Docker or a live connection with::

    uv run python tests/integration/drift/vendor_exec/_seed.py

Set ``OBSL_SEED_SQL_DIR`` to redirect the output directory, or to the
empty string to skip dumping entirely.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[4]
COMMERCE_DUCKDB = REPO_ROOT / "examples" / "orionbelt_1_commerce.duckdb"
SCHEMA = "orionbelt_1"

# ----------------------------------------------------------------------
# Source extraction (run once per session, cheap — ~26k rows total)
# ----------------------------------------------------------------------


def _read_source() -> dict[str, dict[str, Any]]:
    """Return ``{table_name: {"columns": [(name, duckdb_type)], "rows": [(...)]}}``."""
    if not COMMERCE_DUCKDB.exists():
        raise FileNotFoundError(
            f"Bundled DuckDB seed not found at {COMMERCE_DUCKDB}. "
            "Run scripts/build_demo_duckdb.py to generate it."
        )
    conn = duckdb.connect(database=str(COMMERCE_DUCKDB), read_only=True)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{SCHEMA}' ORDER BY table_name"
            ).fetchall()
        ]
        out: dict[str, dict[str, Any]] = {}
        for tbl in tables:
            cols = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                f"WHERE table_schema = '{SCHEMA}' AND table_name = '{tbl}' "
                "ORDER BY ordinal_position"
            ).fetchall()
            rows = conn.execute(f'SELECT * FROM "{SCHEMA}"."{tbl}"').fetchall()
            out[tbl] = {"columns": cols, "rows": rows}
        return out
    finally:
        conn.close()


_CACHE: dict[str, dict[str, Any]] | None = None


def get_source() -> dict[str, dict[str, Any]]:
    """Memoised ``_read_source`` — keeps the seed DuckDB read once per process."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _read_source()
    return _CACHE


# ----------------------------------------------------------------------
# Per-dialect vendor specs
# ----------------------------------------------------------------------


# Source ``DOUBLE`` columns in the bundled seed are all clean 2-dp money
# values. Loading them into the target as ``DECIMAL(18, 2)`` makes every
# engine's SUM/AVG/CAST exact and identical, eliminating cross-vendor
# float drift on monetary aggregates. The OBSL compiler already casts
# measure outputs to ``DECIMAL(18, 2)`` per the model's declared type,
# so the source-column choice never propagates to query output — but it
# does prevent IEEE-754 last-bit drift inside the engine's accumulator.
#
# Identifiers stay lowercase and quoted everywhere, matching what the
# compiler emits per dialect (see tests/integration/drift/compile_only/).
# Snowflake in particular treats a quoted lowercase name as
# case-sensitive lowercase, so unquoted DDL would not resolve.
@dataclass(frozen=True)
class VendorSpec:
    """Everything that differs between one vendor's seed script and another's."""

    name: str
    types: dict[str, str]
    quote: str
    # ``SCHEMA`` for namespace-style engines, ``DATABASE`` for MySQL and
    # ClickHouse. ``None`` means the engine has no CREATE-able namespace
    # we can assume (Dremio), so tables are dropped individually instead.
    container: str | None
    executed: bool
    table_opts: Callable[[list[tuple[str, str]]], str] = lambda _cols: ""
    # ANSI ``DATE 'yyyy-mm-dd'`` literals instead of bare strings. The
    # four container-tested engines coerce plain strings happily and are
    # left as-is; the cloud engines are stricter.
    date_literals: bool = False
    # GoogleSQL (BigQuery) escapes a single quote inside a string literal with a
    # backslash (``\'``); the SQL-standard doubling (``''``) parses there as two
    # adjacent literals ("O''Brien" -> 'O' 'Brien') and errors. Every other
    # dialect here accepts ``''``. Set True to switch to backslash escaping.
    backslash_escape: bool = False
    notes: tuple[str, ...] = ()

    def q(self, name: str) -> str:
        return f"{self.quote}{name}{self.quote}"

    def table(self, tbl: str) -> str:
        return f"{self.q(SCHEMA)}.{self.q(tbl)}"

    def columns_clause(self, columns: list[tuple[str, str]]) -> str:
        return ", ".join(f"{self.q(name)} {self.types[dtype]}" for name, dtype in columns)


def _merge_tree(columns: list[tuple[str, str]]) -> str:
    # Pick the first column as the ORDER BY key — IDs are first by
    # convention in this schema, and any column suffices for our query
    # workload (no real-world ordering matters here).
    return f" ENGINE = MergeTree() ORDER BY `{columns[0][0]}`"


_SPECS: dict[str, VendorSpec] = {
    "postgres": VendorSpec(
        name="postgres",
        types={"VARCHAR": "TEXT", "DATE": "DATE", "DOUBLE": "DECIMAL(18, 2)"},
        quote='"',
        container="SCHEMA",
        executed=True,
    ),
    "mysql": VendorSpec(
        name="mysql",
        types={"VARCHAR": "VARCHAR(255)", "DATE": "DATE", "DOUBLE": "DECIMAL(18, 2)"},
        quote="`",
        container="DATABASE",
        executed=True,
    ),
    "clickhouse": VendorSpec(
        name="clickhouse",
        types={"VARCHAR": "String", "DATE": "Date", "DOUBLE": "Decimal(18, 2)"},
        quote="`",
        container="DATABASE",
        executed=True,
        table_opts=_merge_tree,
    ),
    "duckdb": VendorSpec(
        name="duckdb",
        types={"VARCHAR": "VARCHAR", "DATE": "DATE", "DOUBLE": "DECIMAL(18, 2)"},
        quote='"',
        container="SCHEMA",
        executed=True,
    ),
    "bigquery": VendorSpec(
        name="bigquery",
        types={"VARCHAR": "STRING", "DATE": "DATE", "DOUBLE": "NUMERIC(18, 2)"},
        quote="`",
        container="SCHEMA",
        executed=False,
        date_literals=True,
        backslash_escape=True,
        notes=("Run against the target project. `orionbelt_1` is the dataset.",),
    ),
    "snowflake": VendorSpec(
        name="snowflake",
        types={"VARCHAR": "VARCHAR", "DATE": "DATE", "DOUBLE": "NUMBER(18, 2)"},
        quote='"',
        container="SCHEMA",
        executed=False,
        date_literals=True,
        notes=("USE DATABASE <db> first. Quoted lowercase names are case-sensitive.",),
    ),
    "databricks": VendorSpec(
        name="databricks",
        types={"VARCHAR": "STRING", "DATE": "DATE", "DOUBLE": "DECIMAL(18, 2)"},
        quote="`",
        container="SCHEMA",
        executed=False,
        table_opts=lambda _cols: " USING DELTA",
        date_literals=True,
        notes=("USE CATALOG <catalog> first. `orionbelt_1` is the schema.",),
    ),
    "dremio": VendorSpec(
        name="dremio",
        types={"VARCHAR": "VARCHAR", "DATE": "DATE", "DOUBLE": "DECIMAL(18, 2)"},
        quote='"',
        container=None,
        executed=False,
        date_literals=True,
        notes=(
            "Dremio has no CREATE SCHEMA: prefix every table with a writable",
            '(Iceberg-capable) source, e.g. "nas"."orionbelt_1"."sales".',
        ),
    ),
}

VENDORS: tuple[str, ...] = tuple(_SPECS)


# ----------------------------------------------------------------------
# Row → SQL literal rendering (kept separate from the connection so the
# same logic feeds a pre-prepared INSERT or a streaming bulk loader).
# ----------------------------------------------------------------------


def _lit(v: Any, *, date_literals: bool = False, backslash_escape: bool = False) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, date):
        return f"DATE '{v.isoformat()}'" if date_literals else f"'{v.isoformat()}'"
    s = str(v)
    # GoogleSQL escapes a quote as ``\'``; every other dialect doubles it as
    # ``''``. In the backslash path, escape backslashes first so they don't
    # consume the quote escape.
    s = s.replace("\\", "\\\\").replace("'", "\\'") if backslash_escape else s.replace("'", "''")
    return f"'{s}'"


def _values_batches(
    rows: list[tuple[Any, ...]],
    batch_size: int,
    *,
    date_literals: bool = False,
    backslash_escape: bool = False,
) -> list[str]:
    """Yield ``(...), (...)`` strings, ``batch_size`` rows per chunk."""
    out: list[str] = []
    for i in range(0, len(rows), batch_size):
        chunk = rows[i : i + batch_size]
        out.append(
            ", ".join(
                "("
                + ", ".join(
                    _lit(v, date_literals=date_literals, backslash_escape=backslash_escape)
                    for v in row
                )
                + ")"
                for row in chunk
            )
        )
    return out


_BATCH_SIZE = 500


# ----------------------------------------------------------------------
# Statement builders
#
# One spec-driven builder covers all eight dialects. The four
# container-tested vendors get exactly the statements their seeder runs;
# the four cloud/remote ones are script-only (their tests bulk-load
# parquet through a native loader instead — see ``VendorSpec.executed``).
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SeedPlan:
    """Statements for one vendor, grouped so they can be replayed two ways.

    ``setup`` is the namespace preamble; ``tables`` pairs each table's DDL
    with its INSERTs. Seeders walk it table-by-table (create, then load,
    exactly as they always have); the dumper flattens it into a DDL file
    and a DML file. One source, so the scripts and the seed never diverge.

    ``grants`` is kept apart because it is testcontainer plumbing: it has
    to run, but a grant to a throwaway test user is noise in a script
    someone else is meant to run, so the dump leaves it out.
    """

    setup: list[str]
    tables: list[tuple[list[str], list[str]]]
    grants: list[str] = field(default_factory=list)

    @property
    def ddl(self) -> list[str]:
        return self.setup + [stmt for table_ddl, _ in self.tables for stmt in table_ddl]

    @property
    def dml(self) -> list[str]:
        return [stmt for _, inserts in self.tables for stmt in inserts]


def plan(vendor: str, *, grant_user: str | None = None) -> SeedPlan:
    """Build the full statement plan for ``vendor``.

    ``grant_user`` appends the MySQL grant the testcontainer needs; it is
    omitted from the shareable dumps, where it would be noise.
    """
    spec = _SPECS[vendor]
    setup: list[str] = []
    if spec.container:
        # CASCADE is a SCHEMA-only clause: MySQL and ClickHouse reject it
        # on DROP DATABASE, which drops its contents unconditionally.
        cascade = " CASCADE" if spec.container == "SCHEMA" else ""
        setup.append(f"DROP {spec.container} IF EXISTS {spec.q(SCHEMA)}{cascade}")
        setup.append(f"CREATE {spec.container} {spec.q(SCHEMA)}")
    grants: list[str] = []
    if grant_user and vendor == "mysql":
        grants.append(f"GRANT ALL PRIVILEGES ON {spec.q(SCHEMA)}.* TO '{grant_user}'@'%'")
        grants.append("FLUSH PRIVILEGES")

    tables: list[tuple[list[str], list[str]]] = []
    for tbl, payload in get_source().items():
        table_ddl: list[str] = []
        if not spec.container:
            # No CREATE-able namespace to drop wholesale (Dremio).
            table_ddl.append(f"DROP TABLE IF EXISTS {spec.table(tbl)}")
        table_ddl.append(
            f"CREATE TABLE {spec.table(tbl)} ({spec.columns_clause(payload['columns'])})"
            f"{spec.table_opts(payload['columns'])}"
        )
        inserts = [
            f"INSERT INTO {spec.table(tbl)} VALUES {batch}"
            for batch in _values_batches(
                payload["rows"],
                _BATCH_SIZE,
                date_literals=spec.date_literals,
                backslash_escape=spec.backslash_escape,
            )
        ]
        tables.append((table_ddl, inserts))
    return SeedPlan(setup=setup, tables=tables, grants=grants)


def _run(seed_plan: SeedPlan, execute: Callable[[str], Any]) -> None:
    """Replay a plan in seed order: preamble, grants, then per table create + load."""
    for stmt in (*seed_plan.setup, *seed_plan.grants):
        execute(stmt)
    for table_ddl, inserts in seed_plan.tables:
        for stmt in (*table_ddl, *inserts):
            execute(stmt)


# ----------------------------------------------------------------------
# Script dumping — gitignored per-vendor scripts, generated on seed
# ----------------------------------------------------------------------

_DEFAULT_SEED_SQL_DIR = Path(__file__).resolve().parent / "seed_sql"


def seed_sql_dir() -> Path | None:
    """Destination for the dumped scripts, or ``None`` when disabled.

    ``OBSL_SEED_SQL_DIR`` overrides the location; setting it to the
    empty string turns dumping off (useful in CI, where the artefacts
    are thrown away anyway).
    """
    override = os.environ.get("OBSL_SEED_SQL_DIR")
    if override is None:
        return _DEFAULT_SEED_SQL_DIR
    return Path(override) if override.strip() else None


def _render(vendor: str, kind: str, statements: list[str]) -> str:
    """Join statements into a runnable script with a provenance header.

    No timestamp: the output is a pure function of the bundled seed, so
    identical data yields byte-identical files and a diff means the data
    actually changed.
    """
    spec = _SPECS[vendor]
    provenance = (
        "Mirrors exactly what the vendor_exec seed executes."
        if spec.executed
        else "Generated only — this vendor's tests bulk-load parquet natively."
    )
    lines = [
        f"OrionBelt commerce seed ({SCHEMA}) — {vendor} {kind}",
        "Generated from examples/orionbelt_1_commerce.duckdb by",
        "tests/integration/drift/vendor_exec/_seed.py. Do not edit by hand.",
        provenance,
        "Run 01_schema.sql before 02_data.sql.",
        *spec.notes,
    ]
    header = "".join(f"-- {line}\n" for line in lines)
    return header + "\n" + "".join(f"{stmt};\n" for stmt in statements)


def dump_scripts(vendor: str, seed_plan: SeedPlan) -> Path | None:
    """Write ``<dir>/<vendor>/{01_schema,02_data}.sql``; return the folder.

    Dumping is a convenience, never a reason to fail a test run, so a
    read-only or full filesystem downgrades to a warning.
    """
    base = seed_sql_dir()
    if base is None:
        return None
    out = base / vendor
    try:
        out.mkdir(parents=True, exist_ok=True)
        (out / "01_schema.sql").write_text(_render(vendor, "schema", seed_plan.ddl))
        (out / "02_data.sql").write_text(_render(vendor, "data", seed_plan.dml))
    except OSError as exc:
        warnings.warn(f"Could not write seed SQL scripts to {out}: {exc}", stacklevel=2)
        return None
    return out


# ----------------------------------------------------------------------
# Per-vendor seeders
# ----------------------------------------------------------------------


def seed_postgres(conn: Any) -> None:
    """Create ``orionbelt_1`` schema + tables and bulk-load all rows."""
    seed_plan = plan("postgres")
    dump_scripts("postgres", seed_plan)
    cur = conn.cursor()
    _run(seed_plan, cur.execute)
    conn.commit()


def seed_mysql(conn: Any, *, grant_user: str | None = None) -> None:
    """Create ``orionbelt_1`` database (= MySQL schema) + tables and load rows.

    The testcontainer's default user is permissioned only for the
    container's default database; granting it on ``orionbelt_1`` lets
    the same connection reach the seeded tables. ``grant_user`` should
    be the connection's MySQL user — set to ``None`` to skip grant
    (e.g. when seeding as ``root``).
    """
    seed_plan = plan("mysql", grant_user=grant_user)
    dump_scripts("mysql", seed_plan)
    cur = conn.cursor()
    _run(seed_plan, cur.execute)
    conn.commit()


def seed_clickhouse(client: Any) -> None:
    """ClickHouse: CREATE DATABASE + MergeTree tables; INSERT in chunks."""
    seed_plan = plan("clickhouse")
    dump_scripts("clickhouse", seed_plan)
    _run(seed_plan, client.command)


def seed_duckdb(conn: Any) -> None:
    """In-memory DuckDB seed — same SQL path as the testcontainer engines.

    Useful as a "control" target: the same loader code feeds DuckDB,
    Postgres, MySQL, and ClickHouse, so a divergence in one vendor's
    rows is unambiguously attributable to that engine, not to the
    seed loader.
    """
    seed_plan = plan("duckdb")
    dump_scripts("duckdb", seed_plan)
    _run(seed_plan, conn.execute)


# ----------------------------------------------------------------------
# Standalone generation — no container, no live connection
# ----------------------------------------------------------------------


def dump_all() -> list[Path]:
    """Write every vendor's scripts and return the folders written."""
    written: list[Path] = []
    for vendor in VENDORS:
        out = dump_scripts(vendor, plan(vendor))
        if out is not None:
            written.append(out)
    return written


if __name__ == "__main__":
    for folder in dump_all():
        print(folder)  # noqa: T201

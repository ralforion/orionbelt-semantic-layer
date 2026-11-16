"""Probe schema inspection on each engine: the catalog SQL against a real schema.

The unit tests read most engines' catalogs through a stand-in runner, which
checks how rows are read but not whether a statement asks the right question.
This probe creates a small schema on the engine, inspects it through
``inspect_schema`` over OBSL's own connections, compares what comes back with
what the engine was told, and drops the schema again:

* ``region``: a composite primary key declared in another order than its
  columns (``PRIMARY KEY (code, id)``), a nullable column, a table comment;
* ``customer``: a composite foreign key to ``region``, a column comment;
* ``customer_names``: a view.

Then a table filter, ``views=false``, view definitions, a filter matching
nothing, and an unknown schema with and without a filter.

Usage::

    set -a && source .env && set +a
    uv run python scripts/probe_schema_inspection.py duckdb clickhouse
    uv run python scripts/probe_schema_inspection.py all

Each engine reads its usual variables; ``scripts/probe_types.py`` shows the
MySQL and Dremio containers. DuckDB runs on a temporary file. The schema is
built over OBSL's connection where its driver takes DDL, and beside it where
not: the ``duckdb`` module (OBSL opens a file read-only), ``clickhouse-connect``,
and Dremio's REST API (``DREMIO_REST_URL``, default port 19047 as in
``demo/dremio``). Dremio builds its tables in ``$scratch`` under an
``obsl_inspect_`` prefix and its view in the user's home space.

Engines without a feature (Dremio has no keys, comments or NOT NULL,
ClickHouse no foreign keys) skip those checks rather than fail them. Exits 1
when a check fails.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "obsl_inspect_probe"
UNKNOWN = "obsl_inspect_nowhere"

_STANDARD_DDL = (
    "CREATE SCHEMA {s}",
    "CREATE TABLE {s}.region (id INTEGER NOT NULL, code VARCHAR(8) NOT NULL, "
    "label VARCHAR(40), PRIMARY KEY (code, id))",
    "COMMENT ON TABLE {s}.region IS 'Sales regions'",
    "CREATE TABLE {s}.customer (id INTEGER NOT NULL PRIMARY KEY, name VARCHAR(40) NOT NULL, "
    "region_id INTEGER, region_code VARCHAR(8), "
    "FOREIGN KEY (region_code, region_id) REFERENCES {s}.region (code, id))",
    "COMMENT ON COLUMN {s}.customer.name IS 'Full name'",
    "CREATE VIEW {s}.customer_names AS SELECT name FROM {s}.customer",
)


@dataclass
class Engine:
    """How to build the probe schema on one engine, and what it can report."""

    ddl: tuple[str, ...]
    drop: tuple[str, ...]
    schema: str = SCHEMA
    view_schema: str | None = None  # where the view lives, when not with the tables; {user}
    keys: bool = True
    foreign_keys: bool = True
    comments: bool = True
    not_null: bool = True
    upper: bool = False  # unquoted names fold to upper case
    prefix: str = ""  # on the tables' names, in a schema others use too
    environ: Callable[[], dict[str, str]] = field(default=lambda: {})


ENGINES: dict[str, Engine] = {
    "duckdb": Engine(
        ddl=_STANDARD_DDL,
        drop=("DROP SCHEMA IF EXISTS {s} CASCADE",),
        environ=lambda: {
            "DUCKDB_DATABASE": os.path.join(tempfile.mkdtemp(), "probe.duckdb"),
        },
    ),
    "postgres": Engine(
        ddl=_STANDARD_DDL,
        drop=("DROP SCHEMA IF EXISTS {s} CASCADE",),
    ),
    "snowflake": Engine(
        ddl=_STANDARD_DDL,
        drop=("DROP SCHEMA IF EXISTS {s} CASCADE",),
        schema=SCHEMA.upper(),
        upper=True,
    ),
    "mysql": Engine(
        ddl=(
            "CREATE DATABASE {s}",
            "CREATE TABLE {s}.region (id INT NOT NULL, code VARCHAR(8) NOT NULL, "
            "label VARCHAR(40), PRIMARY KEY (code, id)) COMMENT = 'Sales regions'",
            "CREATE TABLE {s}.customer (id INT NOT NULL PRIMARY KEY, "
            "name VARCHAR(40) NOT NULL COMMENT 'Full name', region_id INT, "
            "region_code VARCHAR(8), "
            "FOREIGN KEY (region_code, region_id) REFERENCES {s}.region (code, id))",
            "CREATE VIEW {s}.customer_names AS SELECT name FROM {s}.customer",
        ),
        drop=("DROP DATABASE IF EXISTS {s}",),
    ),
    "clickhouse": Engine(
        ddl=(
            "CREATE DATABASE {s}",
            "CREATE TABLE {s}.region (id Int32, code String, label Nullable(String)) "
            "ENGINE = MergeTree ORDER BY (code, id) COMMENT 'Sales regions'",
            "CREATE TABLE {s}.customer (id Int32, name String COMMENT 'Full name', "
            "region_id Nullable(Int32), region_code LowCardinality(Nullable(String))) "
            "ENGINE = MergeTree ORDER BY id",
            "CREATE VIEW {s}.customer_names AS SELECT name FROM {s}.customer",
        ),
        drop=("DROP DATABASE IF EXISTS {s}",),
        foreign_keys=False,
    ),
    "bigquery": Engine(
        ddl=(
            "CREATE SCHEMA {s}",
            "CREATE TABLE {s}.region (id INT64 NOT NULL, code STRING NOT NULL, label STRING, "
            "PRIMARY KEY (code, id) NOT ENFORCED) OPTIONS (description = 'Sales regions')",
            "CREATE TABLE {s}.customer (id INT64 NOT NULL, "
            "name STRING NOT NULL OPTIONS (description = 'Full name'), "
            "region_id INT64, region_code STRING, PRIMARY KEY (id) NOT ENFORCED, "
            "FOREIGN KEY (region_code, region_id) REFERENCES {s}.region (code, id) NOT ENFORCED)",
            "CREATE VIEW {s}.customer_names AS SELECT name FROM {s}.customer",
        ),
        drop=("DROP SCHEMA IF EXISTS {s} CASCADE",),
    ),
    "databricks": Engine(
        ddl=(
            "CREATE SCHEMA {s}",
            "CREATE TABLE {s}.region (id INT NOT NULL, code STRING NOT NULL, label STRING, "
            "CONSTRAINT region_pk PRIMARY KEY (code, id)) COMMENT 'Sales regions'",
            "CREATE TABLE {s}.customer (id INT NOT NULL, name STRING NOT NULL COMMENT 'Full name', "
            "region_id INT, region_code STRING, CONSTRAINT customer_pk PRIMARY KEY (id), "
            "CONSTRAINT customer_region_fk FOREIGN KEY (region_code, region_id) "
            "REFERENCES {s}.region (code, id))",
            "CREATE VIEW {s}.customer_names AS SELECT name FROM {s}.customer",
        ),
        drop=("DROP SCHEMA IF EXISTS {s} CASCADE",),
    ),
    "dremio": Engine(
        # $scratch takes CREATE TABLE AS; a view goes to the user's home space.
        # Dremio lists a dataset of a file source once it has been queried.
        ddl=(
            "CREATE TABLE {s}.{p}region AS SELECT * FROM "
            "(VALUES (1, 'EU', 'Europe')) AS t(id, code, label)",
            "CREATE TABLE {s}.{p}customer AS SELECT * FROM "
            "(VALUES (1, 'Ann', 1, 'EU')) AS t(id, name, region_id, region_code)",
            "SELECT * FROM {s}.{p}region LIMIT 1",
            "SELECT * FROM {s}.{p}customer LIMIT 1",
            "CREATE VIEW {v}.{p}customer_names AS SELECT name FROM {s}.{p}customer",
        ),
        drop=(
            "DROP VIEW IF EXISTS {v}.{p}customer_names",
            "DROP TABLE IF EXISTS {s}.{p}customer",
            "DROP TABLE IF EXISTS {s}.{p}region",
        ),
        schema="$scratch",
        view_schema="@{user}",
        keys=False,
        foreign_keys=False,
        comments=False,
        not_null=False,
        prefix="obsl_inspect_",
    ),
}


@dataclass
class Report:
    rows: list[tuple[str, str, str, bool]] = field(default_factory=list)

    def check(self, what: str, expected: Any, got: Any) -> None:
        self.rows.append((what, repr(expected), repr(got), expected == got))


def _dremio_rest(environ: Mapping[str, str], sql: str) -> None:
    """Run *sql* as a Dremio job over REST (``DREMIO_REST_URL``) and wait for it."""
    import json
    import time
    import urllib.request

    base = environ.get("DREMIO_REST_URL") or f"http://{environ.get('DREMIO_HOST')}:19047"

    def call(path: str, body: dict[str, Any] | None = None, token: str = "") -> Any:
        request = urllib.request.Request(
            base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": token},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    login = {"userName": environ["DREMIO_USERNAME"], "password": environ["DREMIO_PASSWORD"]}
    token = "_dremio" + call("/apiv2/login", login)["token"]
    job = call("/api/v3/sql", {"sql": sql}, token)["id"]
    for _ in range(120):
        state = call(f"/api/v3/job/{job}", token=token)
        if state["jobState"] == "COMPLETED":
            return
        if state["jobState"] in {"FAILED", "CANCELED"}:
            raise RuntimeError(f"{sql}: {state.get('errorMessage', state['jobState'])}")
        time.sleep(0.5)
    raise TimeoutError(sql)


def _outcome(call: Callable[[], Any]) -> Any:
    """What *call* returns, or the error it raises, as a value to compare."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - reported as the check's result
        return f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"


def probe(name: str) -> Report:
    from ob_flight.datasources import DataSource
    from ob_flight.db_router import get_connection

    from orionbelt.service.schema_inspection import inspect_schema, list_schemas

    engine = ENGINES[name]
    environ = {**os.environ, **engine.environ()}
    view_schema = (
        engine.view_schema.format(user=environ.get("DREMIO_USERNAME", ""))
        if engine.view_schema
        else None
    )
    source = DataSource(name=f"probe_{name}", dialect=name, environ=environ)

    def n(value: str) -> str:
        return value.upper() if engine.upper else value

    def named(table: str) -> str:
        return n(engine.prefix + table)

    def render(statement: str) -> str:
        if name == "dremio":  # "$scratch", "@user"
            return statement.format(s=f'"{engine.schema}"', v=f'"{view_schema}"', p=engine.prefix)
        return statement.format(s=engine.schema, p=engine.prefix)

    def run(statements: tuple[str, ...], *, tolerate: bool) -> None:
        if name == "duckdb":
            # OBSL opens a DuckDB file read-only: build it beside OBSL.
            import duckdb

            with duckdb.connect(environ["DUCKDB_DATABASE"]) as con:
                for statement in statements:
                    con.execute(render(statement))
            return
        if name == "clickhouse":
            # OBSL's driver reads every answer as Arrow, which DDL does not send.
            import clickhouse_connect

            client = clickhouse_connect.get_client(
                host=source.getenv("CLICKHOUSE_HOST") or "localhost",
                port=int(source.getenv("CLICKHOUSE_PORT") or 8123),
                username=source.getenv("CLICKHOUSE_USERNAME") or "default",
                password=source.getenv("CLICKHOUSE_PASSWORD") or "",
            )
            try:
                for statement in statements:
                    try:
                        client.command(render(statement))
                    except Exception:
                        if not tolerate:
                            raise
            finally:
                client.close()
            return
        if name == "dremio":
            # OBSL's Flight SQL driver refuses DDL's answer (its nullability
            # differs from the announced schema): run it over Dremio's REST API.
            for statement in statements:
                try:
                    _dremio_rest(environ, render(statement))
                except Exception:
                    if not tolerate:
                        raise
            return
        with get_connection(source) as conn:
            cursor = conn.cursor()
            try:
                for statement in statements:
                    try:
                        cursor.execute(render(statement))
                    except Exception:
                        if not tolerate:
                            raise
            finally:
                cursor.close()

    report = Report()
    if name != "duckdb":
        run(engine.drop, tolerate=True)
    run(engine.ddl, tolerate=False)
    try:
        schemas = list_schemas(source)
        report.check("schema listed", True, engine.schema in schemas)
        report.check(
            "engine schemas left out",
            [],
            [s for s in schemas if s.lower() in {"information_schema", "pg_catalog", "sys"}],
        )

        info = inspect_schema(source, engine.schema, view_definitions=True)
        assert info is not None, "the probe schema was not found"
        tables = {t.name: t for t in info.tables}
        expected_tables = [(named("customer"), "table"), (named("region"), "table")]
        if view_schema is None:
            expected_tables.insert(1, (named("customer_names"), "view"))
        report.check(
            "tables and kinds",
            expected_tables,
            [(t.name, t.kind) for t in info.tables if t.name.startswith(n(engine.prefix))],
        )
        report.check("warnings", [], info.warnings)

        region, customer = tables[named("region")], tables[named("customer")]
        report.check(
            "region columns (name, ordinal)",
            [(n("id"), 1), (n("code"), 2), (n("label"), 3)],
            [(c.name, c.ordinal) for c in region.columns],
        )
        if engine.not_null:
            report.check(
                "region nullability",
                [False, False, True],
                [c.nullable for c in region.columns],
            )
        report.check(
            "customer nullability (region_code)",
            True,
            next(c.nullable for c in customer.columns if c.name == n("region_code")),
        )
        if engine.comments:
            report.check("table comment", "Sales regions", region.comment)
            report.check(
                "column comment",
                "Full name",
                next(c.comment for c in customer.columns if c.name == n("name")),
            )
        if engine.keys:
            report.check(
                "composite key in declared order", [n("code"), n("id")], region.primary_key
            )
            report.check("single key", [n("id")], customer.primary_key)
            report.check(
                "key flags on columns",
                [n("id"), n("code")],
                [c.name for c in region.columns if c.primary_key],
            )
        if engine.foreign_keys:
            report.check(
                "composite foreign key",
                [
                    (
                        (n("region_code"), n("region_id")),
                        engine.schema,
                        named("region"),
                        (n("code"), n("id")),
                    )
                ],
                [
                    (fk.columns, fk.referred_schema, fk.referred_table, fk.referred_columns)
                    for fk in customer.foreign_keys
                ],
            )

        views_at = view_schema or engine.schema
        with_views = inspect_schema(source, views_at, view_definitions=True)
        view = next(
            (
                t
                for t in (with_views.tables if with_views else [])
                if t.name == named("customer_names")
            ),
            None,
        )
        report.check("view listed", "view", view.kind if view else None)
        report.check(
            "view definition read",
            True,
            bool(view and view.view_definition and "name" in view.view_definition.lower()),
        )
        plain = inspect_schema(source, views_at)
        report.check(
            "no definition unless asked",
            None,
            next(
                (t.view_definition for t in plain.tables if t.name == named("customer_names")), "?"
            )
            if plain
            else "?",
        )
        no_views = inspect_schema(source, views_at, include_views=False)
        report.check(
            "views=false leaves the view out",
            False,
            any(t.name == named("customer_names") for t in (no_views.tables if no_views else [])),
        )

        only = inspect_schema(source, engine.schema, tables=[named("region")])
        report.check(
            "table filter",
            [(named("region"), region.primary_key)],
            [(t.name, t.primary_key) for t in only.tables] if only else None,
        )
        missing = inspect_schema(source, engine.schema, tables=["missing"])
        report.check("filter matching nothing", [], missing.tables if missing else None)
        report.check("unknown schema", None, _outcome(lambda: inspect_schema(source, UNKNOWN)))
        report.check(
            "unknown schema, filtered",
            None,
            _outcome(lambda: inspect_schema(source, UNKNOWN, tables=["missing"])),
        )
    finally:
        if name != "duckdb":  # a temporary file
            run(engine.drop, tolerate=True)
    return report


def main(argv: list[str]) -> int:
    names = list(ENGINES) if argv == ["all"] or not argv else argv
    failed = 0
    for name in names:
        print(f"\n## {name}\n")
        try:
            report = probe(name)
        except Exception as exc:  # noqa: BLE001 - the probe reports, it does not stop
            print(f"ERROR {type(exc).__name__}: {str(exc)[:400]}")
            failed += 1
            continue
        print("| Check | Expected | Got | |\n|---|---|---|---|")
        for what, expected, got, ok in report.rows:
            print(f"| {what} | {expected} | {got} | {'ok' if ok else 'FAIL'} |")
            failed += not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

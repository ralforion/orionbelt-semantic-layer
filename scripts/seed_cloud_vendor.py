"""Load the commerce seed into a cloud warehouse.

The vendor-execution suite seeds Postgres, MySQL and ClickHouse into a
throwaway testcontainer on every run. The cloud warehouses have no container,
and re-loading 26k rows over the network per session would dominate the suite,
so their seed is applied once, out of band, by this script. The data is static,
so "once" is the right cadence; the fixtures assert the schema is present and
current rather than creating it.

The SQL executed is exactly the generated dump under
``tests/integration/drift/vendor_exec/seed_sql/<vendor>/``, which
``_seed.py`` writes from ``examples/orionbelt_1_commerce.duckdb``. Regenerate
those with::

    uv run python tests/integration/drift/vendor_exec/_seed.py

Usage::

    uv run python scripts/seed_cloud_vendor.py snowflake
    uv run python scripts/seed_cloud_vendor.py --check snowflake

Credentials come from the environment (``.env`` is not read automatically -
``set -a && source .env && set +a`` first). This drops and recreates the
``orionbelt_1`` schema, so point it only at a database you own.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_SQL = REPO_ROOT / "tests" / "integration" / "drift" / "vendor_exec" / "seed_sql"
SCHEMA = "orionbelt_1"
EXPECTED_SALES_ROWS = 10000


def statements(vendor: str) -> Iterator[tuple[str, str]]:
    """Yield ``(filename, statement)`` for a vendor's seed dump, comments stripped."""
    for name in ("01_schema.sql", "02_data.sql"):
        path = SEED_SQL / vendor / name
        if not path.exists():
            raise SystemExit(f"No seed dump at {path}. Generate it with _seed.py first.")
        for chunk in path.read_text(encoding="utf-8").split(";\n"):
            body = "\n".join(
                line for line in chunk.splitlines() if not line.strip().startswith("--")
            ).strip()
            if body:
                yield name, body


def _snowflake() -> tuple[Callable[[str], None], Callable[[str], list[tuple]], Callable[[], None]]:
    import snowflake.connector

    required = ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD", "SNOWFLAKE_DATABASE")
    if missing := [n for n in required if not os.environ.get(n)]:
        raise SystemExit(f"Missing environment: {', '.join(missing)}")
    conn = snowflake.connector.connect(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.environ["SNOWFLAKE_USER"],
        password=os.environ["SNOWFLAKE_PASSWORD"],
        warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE"),
        database=os.environ["SNOWFLAKE_DATABASE"],
    )
    cur = conn.cursor()
    return (lambda sql: cur.execute(sql), lambda sql: cur.execute(sql).fetchall(), conn.close)


VENDORS: dict[str, Callable[[], tuple]] = {"snowflake": _snowflake}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vendor", choices=sorted(VENDORS))
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only report whether the schema is loaded and current; change nothing.",
    )
    args = parser.parse_args(argv)

    execute, query, close = VENDORS[args.vendor]()
    try:
        if args.check:
            try:
                rows = query(f'SELECT COUNT(*) FROM "{SCHEMA}"."sales"')[0][0]
            except Exception as exc:  # noqa: BLE001 — any failure means "not loaded"
                print(f"{args.vendor}: schema '{SCHEMA}' not loaded ({exc})")
                return 1
            ok = rows == EXPECTED_SALES_ROWS
            print(f"{args.vendor}: sales has {rows} rows, expected {EXPECTED_SALES_ROWS}")
            return 0 if ok else 1

        counts: dict[str, int] = {}
        for name, statement in statements(args.vendor):
            execute(statement)
            counts[name] = counts.get(name, 0) + 1
        for name, n in counts.items():
            print(f"{args.vendor}: {name} -> {n} statements")
        rows = query(f'SELECT COUNT(*) FROM "{SCHEMA}"."sales"')[0][0]
        print(f"{args.vendor}: sales has {rows} rows")
        if rows != EXPECTED_SALES_ROWS:
            print(f"WARNING: expected {EXPECTED_SALES_ROWS}")
            return 1
        return 0
    finally:
        close()


if __name__ == "__main__":
    sys.exit(main())

"""Regenerate the parquet fixtures in ``tests/fixtures/commerce/`` from the
bundled demo DuckDB.

Reads ``examples/orionbelt_1_commerce.duckdb`` (the same data that powers the
UI default), samples fact tables down to ~10% with a fixed seed, keeps
dimension tables whole, and writes one parquet file per table next to this
script. Run by hand when the demo data changes::

    uv run python tests/fixtures/commerce/_export.py

The committed parquet files are the canonical test data — the demo
``.duckdb`` is **not** read at test time. Cross-vendor integration tests
load these parquet files into their target database, run the same query
against both DuckDB-truth and the target vendor, then diff the rows.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCE_DB = REPO_ROOT / "examples" / "orionbelt_1_commerce.duckdb"
OUT_DIR = Path(__file__).resolve().parent

# Sampling rules. Dimension tables are tiny — keep all rows. Fact tables get
# a deterministic ``USING SAMPLE n ROWS (reservoir, SEED)`` cut. Smaller rows
# means faster vendor bulk-loads in CI without losing CFL coverage (the
# fact-trap is structural, not row-count dependent).
DIMENSION_TABLES = (
    "calendar",
    "countries",
    "regions",
    "clients",
    "products",
    "employees",
    "suppliers",
    "banks",
    "channels",
    "acctbal",
)

# Fact table → sampled row count.
FACT_SAMPLES = {
    "sales": 1000,
    "shipments": 950,
    "purchases": 300,
    "returns": 50,
    "clientcomplaints": 25,
}

SEED = 42


def main() -> None:
    if not SOURCE_DB.is_file():
        raise SystemExit(f"Source DuckDB not found: {SOURCE_DB}")

    con = duckdb.connect(str(SOURCE_DB), read_only=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for table in DIMENSION_TABLES:
        path = OUT_DIR / f"{table}.parquet"
        con.execute(f"COPY (SELECT * FROM orionbelt_1.{table}) TO '{path}' (FORMAT PARQUET)")
        n = con.execute(f"SELECT COUNT(*) FROM orionbelt_1.{table}").fetchone()[0]
        print(f"  {table:20s} {n:6d} rows (full)")

    for table, sample_n in FACT_SAMPLES.items():
        path = OUT_DIR / f"{table}.parquet"
        con.execute(
            f"COPY (SELECT * FROM orionbelt_1.{table} "
            f"USING SAMPLE {sample_n} ROWS (reservoir, {SEED})) "
            f"TO '{path}' (FORMAT PARQUET)"
        )
        print(f"  {table:20s} {sample_n:6d} rows (sampled, seed={SEED})")

    con.close()
    print(f"\nWrote parquet fixtures to {OUT_DIR}")


if __name__ == "__main__":
    main()

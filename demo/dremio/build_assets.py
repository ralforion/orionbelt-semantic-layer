#!/usr/bin/env python
"""Build the demo's two derived assets from the bundled commerce DuckDB seed.

1. Parquet export - every ``orionbelt_1`` table is written as a folder of
   Parquet under ``parquet/commerce/<table>/`` so a Dremio S3 (MinIO) source
   promotes each folder as a clean dataset named ``<table>`` (no ``.parquet``
   suffix in the path).

2. Dremio-dialect OBML model - the canonical ``examples/orionbelt_1_commerce.yaml``
   re-pointed at the Dremio S3 datasets: ``settings.defaultDialect: dremio`` and
   every ``dataObject`` addressed as ``"<source>"."<bucket>"."<table>"`` via
   ``database``/``schema``/``code``. Business names, dimensions, measures,
   metrics and joins are preserved verbatim - only the physical addressing and
   dialect change, which is the whole point of the comparison.

Re-run any time the seed or the canonical model changes:

    uv run python demo/dremio/build_assets.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
from ruamel.yaml import YAML

REPO_ROOT = Path(__file__).resolve().parents[2]
DUCKDB_SEED = REPO_ROOT / "examples" / "orionbelt_1_commerce.duckdb"
SOURCE_MODEL = REPO_ROOT / "examples" / "orionbelt_1_commerce.yaml"
DEMO_DIR = Path(__file__).resolve().parent
PARQUET_DIR = DEMO_DIR / "parquet"
MODEL_OUT = DEMO_DIR / "model" / "commerce_dremio.yaml"

# Dremio S3 source name + bucket the MinIO sidecar exposes. These three-part
# components become ``"<SOURCE>"."<BUCKET>"."<table>"`` in the compiled SQL.
DREMIO_SOURCE = "lake"
BUCKET = "commerce"


def export_parquet() -> list[str]:
    """Dump every seed table to ``parquet/<bucket>/<table>/data.parquet``."""

    con = duckdb.connect(str(DUCKDB_SEED), read_only=True)
    try:
        tables = [
            row[1]
            for row in con.execute(
                "SELECT schema_name, table_name FROM duckdb_tables() "
                "WHERE schema_name = 'orionbelt_1' ORDER BY table_name"
            ).fetchall()
        ]
        bucket_root = PARQUET_DIR / BUCKET
        if bucket_root.exists():
            shutil.rmtree(bucket_root)
        for table in tables:
            out_dir = bucket_root / table
            out_dir.mkdir(parents=True, exist_ok=True)
            target = (out_dir / "data.parquet").as_posix()
            con.execute(
                f"COPY (SELECT * FROM orionbelt_1.\"{table}\") TO '{target}' (FORMAT PARQUET)"
            )
            print(f"  parquet  {BUCKET}/{table}/data.parquet")
        return tables
    finally:
        con.close()


def build_model() -> None:
    """Re-point the canonical commerce model at the Dremio S3 datasets."""

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    data = yaml.load(SOURCE_MODEL.read_text())

    # Addressing name for single-model mode (Flight/pgwire ``database`` param).
    data["name"] = "commerce"

    settings = data.setdefault("settings", {})
    settings["defaultDialect"] = "dremio"
    # The lakehouse path is Dremio's own session TZ; don't force a TZ override
    # that the DuckDB seed needed - let Dremio resolve naive timestamps.
    settings.pop("overrideDatabaseTimezone", None)

    for name, obj in data.get("dataObjects", {}).items():
        # ``"<source>"."<bucket>"."<table>"`` - code stays the physical name,
        # which is also the promoted dataset (folder) name in MinIO.
        obj["database"] = DREMIO_SOURCE
        obj["schema"] = BUCKET
        # ``code`` already holds the physical table name; leave it untouched.
        print(f"  model    {name} -> {DREMIO_SOURCE}.{BUCKET}.{obj['code']}")

    MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
    with MODEL_OUT.open("w") as fh:
        yaml.dump(data, fh)


def main() -> None:
    print("Exporting Parquet from DuckDB seed...")
    tables = export_parquet()
    print(f"Exported {len(tables)} tables to {PARQUET_DIR / BUCKET}")
    print("Building Dremio-dialect OBML model...")
    build_model()
    print(f"Wrote {MODEL_OUT}")


if __name__ == "__main__":
    main()

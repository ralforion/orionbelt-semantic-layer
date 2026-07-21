#!/usr/bin/env python3
"""Execute every measure and metric of the demo model against the live stack.

Runs one query per measure and per metric through the OrionBelt pgwire
surface (``localhost:15432``), which compiles each to Dremio SQL and executes
it back against Dremio over Arrow Flight. This is the layer that a pure
compile / DuckDB test cannot cover: only a real Dremio execution catches
dialect-specific SQL that parses but the engine rejects (e.g. the quarter
period-over-period ``INTERVAL '-1' QUARTER`` regression).

Bring the demo up first (``demo/dremio/run-demo.sh``), then::

    uv run python demo/dremio/sweep_measures.py

Connection is overridable via env vars (defaults match the demo README):

    OBSL_PG_HOST=localhost OBSL_PG_PORT=15432 OBSL_PG_DB=orionbelt
    OBSL_PG_USER=obsl OBSL_PG_PASSWORD=""
    OBSL_MODEL=orionbelt.commerce.model   # fully-qualified FROM target

Exits non-zero if any measure or metric fails, so it doubles as a smoke gate.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import yaml

MODEL_YAML = Path(__file__).resolve().parent / "model" / "commerce_dremio.yaml"

CONN = (
    f"host={os.environ.get('OBSL_PG_HOST', 'localhost')} "
    f"port={os.environ.get('OBSL_PG_PORT', '15432')} "
    f"dbname={os.environ.get('OBSL_PG_DB', 'orionbelt')} "
    f"user={os.environ.get('OBSL_PG_USER', 'obsl')} "
    f"password={os.environ.get('OBSL_PG_PASSWORD', 'x')} "
    "sslmode=disable"
)
MODEL = os.environ.get("OBSL_MODEL", "orionbelt.commerce.model")


def _load() -> tuple[list[str], dict[str, dict]]:
    m = yaml.safe_load(MODEL_YAML.read_text())
    return list((m.get("measures") or {}).keys()), (m.get("metrics") or {})


def _time_dim(spec: dict) -> str | None:
    return spec.get("timeDimension") or (spec.get("periodOverPeriod") or {}).get("timeDimension")


def _sql(item: str, dims: list[str], target: str) -> str:
    cols = ", ".join(f'"{c}"' for c in [*dims, item])
    order = f' ORDER BY {", ".join(chr(34) + d + chr(34) for d in dims)}' if dims else ""
    return f"SELECT {cols} FROM {target}{order} LIMIT 50"


def _run(cur: psycopg.Cursor, sql: str) -> tuple[bool, str]:
    try:
        cur.execute(sql)
        rows = cur.fetchall()
        return True, f"{len(rows)} rows"
    except Exception as exc:  # noqa: BLE001 -- report, do not abort the sweep
        cur.connection.rollback()
        return False, str(exc).splitlines()[0][:120]


def _resolve_model_target(cur: psycopg.Cursor, probe_measure: str) -> str:
    """Pick the FROM identifier the pgwire catalog actually exposes."""
    candidates = [MODEL, "commerce.model", "model", "orionbelt.commerce.model"]
    seen: list[str] = []
    for target in candidates:
        if target in seen:
            continue
        seen.append(target)
        sql = f'SELECT "{probe_measure}" FROM {target} LIMIT 1'
        ok, _ = _run(cur, sql)
        if ok:
            return target
    raise SystemExit(
        f"Could not resolve the model FROM target (tried {seen}). "
        "Set OBSL_MODEL to match how you query it in DBeaver."
    )


def main() -> int:
    measures, metrics = _load()
    # (label, item, candidate-dimension-sets to try in order)
    plan: list[tuple[str, str, list[list[str]]]] = []
    for name in measures:
        plan.append(("measure", name, [["Year Month"], []]))
    for name, spec in metrics.items():
        td = _time_dim(spec)
        if td:
            plan.append(("metric", name, [[td]]))
        else:
            plan.append(("metric", name, [["Product Category"], []]))

    failures: list[tuple[str, str, str]] = []
    with psycopg.connect(CONN, connect_timeout=10) as conn:
        cur = conn.cursor()
        target = _resolve_model_target(cur, measures[0])
        print(f"Sweeping {len(measures)} measures + {len(metrics)} metrics against {target}\n")
        for kind, name, dim_options in plan:
            ok, detail, used = False, "", []
            for i, dims in enumerate(dim_options):
                ok, detail = _run(cur, _sql(name, dims, target))
                used = dims
                is_last = i == len(dim_options) - 1
                # A resolution error just means this dim is unreachable from the
                # item's fact -> try the next candidate. Any other error is real.
                unreachable = ("cannot be reached" in detail.lower()) or (
                    "resolution" in detail.lower()
                )
                if ok or is_last or not unreachable:
                    break
            mark = "PASS" if ok else "FAIL"
            dim_str = f"by {used[0]}" if used else "grand total"
            print(f"  [{mark}] {kind:7} {name:24} ({dim_str}): {detail}")
            if not ok:
                failures.append((kind, name, detail))

    print()
    if failures:
        print(f"=== {len(failures)} FAILURE(S) ===")
        for kind, name, detail in failures:
            print(f"  {kind} {name!r}: {detail}")
        return 1
    print(f"=== ALL {len(plan)} measures + metrics executed successfully on Dremio ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

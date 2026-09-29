"""Benchmark query compilation on a model and a directory of query files.

Measures warm compilation only: the model is loaded and every query parsed into
a ``QueryObject`` before timing starts, and no database is queried. Validation
stays on, as in production. Each query is warmed up, then compiled ``--reps``
times; the order of (query, variant) runs is shuffled so drift is shared.

``--ab`` compares the compiler with every in-compilation reuse against one
with ``--ab-off`` of them turned off (``CompilationPipeline(reuse_measures=...,
reuse_graphs=...)``), in one process with interleaved runs, after checking both
give equal results.
``--profile`` runs cProfile instead of timing; its cumulative times are not
comparable with timed runs, its call counts are.

Usage::

    uv run python scripts/bench_compile.py                        # TPC-DS, duckdb
    uv run python scripts/bench_compile.py --ab --json out.json   # A/B, raw samples
    uv run python scripts/bench_compile.py --ab --ab-off graphs   # one reuse only
    uv run python scripts/bench_compile.py --dialect snowflake --reps 50
    uv run python scripts/bench_compile.py --profile --top 25

Numbers are local microbenchmarks: compare variants within one run, never
across machines, and never read them as production latency.
"""

from __future__ import annotations

import argparse
import cProfile
import json
import platform
import pstats
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import sqlglot
import yaml

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import SemanticModel
from orionbelt.service.model_store import ModelStore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = ROOT / "examples" / "tpcds.obml.yml"
DEFAULT_QUERIES = ROOT / "examples" / "tpcds_queries"


def _load(model_path: Path, query_dir: Path) -> tuple[SemanticModel, dict[str, QueryObject]]:
    store = ModelStore()
    loaded = store.load_model(model_path.read_text(), dedup=False)
    model = store.get_model(loaded.model_id)
    queries = {
        path.stem: QueryObject.model_validate(yaml.safe_load(path.read_text()))
        for path in sorted(query_dir.glob("*.yml"))
    }
    if not queries:
        raise SystemExit(f"No *.yml queries in {query_dir}")
    return model, queries


def _commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def _summary(samples: list[float]) -> dict[str, float]:
    return {
        "mean_ms": statistics.fmean(samples),
        "median_ms": statistics.median(samples),
        "p95_ms": _p95(samples),
    }


def _check_parity(
    pipelines: dict[str, CompilationPipeline],
    queries: dict[str, QueryObject],
    model: SemanticModel,
    dialect: str,
) -> None:
    baseline, *others = pipelines
    for name, query in queries.items():
        expected = pipelines[baseline].compile(query, model, dialect)
        for variant in others:
            if pipelines[variant].compile(query, model, dialect) != expected:
                raise SystemExit(f"{name}: '{variant}' differs from '{baseline}'")


def _time(
    pipelines: dict[str, CompilationPipeline],
    queries: dict[str, QueryObject],
    model: SemanticModel,
    dialect: str,
    reps: int,
    warmup: int,
    seed: int,
) -> dict[str, dict[str, list[float]]]:
    for pipeline in pipelines.values():
        for query in queries.values():
            for _ in range(warmup):
                pipeline.compile(query, model, dialect)
    runs = [(variant, name) for variant in pipelines for name in queries for _ in range(reps)]
    random.Random(seed).shuffle(runs)
    samples: dict[str, dict[str, list[float]]] = {v: {n: [] for n in queries} for v in pipelines}
    for variant, name in runs:
        start = time.perf_counter()
        pipelines[variant].compile(queries[name], model, dialect)
        samples[variant][name].append((time.perf_counter() - start) * 1000)
    return samples


def _report(samples: dict[str, dict[str, list[float]]]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for variant, per_query in samples.items():
        pooled = [s for values in per_query.values() for s in values]
        report[variant] = {
            "workload": _summary(pooled),
            "queries": {name: _summary(values) for name, values in per_query.items()},
        }
    return report


def _print(report: dict[str, Any], queries: int) -> None:
    variants = list(report)
    header = f"{'':<10}" + "".join(f"{v:>14}" for v in variants)
    print(header)
    for stat in ("mean_ms", "median_ms", "p95_ms"):
        cells = "".join(f"{report[v]['workload'][stat]:>14.2f}" for v in variants)
        print(f"{stat:<10}{cells}")
    if len(variants) == 2:
        a, b = variants
        ratio = report[a]["workload"]["mean_ms"] / report[b]["workload"]["mean_ms"]
        print(f"\nmean {a}/{b}: {ratio:.2f}x over {queries} queries")
        slowest = sorted(
            report[a]["queries"].items(), key=lambda kv: kv[1]["median_ms"], reverse=True
        )[:5]
        print("\nslowest medians:")
        for name, stats in slowest:
            print(
                f"  {name:<8}{stats['median_ms']:>9.2f}"
                f"{report[b]['queries'][name]['median_ms']:>9.2f}"
            )


def _profile(
    pipeline: CompilationPipeline,
    queries: dict[str, QueryObject],
    model: SemanticModel,
    dialect: str,
    reps: int,
    top: int,
) -> None:
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(reps):
        for query in queries.values():
            pipeline.compile(query, model, dialect)
    profiler.disable()
    pstats.Stats(profiler).sort_stats("cumulative").print_stats(top)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--dialect", default="duckdb")
    parser.add_argument("--reps", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--ab", action="store_true", help="compare reuse on/off")
    parser.add_argument(
        "--ab-off",
        choices=["all", "measures", "graphs"],
        default="all",
        help="which reuse the --ab baseline turns off",
    )
    parser.add_argument("--profile", action="store_true", help="cProfile instead of timing")
    parser.add_argument("--top", type=int, default=30, help="rows printed by --profile")
    parser.add_argument("--json", type=Path, help="write metadata and raw samples here")
    args = parser.parse_args()

    load_start = time.perf_counter()
    model, queries = _load(args.model, args.queries)
    load_ms = (time.perf_counter() - load_start) * 1000

    if args.profile:
        _profile(CompilationPipeline(), queries, model, args.dialect, args.reps, args.top)
        return

    pipelines = {"reuse": CompilationPipeline()}
    if args.ab:
        baseline = CompilationPipeline(
            reuse_measures=args.ab_off not in ("all", "measures"),
            reuse_graphs=args.ab_off not in ("all", "graphs"),
        )
        pipelines = {"rebuild": baseline, **pipelines}
        _check_parity(pipelines, queries, model, args.dialect)
    samples = _time(pipelines, queries, model, args.dialect, args.reps, args.warmup, args.seed)
    report = _report(samples)
    print(
        f"{args.model.name}: {len(queries)} queries, {args.dialect}, {args.reps} reps, "
        f"model load {load_ms:.0f} ms (untimed below)\n"
    )
    _print(report, len(queries))

    if args.json:
        payload = {
            "meta": {
                "commit": _commit(),
                "python": sys.version.split()[0],
                "implementation": platform.python_implementation(),
                "sqlglot": sqlglot.__version__,
                "platform": platform.platform(),
                "machine": platform.machine(),
                "model": str(
                    args.model.relative_to(ROOT) if args.model.is_relative_to(ROOT) else args.model
                ),
                "queries": len(queries),
                "dialect": args.dialect,
                "reps": args.reps,
                "warmup": args.warmup,
                "seed": args.seed,
                "variants": list(pipelines),
                "model_load_ms": load_ms,
            },
            "summary": report,
            "samples_ms": samples,
        }
        args.json.write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()

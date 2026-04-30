"""Compile and run every OBML query in tpcds_queries/*.yml against ClickHouse.

Each *.yml file in the tpcds_queries/ directory is one OBML query (a single
QueryObject conforming to schema/query-schema.json). The label is taken from
the filename stem (e.g. Q3.yml → "Q3"); the optional second comment line of
the file is used as a human-readable description.

The catalogue mirrors a subset of the 99 TPC-DS benchmark queries — only
those expressible as a single grouped aggregation. The other ~79 need
correlated subqueries, CTEs, window functions, or facts/dims this focused
model does not expose.

Connection is read from env vars (with sensible defaults for a default
ClickHouse install)::

    CH_HOST     default: localhost
    CH_PORT     default: 8123 (HTTP)
    CH_USER     default: default
    CH_PASSWORD default: ""
    CH_DB       default: tpcds

Usage::

    uv run python examples/tpcds_run.py            # run all queries
    uv run python examples/tpcds_run.py Q3 Q42     # run only the named ones
    uv run python examples/tpcds_run.py --dry      # compile only, do not execute
    uv run python examples/tpcds_run.py --compare  # compare numeric totals to reference CSVs

When --compare is set, each ``Qnn`` query is matched to ``qnn.csv`` in
``REF_RESULTS_DIR`` (default ``../clickhouse-tpcds-uss/results-tpcds`` relative
to this repo). Reference CSVs are pipe-delimited, no header. We compare
row count and the sum of every numeric column — exact match modulo floating
point tolerance. Reference files often include extra columns (window
calculations, item descriptions) that this model doesn't surface; we
align by tail-of-numeric-columns, not column index.
"""

from __future__ import annotations

import os
import re
import sys
import time
import traceback
from pathlib import Path

import yaml

import orionbelt.dialect  # noqa: F401  — registers all dialects
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

MODEL_PATH = Path(__file__).parent / "tpcds.obml.yml"
QUERIES_DIR = Path(__file__).parent / "tpcds_queries"
DEFAULT_REF_DIR = Path(
    os.environ.get(
        "REF_RESULTS_DIR",
        Path(__file__).resolve().parents[2] / "clickhouse-tpcds-uss" / "results-tpcds",
    )
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

# A "label — description" header line, e.g.: "# Q3 — Brand sales by year ..."
_HEADER_RE = re.compile(r"^#\s*[A-Za-z0-9_\-]+\s*[—\-:]\s*(.+?)\s*$")


def _extract_description(text: str) -> str:
    """Pull the first non-schema header comment from a query YAML file."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or "yaml-language-server" in stripped:
            continue
        m = _HEADER_RE.match(stripped)
        if m:
            return m.group(1)
        # First real (non-comment) line — give up.
        if not stripped.startswith("#"):
            return ""
    return ""


def _natural_key(name: str) -> tuple[int, int | str]:
    """Sort 'Q3' < 'Q19' < 'Q98' < 'DEMO-Margin'."""
    if name.startswith("Q") and name[1:].isdigit():
        return (0, int(name[1:]))
    return (1, name)


def load_queries() -> dict[str, tuple[str, QueryObject]]:
    """Load every *.yml file in QUERIES_DIR into {label: (description, query)}."""
    if not QUERIES_DIR.is_dir():
        raise SystemExit(f"queries directory not found: {QUERIES_DIR}")

    files = sorted(QUERIES_DIR.glob("*.yml"), key=lambda p: _natural_key(p.stem))
    if not files:
        raise SystemExit(f"no *.yml query files found in {QUERIES_DIR}")

    catalogue: dict[str, tuple[str, QueryObject]] = {}
    for path in files:
        text = path.read_text()
        label = path.stem
        description = _extract_description(text) or label
        spec = yaml.safe_load(text)
        if not isinstance(spec, dict):
            raise SystemExit(f"{path}: expected a YAML mapping at top level")
        catalogue[label] = (description, QueryObject.model_validate(spec))
    return catalogue


def load_model():
    yaml_text = MODEL_PATH.read_text()
    raw, src = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, src)
    if not result.valid:
        for e in result.errors:
            print(f"  [{e.code}] {e.message}", file=sys.stderr)
        raise SystemExit("Model failed to validate")
    return model


# ---------------------------------------------------------------------------
# Reference-CSV comparison
# ---------------------------------------------------------------------------


def _label_to_ref_path(label: str, ref_dir: Path) -> Path | None:
    """Resolve ``Q3`` → ``q03.csv`` (or None if no canonical mapping)."""
    if not (label.startswith("Q") and label[1:].isdigit()):
        return None
    n = int(label[1:])
    candidate = ref_dir / f"q{n:02d}.csv"
    return candidate if candidate.exists() else None


def _to_float(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _column_numeric_sum(column_values: list[str]) -> float | None:
    """Sum a column if it parses as numeric across all non-empty rows; else None."""
    total = 0.0
    parsed = 0
    for v in column_values:
        if v == "" or v is None:
            continue
        f = _to_float(v)
        if f is None:
            return None
        total += f
        parsed += 1
    return total if parsed > 0 else None


def compare_to_reference(
    label: str,
    columns: list[str],
    rows: list[tuple],
    ref_dir: Path,
) -> str:
    """Compare query result against reference CSV. Returns a human note."""
    ref_path = _label_to_ref_path(label, ref_dir)
    if ref_path is None:
        return "no reference CSV"

    # Reference files are pipe-delimited, no header.
    ref_rows: list[list[str]] = []
    for line in ref_path.read_text().splitlines():
        if not line.strip():
            continue
        ref_rows.append(line.split("|"))
    if not ref_rows:
        return f"reference {ref_path.name} is empty"

    n_ref = len(ref_rows)
    n_got = len(rows)
    n_ref_cols = len(ref_rows[0])
    n_got_cols = len(columns)

    # Row count: exact match preferred. Reference often has 100 rows (LIMIT 100);
    # we also limit to 100 — any mismatch is a real divergence.
    row_match = "✓" if n_ref == n_got else f"rows {n_got} vs ref {n_ref}"

    # Numeric-column totals: align by tail (skip leading non-numeric ID columns).
    ref_numeric_sums: list[float] = []
    for ci in range(n_ref_cols):
        col = [r[ci] if ci < len(r) else "" for r in ref_rows]
        s = _column_numeric_sum(col)
        if s is not None:
            ref_numeric_sums.append(s)

    got_numeric_sums: list[float] = []
    for ci in range(n_got_cols):
        col = [str(r[ci]) if ci < len(r) and r[ci] is not None else "" for r in rows]
        s = _column_numeric_sum(col)
        if s is not None:
            got_numeric_sums.append(s)

    # Align by tail: compare the last min(n) numeric columns. Many reference
    # files have a leading numeric ID column (year, brand_id, manager_id) that
    # we project as a dimension — those sums match by happy accident, but the
    # measure totals are what we really care about. Tail alignment keeps the
    # comparison stable when the reference includes extra computed columns.
    n_compare = min(len(ref_numeric_sums), len(got_numeric_sums))
    if n_compare == 0:
        return f"{row_match} | no numeric columns to compare"

    ref_tail = ref_numeric_sums[-n_compare:]
    got_tail = got_numeric_sums[-n_compare:]
    diffs: list[str] = []
    for i, (rs, gs) in enumerate(zip(ref_tail, got_tail)):
        if rs == 0 and gs == 0:
            continue
        denom = max(abs(rs), abs(gs), 1.0)
        rel = abs(rs - gs) / denom
        if rel > 0.005:  # 0.5% tolerance
            diffs.append(f"col[-{n_compare - i}]: got {gs:,.2f} vs ref {rs:,.2f} ({rel:.1%})")

    sums_match = "✓ totals" if not diffs else f"✗ totals: {'; '.join(diffs)}"
    return f"{row_match} | {sums_match}"


# ---------------------------------------------------------------------------


def get_clickhouse_client():
    """Return a clickhouse-connect client configured from CH_* env vars."""
    try:
        import clickhouse_connect  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "clickhouse-connect is not installed. Run:  uv add --optional drivers clickhouse-connect"
        ) from exc

    return clickhouse_connect.get_client(
        host=os.environ.get("CH_HOST", "localhost"),
        port=int(os.environ.get("CH_PORT", "8123")),
        username=os.environ.get("CH_USER", "default"),
        password=os.environ.get("CH_PASSWORD", ""),
        database=os.environ.get("CH_DB", "tpcds"),
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def run_one(
    label: str,
    description: str,
    query: QueryObject,
    model,
    client,
    dry: bool,
    compare: bool = False,
    ref_dir: Path = DEFAULT_REF_DIR,
) -> tuple[bool, bool, str]:
    """Compile and (optionally) execute one query.

    Returns (compile_ok, execute_ok, note). When dry=True, execute_ok is True
    iff compile succeeded.
    """
    print("\n" + "=" * 88)
    print(f"[{label}] {description}")
    print("=" * 88)

    try:
        result = CompilationPipeline().compile(query, model, dialect_name="clickhouse")
    except Exception as exc:
        print(f"\n!! compile failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return False, False, f"compile: {type(exc).__name__}: {exc}"

    print("\n--- compiled SQL ------------------------------------------------------------\n")
    print(result.sql)

    if dry:
        return True, True, "compiled (dry)"

    print("\n--- execution ---------------------------------------------------------------\n")
    started = time.perf_counter()
    try:
        rows = client.query(result.sql)
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"!! execute failed after {elapsed_ms:.1f} ms: {type(exc).__name__}: {exc}")
        return True, False, f"execute: {type(exc).__name__}: {exc}"
    elapsed_ms = (time.perf_counter() - started) * 1000

    columns = rows.column_names
    data = rows.result_rows
    print(f"{len(data)} rows in {elapsed_ms:.1f} ms")

    # Compact preview — pad columns and print the first 25 rows.
    if data:
        widths = [
            max(len(str(c)), max((len(str(r[i])) for r in data[:25]), default=0))
            for i, c in enumerate(columns)
        ]
        fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
        print(fmt.format(*columns))
        print(fmt.format(*("-" * w for w in widths)))
        for row in data[:25]:
            print(fmt.format(*[str(v) for v in row]))
        if len(data) > 25:
            print(f"... ({len(data) - 25} more rows)")

    note = f"{len(data)} rows in {elapsed_ms:.1f} ms"
    if compare:
        cmp_note = compare_to_reference(label, list(columns), list(data), ref_dir)
        print(f"\n--- compare to reference ----------------------------------------------------\n")
        print(cmp_note)
        note = f"{note}  [{cmp_note}]"
    return True, True, note


def main(argv: list[str]) -> None:
    dry = "--dry" in argv
    compare = "--compare" in argv
    args = [a for a in argv if not a.startswith("--")]

    queries = load_queries()
    selected = args if args else list(queries.keys())
    unknown = [a for a in selected if a not in queries]
    if unknown:
        raise SystemExit(f"Unknown query labels: {unknown}. Available: {sorted(queries)}")

    model = load_model()
    client = None if dry else get_clickhouse_client()
    if compare and not DEFAULT_REF_DIR.is_dir():
        print(
            f"!! --compare requested but reference dir not found: {DEFAULT_REF_DIR}",
            file=sys.stderr,
        )
        compare = False

    results: list[tuple[str, bool, bool, str]] = []
    for label in selected:
        description, query = queries[label]
        compile_ok, execute_ok, note = run_one(
            label, description, query, model, client, dry, compare=compare
        )
        results.append((label, compile_ok, execute_ok, note))

    # ── Summary ─────────────────────────────────────────────────────
    print("\n\n" + "#" * 88)
    print(f"# SUMMARY ({len(results)} queries)")
    print("#" * 88)
    label_w = max(len(r[0]) for r in results)
    for label, compile_ok, execute_ok, note in results:
        if not compile_ok:
            status = "COMPILE FAIL"
        elif dry:
            status = "COMPILED   "
        elif not execute_ok:
            status = "EXEC FAIL  "
        else:
            status = "OK         "
        print(f"  {label:<{label_w}}  {status}  {note}")

    n_compile_ok = sum(1 for _, c, _, _ in results if c)
    if dry:
        print(f"\n  compiled: {n_compile_ok}/{len(results)}")
    else:
        n_exec_ok = sum(1 for _, _, e, _ in results if e)
        print(f"\n  compiled: {n_compile_ok}/{len(results)}    executed: {n_exec_ok}/{len(results)}")


if __name__ == "__main__":
    main(sys.argv[1:])

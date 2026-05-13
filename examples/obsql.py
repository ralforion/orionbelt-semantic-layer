"""``obsql`` — tiny Arrow Flight SQL CLI for OBSL.

A minimal command-line client for running OBSQL against an OBSL Flight
SQL server. Useful for smoke testing, demos, and CI assertions without
pulling in a full BI tool. Naming follows the ``psql``/``snowsql``
convention — the CLI shares the name with the language it runs.

Usage examples:

    # Run a query against the auto-resolved model (single-model mode)
    uv run python examples/obsql.py 'SELECT version()'

    # Pick a model in multi-model mode
    uv run python examples/obsql.py --model sales 'SHOW TABLES'

    # List the loaded models (via REST /v1/models)
    uv run python examples/obsql.py --list --rest-port 9003

    # OBSQL aggregate
    uv run python examples/obsql.py -m sales \\
        'SELECT "Region Name", "Total Sales" FROM sales LIMIT 5'

    # Hierarchical subtotals
    uv run python examples/obsql.py -m sales \\
        'SELECT "Region", "Total Sales" FROM sales WITH ROLLUP'

    # Verify governance rejections
    uv run python examples/obsql.py -m sales 'DROP TABLE foo'
    # → WRITE_OPERATION_REJECTED

    uv run python examples/obsql.py -m sales 'SELECT * FROM nonexistent'
    # → RAW_SQL_REJECTED

Exit codes:
    0 — query succeeded
    1 — server-side rejection (RAW_SQL_REJECTED, UNKNOWN_MODEL, ...)
    2 — client-side error (connection refused, bad args)
"""

from __future__ import annotations

import argparse
import sys

import pyarrow.flight as flight


def _make_options(model: str | None) -> flight.FlightCallOptions:
    """Build per-call options carrying the model selector header.

    The Arrow Flight SQL JDBC driver uses ``database`` for the same slot
    (Connection.setCatalog()); programmatic clients can set it via gRPC
    metadata directly. OBSL accepts ``database`` / ``x-obsl-model`` /
    ``catalog`` (in that order) — we send ``database`` to match what BI
    tools send.
    """
    headers: list[tuple[bytes, bytes]] = []
    if model:
        headers.append((b"database", model.encode("utf-8")))
    return flight.FlightCallOptions(headers=headers)


def _execute(client: flight.FlightClient, sql: str, model: str | None) -> int:
    """Execute one OBSQL statement and print the result to stdout."""
    options = _make_options(model)
    try:
        info = client.get_flight_info(
            flight.FlightDescriptor.for_command(sql.encode("utf-8")),
            options=options,
        )
        reader = client.do_get(info.endpoints[0].ticket, options=options)
        table = reader.read_all()
    except flight.FlightServerError as exc:
        # Server-side rejection — print the OBSL error code + message
        print(f"FlightServerError: {exc}", file=sys.stderr)
        return 1
    except flight.FlightUnavailableError as exc:
        print(f"FlightUnavailableError: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    df = table.to_pandas()
    if len(df) == 0:
        print("(empty result)")
        return 0
    print(df.to_string(index=False))
    return 0


def _list_models(rest_host: str, rest_port: int) -> int:
    """Hit ``GET /v1/models`` to print the admin-curated catalog."""
    import json
    import urllib.request

    url = f"http://{rest_host}:{rest_port}/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except Exception as exc:
        print(f"Failed to fetch {url}: {exc}", file=sys.stderr)
        return 2

    models = data.get("models", [])
    if not models:
        print("(no admin-pre-loaded models — dynamic mode)")
        return 0
    print(f"{'NAME':<20} {'DIMS':>5} {'MEAS':>5} {'METR':>5} DESCRIPTION")
    print("-" * 72)
    for m in models:
        print(
            f"{m['name']:<20} {m['dimensions']:>5} {m['measures']:>5} "
            f"{m['metrics']:>5} {m.get('description') or ''}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="obsql",
        description=(
            "Tiny Arrow Flight SQL CLI for OrionBelt Semantic Layer (OBSL). "
            "Run OBSQL statements without a full BI tool."
        ),
    )
    parser.add_argument(
        "--host",
        default="localhost",
        help="Flight server host (default: localhost)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8815,
        help="Flight server port (default: 8815)",
    )
    parser.add_argument(
        "-m",
        "--model",
        help=(
            "Model selector — set as the gRPC `database` header so the server "
            "routes to the named model in multi-model mode. Omit for single-"
            "model mode or auto-resolve."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List admin-pre-loaded models via REST /v1/models, then exit.",
    )
    parser.add_argument(
        "--rest-host",
        default="localhost",
        help="REST host for --list (default: localhost)",
    )
    parser.add_argument(
        "--rest-port",
        type=int,
        default=8000,
        help="REST port for --list (default: 8000)",
    )
    parser.add_argument(
        "sql",
        nargs="*",
        help="OBSQL statement(s) to execute. Multiple positional args are joined.",
    )
    args = parser.parse_args(argv)

    if args.list:
        return _list_models(args.rest_host, args.rest_port)

    if not args.sql:
        parser.error("provide a SQL statement to execute, or use --list")

    sql = " ".join(args.sql)
    client = flight.FlightClient(f"grpc://{args.host}:{args.port}")
    return _execute(client, sql, args.model)


if __name__ == "__main__":
    sys.exit(main())

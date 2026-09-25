"""``obsl`` — command-line interface for the OrionBelt Semantic Layer.

Run ``obsl --help`` for the command list. Heavy compiler / service imports are
deferred into each command body so ``--help`` and ``--version`` stay fast.
"""

from __future__ import annotations

import dataclasses
import enum
import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from orionbelt import __version__
from orionbelt.cli import _io, _render
from orionbelt.cli._render import OutputFormat

app = typer.Typer(
    name="obsl",
    help=(
        "OrionBelt Semantic Layer CLI. Compile, validate, execute and convert "
        "OBML semantic models locally, or against a deployed server with --server."
    ),
    no_args_is_help=True,
    add_completion=True,
)


# --------------------------------------------------------------------------
# Shared option types
# --------------------------------------------------------------------------

ModelArg = Annotated[
    str, typer.Argument(metavar="MODEL", help="Path to an OBML model YAML file (or '-' for stdin).")
]
# compile / execute can run against a deployed model with --server, where the
# local file is not needed — so MODEL is optional there.
ModelArgOpt = Annotated[
    str | None,
    typer.Argument(
        metavar="[MODEL]",
        help="Path to an OBML model YAML file. Required locally; omit with --server "
        "to query the server's curated model.",
    ),
]
QueryOpt = Annotated[
    str | None,
    typer.Option("--query", "-q", help="Path to a query document (JSON or YAML; '-' for stdin)."),
]
SqlOpt = Annotated[
    str | None,
    typer.Option(
        "--sql",
        help='OrionBelt Semantic QL string, e.g. \'SELECT "Dim", "Measure" FROM model LIMIT 5\'.',
    ),
]
DialectOpt = Annotated[
    str | None,
    typer.Option(
        "--dialect", "-d", help="Target SQL dialect (defaults to the model's, then DB_VENDOR)."
    ),
]
FormatOpt = Annotated[
    OutputFormat,
    typer.Option("--format", "-f", help="Output format for tabular results."),
]
ServerOpt = Annotated[
    str | None,
    typer.Option(
        "--server",
        "-s",
        envvar="OBSL_SERVER",
        help="Run against a deployed OrionBelt REST API (e.g. https://host) instead of locally.",
    ),
]
ApiKeyOpt = Annotated[
    str | None,
    typer.Option("--api-key", envvar="OBSL_API_KEY", help="API key for the remote server."),
]
#: Client certificate material for ``--server``. Per-command rather than
#: global, so it sits beside ``--server`` and ``--api-key`` where a reader
#: expects it: a global flag would have to precede the subcommand while its two
#: companions follow it.
ClientCertOpt = Annotated[
    str | None,
    typer.Option(
        "--client-cert",
        envvar="OBSL_CLIENT_CERT",
        help=(
            "PEM client certificate for --server, when an ingress in front of the REST "
            "API requires one (mutual TLS). Holds the key too unless --client-key is given."
        ),
    ),
]
ClientKeyOpt = Annotated[
    str | None,
    typer.Option(
        "--client-key",
        envvar="OBSL_CLIENT_KEY",
        help="PEM private key for --client-cert, when the two are separate files.",
    ),
]
CaCertOpt = Annotated[
    str | None,
    typer.Option(
        "--ca-cert",
        envvar="OBSL_CA_CERT",
        help=(
            "PEM CA bundle used to verify the --server certificate, for a private or "
            "self-signed authority. Replaces the default trust store; it never disables "
            "verification."
        ),
    ),
]


def _remote_client(
    server: str,
    api_key: str | None,
    client_cert: str | None,
    client_key: str | None,
    ca_cert: str | None,
) -> Any:
    """Build a ``RemoteClient``, failing on bad certificate paths first.

    Every failure names the setting it is about, for the same reason the
    listener loaders do: a missing or unreadable path otherwise surfaces as a
    TLS handshake error mentioning none of them.
    """
    from orionbelt.cli._local import CliError
    from orionbelt.cli._remote import RemoteClient, resolve_tls

    try:
        tls = resolve_tls(client_cert, client_key, ca_cert)
    except CliError as exc:
        raise _fail(str(exc)) from None
    return RemoteClient(server, api_key, tls=tls)


class ConvertDirection(enum.StrEnum):
    """Direction for the ``convert`` command."""

    osi_to_obml = "osi-to-obml"
    obml_to_osi = "obml-to-osi"


def _version_callback(value: bool) -> None:
    if value:
        _render.raw(f"obsl (orionbelt-semantic-layer) {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """OrionBelt Semantic Layer CLI."""


# --------------------------------------------------------------------------
# Error handling
# --------------------------------------------------------------------------


def _fail(message: str) -> typer.Exit:
    _render.error(message)
    return typer.Exit(1)


def _require_one_query_input(query: str | None, sql: str | None) -> None:
    """Ensure exactly one of --query / --sql was given."""
    if bool(query) == bool(sql):
        raise _fail(
            "Provide exactly one of --query/-q (a query document) or --sql (an OBSQL string)."
        )


def _emit_warnings(warnings: list[Any]) -> None:
    """Print a list of warnings (strings or dicts) to stderr."""
    for w in warnings:
        if isinstance(w, dict):
            msg = w.get("message") or w.get("msg") or str(w)
            _render.warn(str(msg))
        else:
            _render.warn(str(w))


def _remote_input_schema_warnings(data: dict[str, Any], label: str) -> list[str]:
    """Input-schema issues from a convert response, as warning strings.

    The REST convert endpoints surface input schema violations under
    ``input_validation.schema_errors`` rather than ``warnings``. The local
    conversion paths fold the same issues into their warnings, so mirror that
    for ``--server`` users to keep both paths symmetric (see #225).
    """
    iv = data.get("input_validation") or {}
    return [f"{label} input schema: {msg}" for msg in (iv.get("schema_errors") or [])]


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


OnlineOpt = Annotated[
    bool,
    typer.Option(
        "--online",
        help=(
            "Also check the model against the datasource: probe every data object "
            "for its table, declared columns and column types."
        ),
    ),
]
#: Separate from ``DialectOpt`` because it selects a different thing. That one
#: picks the SQL to generate and falls back to the model's ``defaultDialect``;
#: this one picks the connection the probe opens, and the only connection a
#: deployment has is ``DB_VENDOR``.
ProbeDialectOpt = Annotated[
    str | None,
    typer.Option(
        "--dialect",
        "-d",
        help="Datasource to probe with --online (defaults to DB_VENDOR).",
    ),
]


@app.command()
def validate(
    model: ModelArg,
    fmt: FormatOpt = OutputFormat.table,
    online: OnlineOpt = False,
    dialect: ProbeDialectOpt = None,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """Validate an OBML model. Exits non-zero when the model is invalid.

    Offline by default: the model is checked against itself and the OBML
    schema, and no connection is opened. ``--online`` adds a datasource probe
    whose findings are errors like any other, so a dropped table or a renamed
    column fails the command.
    """
    model_yaml = _io.read_text(model)
    if server:
        from orionbelt.cli._local import CliError

        try:
            data = _remote_client(server, api_key, client_cert, client_key, ca_cert).validate(
                model_yaml, online=online, dialect=dialect
            )
        except CliError as exc:
            raise _fail(str(exc)) from None
        valid = bool(data.get("valid"))
        errors = data.get("errors") or []
        warnings = data.get("warnings") or []
    else:
        from orionbelt.cli import _local

        summary = _local.validate(model_yaml, online=online, dialect=dialect)
        valid = summary.valid
        errors = [dataclasses.asdict(e) for e in summary.errors]
        warnings = [dataclasses.asdict(w) for w in summary.warnings]

    if fmt is OutputFormat.json:
        _render.emit_json({"valid": valid, "errors": errors, "warnings": warnings})
    else:
        for w in warnings:
            _render.warn(_fmt_issue(w))
        if valid:
            _render.note("model is valid")
        else:
            _render.error("model is invalid:")
            for e in errors:
                _render.error("  " + _fmt_issue(e))
    if not valid:
        raise typer.Exit(1)


def _fmt_issue(issue: dict[str, Any]) -> str:
    code = issue.get("code", "")
    msg = issue.get("message", "")
    path = issue.get("path")
    head = f"[{code}] {msg}" if code else str(msg)
    return f"{head} ({path})" if path else head


@app.command()
def compile(  # noqa: A001 — "compile" is the natural verb for this command
    query: QueryOpt = None,
    sql: SqlOpt = None,
    model: ModelArgOpt = None,
    dialect: DialectOpt = None,
    explain: Annotated[
        bool, typer.Option("--explain", help="Also print the planner decisions.")
    ] = False,
    pretty: Annotated[
        bool, typer.Option("--pretty/--no-pretty", help="Pretty-print the SQL.")
    ] = True,
    fmt: FormatOpt = OutputFormat.table,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """Compile a query to SQL from a query document (-q) or an OBSQL string (--sql).

    Locally, MODEL is required. With --server the query runs against the
    server's curated model, so MODEL may be omitted.
    """
    _require_one_query_input(query, sql)
    q = _io.load_query(query) if query else None

    from orionbelt.cli._local import CliError

    payload: dict[str, Any]
    if server and not model:
        # Remote only when no local model is given: a provided MODEL is
        # authoritative (compiled locally), so an ambient OBSL_SERVER never
        # silently redirects an explicit `obsl compile model.yaml`.

        client = _remote_client(server, api_key, client_cert, client_key, ca_cert)
        try:
            if sql:
                item = client.compile_obsql(sql, dialect)
            else:
                assert q is not None  # guaranteed by _require_one_query_input
                item = client.compile(q, dialect)
        except CliError as exc:
            raise _fail(str(exc)) from None
        payload = {
            "sql": item.get("sql"),
            "dialect": item.get("dialect"),
            "sql_valid": item.get("sql_valid"),
            "warnings": item.get("warnings") or [],
            "physical_tables": item.get("physical_tables") or [],
            "explain": item.get("explain"),
        }
    else:
        from orionbelt.cli import _local
        from orionbelt.service.model_store import ModelValidationError

        if not model:
            raise _fail(
                "MODEL is required for local compile "
                "(or omit MODEL with --server to query the deployed model)."
            )
        if server:
            _render.note("MODEL provided: compiling locally; --server ignored.")
        model_yaml = _io.read_text(model)
        try:
            if sql:
                result = _local.compile_obsql(model_yaml, sql, dialect, pretty=pretty)
            else:
                assert q is not None  # guaranteed by _require_one_query_input
                result = _local.compile_query(model_yaml, q, dialect, pretty=pretty)
        except ModelValidationError as exc:
            raise _model_invalid(exc) from None
        except CliError as exc:
            raise _fail(str(exc)) from None
        payload = {
            "sql": result.sql,
            "dialect": result.dialect,
            "sql_valid": result.sql_valid,
            "warnings": [w.message for w in result.warnings],
            "physical_tables": list(result.physical_tables),
            "explain": dataclasses.asdict(result.explain) if result.explain else None,
        }

    if fmt is OutputFormat.json:
        _render.emit_json(payload)
        return

    _emit_warnings(payload["warnings"])
    if explain and payload["explain"]:
        _print_explain(payload["explain"])
    _render.raw(payload["sql"] or "")


def _print_explain(plan: dict[str, Any]) -> None:
    """Print a compact planner summary to stderr."""
    _render.note(f"planner: {plan.get('planner')} - {plan.get('planner_reason')}")
    _render.note(f"base object: {plan.get('base_object')} - {plan.get('base_object_reason')}")
    for j in plan.get("joins") or []:
        cols = ", ".join(j.get("join_columns") or [])
        _render.note(f"  join {j.get('from_object')} -> {j.get('to_object')} on {cols}")
    for leg in plan.get("cfl_legs") or []:
        _render.note(f"  CFL leg: source={leg.get('measure_source')} root={leg.get('common_root')}")


@app.command()
def execute(
    query: QueryOpt = None,
    sql: SqlOpt = None,
    model: ModelArgOpt = None,
    dialect: DialectOpt = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="Default row limit when the query has none (default 1000). "
            "Applies to -q queries and local --sql; not to remote --sql (put LIMIT in the SQL).",
        ),
    ] = None,
    fmt: FormatOpt = OutputFormat.table,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """Execute a query (from -q or --sql) against the configured warehouse.

    Locally, MODEL is required and a database must be configured. With
    --server the query runs against the server's curated model and warehouse.
    """
    _require_one_query_input(query, sql)
    effective_limit = 1000 if limit is None else limit
    q = _io.load_query(query) if query else None
    if q is not None and q.limit is None:
        q = q.model_copy(update={"limit": effective_limit})

    from orionbelt.cli._local import CliError

    if server and not model:
        if sql and limit is not None:
            _render.warn(
                "--limit cannot be applied to --sql in remote mode; include LIMIT in the "
                "query (the server applies its own default row limit otherwise)."
            )
        client = _remote_client(server, api_key, client_cert, client_key, ca_cert)
        try:
            if sql:
                item = client.execute_obsql(sql, dialect)
            else:
                assert q is not None  # guaranteed by _require_one_query_input
                item = client.execute(q, dialect)
        except CliError as exc:
            raise _fail(str(exc)) from None
        columns = [c.get("name", "") for c in (item.get("columns") or [])]
        rows = item.get("rows") or []
        meta = {
            "row_count": item.get("row_count"),
            "execution_time_ms": item.get("execution_time_ms"),
            "dialect": item.get("dialect"),
        }
        warnings = item.get("warnings") or []
    else:
        from orionbelt.cli import _local
        from orionbelt.service.db_executor import ExecutionError, ExecutionUnavailableError
        from orionbelt.service.model_store import ModelValidationError

        if not model:
            raise _fail(
                "MODEL is required for local execute "
                "(or omit MODEL with --server to run against the deployed model)."
            )
        if server:
            _render.note("MODEL provided: executing locally; --server ignored.")
        model_yaml = _io.read_text(model)
        try:
            if sql:
                compiled, executed = _local.execute_obsql(
                    model_yaml, sql, dialect, limit=effective_limit
                )
            else:
                assert q is not None  # guaranteed by _require_one_query_input
                compiled, executed = _local.execute_query(
                    model_yaml, q, dialect, limit=effective_limit
                )
        except ModelValidationError as exc:
            raise _model_invalid(exc) from None
        except (ExecutionUnavailableError, ExecutionError, CliError) as exc:
            raise _fail(str(exc)) from None
        columns = [c.name for c in executed.columns]
        rows = executed.rows
        meta = {
            "row_count": executed.row_count,
            "execution_time_ms": executed.execution_time_ms,
            "dialect": compiled.dialect,
        }
        warnings = [w.message for w in compiled.warnings]

    if fmt is OutputFormat.json:
        _render.emit_json({"columns": columns, "rows": rows, **meta})
        return
    _emit_warnings(warnings)
    _render.emit_table(columns, rows, fmt)
    _render.note(f"{meta['row_count']} rows in {meta['execution_time_ms']} ms ({meta['dialect']})")


@app.command()
def describe(
    model: ModelArg,
    fmt: FormatOpt = OutputFormat.table,
) -> None:
    """Show a structured overview of a model's data objects and artefacts."""
    model_yaml = _io.read_text(model)
    from orionbelt.cli import _local
    from orionbelt.service.model_store import ModelValidationError

    try:
        desc = _local.describe(model_yaml)
    except ModelValidationError as exc:
        raise _model_invalid(exc) from None

    if fmt is OutputFormat.json:
        _render.emit_json(dataclasses.asdict(desc))
        return

    _render.emit_table(
        ["data object", "table", "columns", "joins"],
        [[o.label, o.code, len(o.columns), ", ".join(o.join_targets)] for o in desc.data_objects],
        fmt,
        title="Data objects",
    )
    _render.emit_table(
        ["dimension", "type", "data object", "column"],
        [[d.name, d.result_type, d.data_object, d.column] for d in desc.dimensions],
        fmt,
        title="Dimensions",
    )
    _render.emit_table(
        ["measure", "type", "aggregation"],
        [[m.name, m.result_type, m.aggregation] for m in desc.measures],
        fmt,
        title="Measures",
    )
    if desc.metrics:
        _render.emit_table(
            ["metric", "type", "measure"],
            [[m.name, m.type, m.measure or ""] for m in desc.metrics],
            fmt,
            title="Metrics",
        )


def _local_model(model: str | None, server: str | None, action: str) -> str | None:
    """The model YAML to use locally, or ``None`` to run against ``--server``.

    Same rule as ``compile`` / ``execute``: a given MODEL is authoritative, so an
    ambient ``OBSL_SERVER`` never redirects an explicit local run.
    """
    if server and not model:
        return None
    if not model:
        raise _fail(
            f"MODEL is required to {action} locally "
            "(or omit MODEL with --server to use the deployed model)."
        )
    if server:
        _render.note("MODEL provided: running locally; --server ignored.")
    return _io.read_text(model)


def _guarded[T](fn: Callable[[], T]) -> T:
    """Run a local or remote call, turning its expected failures into a clean exit."""
    from orionbelt.cli._local import CliError
    from orionbelt.service.db_executor import ExecutionError, ExecutionUnavailableError
    from orionbelt.service.model_store import ModelValidationError

    try:
        return fn()
    except ModelValidationError as exc:
        raise _model_invalid(exc) from None
    except (CliError, ExecutionError, ExecutionUnavailableError) as exc:
        raise _fail(str(exc)) from None


OutputOpt = Annotated[
    str | None,
    typer.Option("--output", "-o", help="Write to this file instead of standard output."),
]


def _emit_text(text: str, output: str | None) -> None:
    """Print *text*, or write it to *output* and say so on stderr."""
    if output is None:
        _render.raw(text)
        return
    _io.write_text(output, text if text.endswith("\n") else text + "\n")
    _render.note(f"wrote {output}")


@app.command()
def diagram(
    model: ModelArgOpt = None,
    columns: Annotated[
        bool, typer.Option("--columns/--no-columns", help="Show columns in entities.")
    ] = True,
    theme: Annotated[str, typer.Option("--theme", help="Mermaid theme.")] = "default",
    markdown: Annotated[
        bool,
        typer.Option(
            "--markdown",
            "--md",
            help="Wrap the diagram in a ```mermaid Markdown fence. Implied by an -o path "
            "ending in .md.",
        ),
    ] = False,
    output: OutputOpt = None,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """Render the model as a Mermaid ER diagram.

    Locally, MODEL is required. With --server the diagram is downloaded from the
    server's curated model.
    """
    model_yaml = _local_model(model, server, "render the diagram")
    if model_yaml is None:
        client = _remote_client(str(server), api_key, client_cert, client_key, ca_cert)
        mermaid = _guarded(lambda: client.diagram(show_columns=columns, theme=theme))
    else:
        from orionbelt.cli import _local

        mermaid = _guarded(lambda: _local.diagram(model_yaml, show_columns=columns, theme=theme))
    if markdown or (output or "").lower().endswith(".md"):
        mermaid = f"```mermaid\n{mermaid.rstrip()}\n```"
    _emit_text(mermaid, output)


@app.command()
def graph(
    model: ModelArgOpt = None,
    output: OutputOpt = None,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """Render the model's OBSL-Core RDF graph (its ontology export) as Turtle.

    Locally, MODEL is required. With --server the graph is downloaded from the
    server's curated model.
    """
    model_yaml = _local_model(model, server, "render the graph")
    if model_yaml is None:
        client = _remote_client(str(server), api_key, client_cert, client_key, ca_cert)
        turtle = _guarded(client.graph)
    else:
        from orionbelt.cli import _local

        turtle = _guarded(lambda: _local.graph(model_yaml))
    _emit_text(turtle, output)


class LineageOutput(enum.StrEnum):
    """Output format for the ``lineage`` command."""

    mermaid = "mermaid"
    markdown = "markdown"
    json = "json"
    turtle = "turtle"


#: The format an ``-o`` path implies when ``-f`` is not given.
_LINEAGE_SUFFIXES = {
    ".md": LineageOutput.markdown,
    ".json": LineageOutput.json,
    ".ttl": LineageOutput.turtle,
    ".mmd": LineageOutput.mermaid,
}


@app.command()
def lineage(
    model: ModelArgOpt = None,
    dimension: Annotated[
        str | None, typer.Option("--dimension", help="Lineage of this dimension.")
    ] = None,
    measure: Annotated[
        str | None, typer.Option("--measure", help="Lineage of this measure.")
    ] = None,
    metric: Annotated[str | None, typer.Option("--metric", help="Lineage of this metric.")] = None,
    rule: Annotated[str | None, typer.Option("--rule", "-r", help="Lineage of this rule.")] = None,
    query: QueryOpt = None,
    sql: SqlOpt = None,
    dialect: DialectOpt = None,
    fmt: Annotated[
        LineageOutput | None,
        typer.Option(
            "--format",
            "-f",
            help="mermaid (default), markdown (a ```mermaid fence), json, or turtle "
            "(OBSL graph IRIs linked by prov:wasDerivedFrom).",
        ),
    ] = None,
    output: OutputOpt = None,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """Show what a dimension, measure, metric, rule or query is built from.

    Give exactly one of --dimension, --measure, --metric, --rule, -q (a query
    document) or --sql (OBSQL). The graph follows references down to the tables,
    and a query's lineage includes the joins the planner chose. Without -f, an
    -o path ending in .md, .json or .ttl picks the format.
    """
    targets = [
        (kind, value)
        for kind, value in (
            ("dimension", dimension),
            ("measure", measure),
            ("metric", metric),
            ("rule", rule),
            ("query", query or sql),
        )
        if value
    ]
    if len(targets) != 1 or (query and sql):
        raise _fail("Give exactly one of --dimension, --measure, --metric, --rule, -q or --sql.")
    kind, name = targets[0]
    suffix = Path(output).suffix.lower() if output else ""
    chosen = fmt or _LINEAGE_SUFFIXES.get(suffix, LineageOutput.mermaid)
    q = _io.load_query(query) if query else None

    model_yaml = _local_model(model, server, "trace lineage")
    if model_yaml is None:
        if sql:
            raise _fail("--sql needs a local MODEL. With --server, pass the query with -q.")
        client = _remote_client(str(server), api_key, client_cert, client_key, ca_cert)
        api_format = "mermaid" if chosen is LineageOutput.markdown else chosen.value
        data = _guarded(lambda: client.lineage(kind, name, api_format, query=q, dialect=dialect))
        text = (
            json.dumps(data, indent=2, ensure_ascii=False)
            if chosen is LineageOutput.json
            else str(data)
        )
    else:
        from orionbelt.cli import _local
        from orionbelt.service.lineage import to_turtle

        graph, model_id = _guarded(
            lambda: _local.lineage(
                model_yaml,
                kind,
                name if kind != "query" else None,
                query=q,
                sql=sql,
                dialect=dialect,
            )
        )
        if chosen is LineageOutput.json:
            payload = {**dataclasses.asdict(graph), "mermaid": graph.to_mermaid()}
            text = json.dumps(payload, indent=2, ensure_ascii=False)
        elif chosen is LineageOutput.turtle:
            text = to_turtle(graph, model_id)
        else:
            text = graph.to_mermaid()
    if chosen is LineageOutput.markdown:
        text = f"```mermaid\n{text.rstrip()}\n```"
    _emit_text(text, output)


@app.command()
def convert(
    direction: Annotated[ConvertDirection, typer.Argument(help="Conversion direction.")],
    input_file: Annotated[
        str, typer.Argument(metavar="INPUT", help="Input YAML file (or '-' for stdin).")
    ],
    model_name: Annotated[
        str, typer.Option("--name", help="OSI model name (obml-to-osi only).")
    ] = "semantic_model",
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """Convert between OSI and OBML model formats."""
    input_yaml = _io.read_text(input_file)
    from orionbelt.cli._local import CliError

    warnings: list[Any]
    if direction is ConvertDirection.osi_to_obml:
        if server:
            try:
                data = _remote_client(
                    server, api_key, client_cert, client_key, ca_cert
                ).convert_osi_to_obml(input_yaml)
            except CliError as exc:
                raise _fail(str(exc)) from None
            output = data.get("output_yaml", "")
            warnings = _remote_input_schema_warnings(data, "OSI") + (data.get("warnings") or [])
        else:
            import yaml

            from orionbelt.cli import _local

            try:
                result, warnings, _ = _local.convert_osi_to_obml(input_yaml)
            except CliError as exc:
                raise _fail(str(exc)) from None
            output = yaml.dump(result, sort_keys=False, allow_unicode=True, width=120)
        _emit_warnings(warnings)
        _render.raw(output)
        return

    # obml-to-osi
    if server:
        try:
            data = _remote_client(
                server, api_key, client_cert, client_key, ca_cert
            ).convert_obml_to_osi(input_yaml, model_name=model_name)
        except CliError as exc:
            raise _fail(str(exc)) from None
        output = data.get("output_yaml", "")
        warnings = _remote_input_schema_warnings(data, "OBML") + (data.get("warnings") or [])
    else:
        import yaml

        from orionbelt.cli import _local

        try:
            result, warnings, _ = _local.convert_obml_to_osi(input_yaml, model_name=model_name)
        except CliError as exc:
            raise _fail(str(exc)) from None
        output = yaml.dump(result, sort_keys=False, allow_unicode=True, width=120)
    _emit_warnings(warnings)
    _render.raw(output)


@app.command()
def dialects(
    fmt: FormatOpt = OutputFormat.table,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """List the supported SQL dialects."""
    if server:
        from orionbelt.cli._local import CliError

        try:
            names = _remote_client(server, api_key, client_cert, client_key, ca_cert).dialects()
        except CliError as exc:
            raise _fail(str(exc)) from None
    else:
        from orionbelt.cli import _local

        names = _local.list_dialects()
    if fmt is OutputFormat.json:
        _render.emit_json(names)
    else:
        _render.emit_table(["dialect"], [[n] for n in names], fmt)


SparqlFileOpt = Annotated[
    str | None,
    typer.Option("--query", "-q", help="Path to a SPARQL query file ('-' for stdin)."),
]
SparqlTextOpt = Annotated[
    str | None,
    typer.Option("--sparql", help="SPARQL query string, e.g. 'SELECT ?m WHERE { ... }'."),
]


@app.command()
def sparql(
    model: ModelArgOpt = None,
    query: SparqlFileOpt = None,
    text: SparqlTextOpt = None,
    fmt: FormatOpt = OutputFormat.table,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """Run a read-only SPARQL query (SELECT or ASK) against the model's RDF graph.

    The graph is the OBSL-Core graph that `obsl graph` prints. Locally, MODEL is
    required. With --server the query runs against the server's curated model.
    """
    if bool(query) == bool(text):
        raise _fail("Provide exactly one of --query/-q (a SPARQL file) or --sparql (a string).")
    sparql_text = _io.read_text(query) if query else str(text)
    model_yaml = _local_model(model, server, "query the graph")
    if model_yaml is None:
        client = _remote_client(str(server), api_key, client_cert, client_key, ca_cert)
        data: dict[str, Any] = _guarded(lambda: client.sparql(sparql_text))
    else:
        from orionbelt.cli import _local

        result = _guarded(lambda: _local.sparql(model_yaml, sparql_text))
        data = {
            "type": result.type,
            "variables": result.variables,
            "results": result.results,
            "boolean": result.boolean,
        }

    if fmt is OutputFormat.json:
        _render.emit_json(data)
    elif data.get("type") == "ask":
        _render.raw("true" if data.get("boolean") else "false")
    else:
        variables = list(data.get("variables") or [])
        results = data.get("results") or []
        _render.emit_table(variables, [[r.get(v) for v in variables] for r in results], fmt)
        _render.note(f"{len(results)} results")


# --------------------------------------------------------------------------
# Business rules
# --------------------------------------------------------------------------

rules_app = typer.Typer(
    name="rules",
    help=(
        "List, compile and evaluate a model's business rules. Each rule compiles to "
        "a query whose rows are its findings."
    ),
    no_args_is_help=True,
)
app.add_typer(rules_app)

RuleNameOpt = Annotated[
    list[str] | None,
    typer.Option("--rule", "-r", help="Rule name; repeat for several. Default: every rule."),
]


def _rules(
    model: str | None,
    server: str | None,
    remote: Callable[[Any], tuple[str, list[Any]]],
    local: Callable[[str], tuple[str, list[Any]]],
    tls: tuple[str | None, str | None, str | None, str | None],
    action: str,
) -> tuple[str, list[Any]]:
    """Run a rules operation locally or against ``--server``."""
    model_yaml = _local_model(model, server, action)
    if model_yaml is None:
        client = _remote_client(str(server), *tls)
        return _guarded(lambda: remote(client))
    return _guarded(lambda: local(model_yaml))


def _exit_on_failed(outcomes: list[Any]) -> None:
    failed = [o.name for o in outcomes if o.status == "failed"]
    if failed:
        _render.error(f"{len(failed)} rule(s) failed: {', '.join(failed)}")
        raise typer.Exit(1)


@rules_app.command("list")
def rules_list(
    model: ModelArgOpt = None,
    dialect: DialectOpt = None,
    fmt: FormatOpt = OutputFormat.table,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """List the business rules and whether each rule's query compiles."""
    from orionbelt.cli import _local

    used_dialect, outcomes = _rules(
        model,
        server,
        lambda client: client.list_rules(dialect),
        lambda yaml_text: _local.run_rules(yaml_text, dialect),
        (api_key, client_cert, client_key, ca_cert),
        "list rules",
    )
    if fmt is OutputFormat.json:
        rules = [
            {
                "name": o.name,
                "type": o.type,
                "level": o.level,
                "severity": o.severity,
                "findings": o.findings,
                "compiles": o.status != "failed",
                "error": o.error,
            }
            for o in outcomes
        ]
        _render.emit_json({"dialect": used_dialect, "rules": rules})
        return
    _render.emit_table(
        ["rule", "type", "level", "severity", "findings", "compiles", "error"],
        [
            [
                o.name,
                o.type,
                o.level,
                o.severity or "",
                o.findings,
                "no" if o.status == "failed" else "yes",
                o.error or "",
            ]
            for o in outcomes
        ],
        fmt,
    )
    _render.note(f"{len(outcomes)} rules ({used_dialect})")


@rules_app.command("compile")
def rules_compile(
    model: ModelArgOpt = None,
    rule: RuleNameOpt = None,
    dialect: DialectOpt = None,
    fmt: FormatOpt = OutputFormat.table,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """Print the SQL behind each rule. Exits non-zero when a rule fails to compile."""
    from orionbelt.cli import _local

    used_dialect, outcomes = _rules(
        model,
        server,
        lambda client: client.compile_rules(dialect, rule),
        lambda yaml_text: _local.run_rules(yaml_text, dialect, names=rule),
        (api_key, client_cert, client_key, ca_cert),
        "compile rules",
    )
    if fmt is OutputFormat.json:
        keys = ("name", "status", "level", "findings", "sql", "error")
        _render.emit_json(
            {
                "dialect": used_dialect,
                "rules": [{k: getattr(o, k) for k in keys} for o in outcomes],
            }
        )
    else:
        for o in outcomes:
            if o.status == "failed":
                _render.error(f"{o.name}: {o.error}")
            else:
                _render.raw(f"-- Rule: {o.name} ({o.findings})\n{o.sql}\n")
    _exit_on_failed(outcomes)


@rules_app.command("evaluate")
def rules_evaluate(
    model: ModelArgOpt = None,
    rule: RuleNameOpt = None,
    rule_type: Annotated[
        list[str] | None,
        typer.Option(
            "--type",
            help="Only rules of this type (classification, eligibility, validation, "
            "constraint); repeat for several.",
        ),
    ] = None,
    severity: Annotated[
        list[str] | None,
        typer.Option(
            "--severity", help="Only rules of this severity (info, warning, error); repeatable."
        ),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=1000, help="Findings fetched per rule."),
    ] = 20,
    dialect: DialectOpt = None,
    fmt: FormatOpt = OutputFormat.table,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
    client_cert: ClientCertOpt = None,
    client_key: ClientKeyOpt = None,
    ca_cert: CaCertOpt = None,
) -> None:
    """Run rules against the warehouse and report their findings.

    With a single --rule, prints that rule's findings. Otherwise prints one summary
    row per rule; a count shown as "20+" reached --limit and may be higher.
    Exits non-zero when a rule fails to compile or run.
    """
    from orionbelt.cli import _local

    used_dialect, outcomes = _rules(
        model,
        server,
        lambda client: client.evaluate_rules(
            dialect, names=rule, types=rule_type, severities=severity, limit=limit
        ),
        lambda yaml_text: _local.run_rules(
            yaml_text,
            dialect,
            names=rule,
            types=rule_type,
            severities=severity,
            execute=True,
            limit=limit,
        ),
        (api_key, client_cert, client_key, ca_cert),
        "evaluate rules",
    )
    if fmt is OutputFormat.json:
        _render.emit_json(
            {"dialect": used_dialect, "rules": [dataclasses.asdict(o) for o in outcomes]}
        )
    elif rule and len(rule) == 1 and len(outcomes) == 1 and outcomes[0].status == "executed":
        only = outcomes[0]
        _render.emit_table(only.columns, only.rows, fmt)
        _render.note(f"{only.name}: {_count(only.finding_count, limit)} findings ({only.findings})")
    else:
        _render.emit_table(
            ["rule", "type", "severity", "status", "findings", "error"],
            [
                [
                    o.name,
                    o.type,
                    o.severity or "",
                    o.status,
                    _count(o.finding_count, limit),
                    o.error or "",
                ]
                for o in outcomes
            ],
            fmt,
        )
        with_findings = sum(1 for o in outcomes if o.finding_count)
        _render.note(f"{len(outcomes)} rules, {with_findings} with findings ({used_dialect})")
    _exit_on_failed(outcomes)


def _count(finding_count: int | None, limit: int) -> str:
    """A finding count, marked when it hit the fetch limit."""
    if finding_count is None:
        return ""
    return f"{finding_count}+" if finding_count >= limit else str(finding_count)


def _model_invalid(exc: Any) -> typer.Exit:
    """Render a ModelValidationError's structured errors and return Exit(1)."""
    _render.error("model validation failed:")
    for e in exc.errors:
        path = f" ({e.path})" if getattr(e, "path", None) else ""
        _render.error(f"  [{e.code}] {e.message}{path}")
    return typer.Exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()

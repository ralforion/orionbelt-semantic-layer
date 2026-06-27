"""``obsl`` — command-line interface for the OrionBelt Semantic Layer.

Run ``obsl --help`` for the command list. Heavy compiler / service imports are
deferred into each command body so ``--help`` and ``--version`` stay fast.
"""

from __future__ import annotations

import dataclasses
import enum
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
QueryOpt = Annotated[
    str,
    typer.Option("--query", "-q", help="Path to a query document (JSON or YAML; '-' for stdin)."),
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


def _emit_warnings(warnings: list[Any]) -> None:
    """Print a list of warnings (strings or dicts) to stderr."""
    for w in warnings:
        if isinstance(w, dict):
            msg = w.get("message") or w.get("msg") or str(w)
            _render.warn(str(msg))
        else:
            _render.warn(str(w))


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


@app.command()
def validate(
    model: ModelArg,
    fmt: FormatOpt = OutputFormat.table,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
) -> None:
    """Validate an OBML model. Exits non-zero when the model is invalid."""
    model_yaml = _io.read_text(model)
    if server:
        from orionbelt.cli._local import CliError
        from orionbelt.cli._remote import RemoteClient

        try:
            data = RemoteClient(server, api_key).validate(model_yaml)
        except CliError as exc:
            raise _fail(str(exc)) from None
        valid = bool(data.get("valid"))
        errors = data.get("errors") or []
        warnings = data.get("warnings") or []
    else:
        from orionbelt.cli import _local

        summary = _local.validate(model_yaml)
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
    model: ModelArg,
    query: QueryOpt,
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
) -> None:
    """Compile a query against a model to SQL."""
    model_yaml = _io.read_text(model)
    q = _io.load_query(query)

    from orionbelt.cli._local import CliError

    payload: dict[str, Any]
    if server:
        from orionbelt.cli._remote import RemoteClient

        try:
            item = RemoteClient(server, api_key).compile(model_yaml, q, dialect)
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

        try:
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
    model: ModelArg,
    query: QueryOpt,
    dialect: DialectOpt = None,
    limit: Annotated[
        int, typer.Option("--limit", help="Default row limit when the query has none.")
    ] = 1000,
    fmt: FormatOpt = OutputFormat.table,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
) -> None:
    """Compile and execute a query against the configured warehouse."""
    model_yaml = _io.read_text(model)
    q = _io.load_query(query)

    from orionbelt.cli._local import CliError

    if server:
        from orionbelt.cli._remote import RemoteClient

        if q.limit is None:
            q = q.model_copy(update={"limit": limit})
        try:
            item = RemoteClient(server, api_key).execute(model_yaml, q, dialect)
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

        try:
            compiled, executed = _local.execute_query(model_yaml, q, dialect, limit=limit)
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


@app.command()
def diagram(
    model: ModelArg,
    columns: Annotated[
        bool, typer.Option("--columns/--no-columns", help="Show columns in entities.")
    ] = True,
    theme: Annotated[str, typer.Option("--theme", help="Mermaid theme.")] = "default",
) -> None:
    """Render the model as a Mermaid ER diagram."""
    model_yaml = _io.read_text(model)
    from orionbelt.cli import _local
    from orionbelt.service.model_store import ModelValidationError

    try:
        _render.raw(_local.diagram(model_yaml, show_columns=columns, theme=theme))
    except ModelValidationError as exc:
        raise _model_invalid(exc) from None


@app.command()
def graph(model: ModelArg) -> None:
    """Render the model's OBSL-Core RDF graph as Turtle."""
    model_yaml = _io.read_text(model)
    from orionbelt.cli import _local
    from orionbelt.service.model_store import ModelValidationError

    try:
        _render.raw(_local.graph(model_yaml))
    except ModelValidationError as exc:
        raise _model_invalid(exc) from None


@app.command()
def convert(
    direction: Annotated[ConvertDirection, typer.Argument(help="Conversion direction.")],
    input_file: Annotated[
        str, typer.Argument(metavar="INPUT", help="Input YAML file (or '-' for stdin).")
    ],
    ontology: Annotated[
        bool, typer.Option("--ontology", help="Also emit the OSI ontology (obml-to-osi only).")
    ] = False,
    model_name: Annotated[
        str, typer.Option("--name", help="OSI model name (obml-to-osi only).")
    ] = "semantic_model",
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
) -> None:
    """Convert between OSI and OBML model formats."""
    input_yaml = _io.read_text(input_file)
    from orionbelt.cli._local import CliError

    warnings: list[Any]
    if direction is ConvertDirection.osi_to_obml:
        if server:
            from orionbelt.cli._remote import RemoteClient

            try:
                data = RemoteClient(server, api_key).convert_osi_to_obml(input_yaml)
            except CliError as exc:
                raise _fail(str(exc)) from None
            output, warnings = data.get("output_yaml", ""), data.get("warnings") or []
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
        from orionbelt.cli._remote import RemoteClient

        try:
            data = RemoteClient(server, api_key).convert_obml_to_osi(
                input_yaml, model_name=model_name, include_ontology=ontology
            )
        except CliError as exc:
            raise _fail(str(exc)) from None
        output, warnings = data.get("output_yaml", ""), data.get("warnings") or []
        onto_yaml = data.get("ontology_yaml")
    else:
        import yaml

        from orionbelt.cli import _local

        try:
            result, warnings, _, onto = _local.convert_obml_to_osi(
                input_yaml, model_name=model_name, include_ontology=ontology
            )
        except CliError as exc:
            raise _fail(str(exc)) from None
        output = yaml.dump(result, sort_keys=False, allow_unicode=True, width=120)
        onto_yaml = (
            yaml.dump(onto, sort_keys=False, allow_unicode=True, width=120) if onto else None
        )
    _emit_warnings(warnings)
    _render.raw(output)
    if ontology and onto_yaml:
        _render.note("--- ontology ---")
        _render.raw(onto_yaml)


@app.command()
def dialects(
    fmt: FormatOpt = OutputFormat.table,
    server: ServerOpt = None,
    api_key: ApiKeyOpt = None,
) -> None:
    """List the supported SQL dialects."""
    if server:
        from orionbelt.cli._local import CliError
        from orionbelt.cli._remote import RemoteClient

        try:
            names = RemoteClient(server, api_key).dialects()
        except CliError as exc:
            raise _fail(str(exc)) from None
    else:
        from orionbelt.cli import _local

        names = _local.list_dialects()
    if fmt is OutputFormat.json:
        _render.emit_json(names)
    else:
        _render.emit_table(["dialect"], [[n] for n in names], fmt)


def _model_invalid(exc: Any) -> typer.Exit:
    """Render a ModelValidationError's structured errors and return Exit(1)."""
    _render.error("model validation failed:")
    for e in exc.errors:
        path = f" ({e.path})" if getattr(e, "path", None) else ""
        _render.error(f"  [{e.code}] {e.message}{path}")
    return typer.Exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()

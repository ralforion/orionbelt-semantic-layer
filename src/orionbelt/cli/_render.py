"""Output rendering for the ``obsl`` CLI.

Two streams are used deliberately: data goes to stdout (so ``obsl ... | jq``
and redirects work), while human-facing notes, warnings and errors go to
stderr. ``--format json`` always emits machine-readable JSON on stdout.
"""

from __future__ import annotations

import csv
import enum
import io
import json
from collections.abc import Sequence
from typing import Any

from rich.console import Console
from rich.table import Table

# stdout = data; stderr = chatter. Keep them on separate consoles so piping
# stdout never captures a progress note or a warning.
_out = Console()
_err = Console(stderr=True)


class OutputFormat(enum.StrEnum):
    """Rendering style for tabular command output."""

    table = "table"
    json = "json"
    csv = "csv"
    tsv = "tsv"


def note(message: str) -> None:
    """Print a dimmed informational note to stderr."""
    _err.print(f"[dim]{message}[/dim]")


def warn(message: str) -> None:
    """Print a warning to stderr."""
    _err.print(f"[yellow]warning:[/yellow] {message}")


def error(message: str) -> None:
    """Print an error to stderr."""
    _err.print(f"[red]error:[/red] {message}")


def raw(text: str) -> None:
    """Emit text verbatim to stdout (SQL, YAML, Turtle, Mermaid).

    Uses the plain ``print`` builtin rather than the rich console so syntax
    characters are never interpreted as markup and output is byte-faithful.
    """
    print(text)


def emit_json(data: Any) -> None:
    """Serialize ``data`` as pretty JSON to stdout."""
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def emit_table(
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    fmt: OutputFormat,
    *,
    title: str | None = None,
) -> None:
    """Render a column/row result in the requested tabular format."""
    if fmt is OutputFormat.json:
        emit_json({"columns": list(columns), "rows": [list(r) for r in rows]})
        return
    if fmt in (OutputFormat.csv, OutputFormat.tsv):
        delimiter = "," if fmt is OutputFormat.csv else "\t"
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
        writer.writerow(list(columns))
        for r in rows:
            writer.writerow(["" if c is None else c for c in r])
        print(buf.getvalue(), end="")
        return

    table = Table(title=title, show_lines=False, header_style="bold cyan")
    for col in columns:
        table.add_column(str(col))
    for r in rows:
        table.add_row(*["" if c is None else str(c) for c in r])
    _out.print(table)

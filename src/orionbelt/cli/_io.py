"""Input helpers for the ``obsl`` CLI: reading model / query / OSI files."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer
import yaml

from orionbelt.models.query import QueryObject

# Sentinel a user passes as the path to read from standard input instead.
STDIN_SENTINEL = "-"


def read_text(path: str) -> str:
    """Read a UTF-8 text file, or standard input when ``path`` is ``"-"``.

    Raises ``typer.BadParameter`` with a clear message when the file is
    missing or unreadable, so the CLI exits cleanly rather than dumping a
    traceback.
    """
    if path == STDIN_SENTINEL:
        return sys.stdin.read()
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise typer.BadParameter(f"File not found: {path}") from None
    except OSError as exc:
        raise typer.BadParameter(f"Could not read {path}: {exc}") from None


def load_query(path: str) -> QueryObject:
    """Load a query document (JSON or YAML) into a :class:`QueryObject`.

    YAML is a superset of JSON, so a single ``yaml.safe_load`` accepts both
    ``.json`` and ``.yaml`` query files (and stdin via ``"-"``). Field names
    may be snake_case or camelCase — ``QueryObject`` accepts either.
    """
    raw = read_text(path)
    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise typer.BadParameter(f"Invalid query document: {exc}") from None
    if not isinstance(data, dict):
        raise typer.BadParameter("Query document must be a mapping (object) with a 'select' key")
    try:
        return QueryObject.model_validate(data)
    except Exception as exc:  # noqa: BLE001 — surface pydantic errors as a clean CLI message
        raise typer.BadParameter(f"Invalid query: {exc}") from None

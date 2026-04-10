"""Read-only SPARQL query execution over OBSL graphs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rdflib import Graph

_FORBIDDEN = re.compile(
    r"\b(INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|COPY|MOVE|ADD)\b",
    re.IGNORECASE,
)


class SPARQLUpdateError(ValueError):
    """Raised when a SPARQL update operation is attempted."""


@dataclass
class SPARQLResult:
    """Result of a read-only SPARQL query."""

    type: str  # "select", "ask", "construct"
    variables: list[str] = field(default_factory=list)
    results: list[dict[str, str | None]] = field(default_factory=list)
    boolean: bool | None = None


def execute_sparql(graph: Graph, query: str) -> SPARQLResult:
    """Execute a read-only SPARQL query against an in-memory RDF graph.

    Parameters
    ----------
    graph:
        rdflib Graph to query.
    query:
        SPARQL query string.  Only ``SELECT`` and ``ASK`` are supported.
        Update operations (``INSERT``, ``DELETE``, …) are rejected.

    Returns
    -------
    SPARQLResult
        Query results with type indicator, variable names, and row data.

    Raises
    ------
    SPARQLUpdateError
        If the query contains an update keyword.
    ValueError
        If the query is syntactically invalid or uses an unsupported form.
    """
    if _FORBIDDEN.search(query):
        raise SPARQLUpdateError("SPARQL update operations are not allowed")

    result = graph.query(query)
    result_any: Any = result

    if result_any.type == "ASK":
        return SPARQLResult(type="ask", boolean=bool(result_any.askAnswer))

    if result_any.type == "CONSTRUCT":
        raise ValueError("Only SELECT and ASK queries are supported; CONSTRUCT is not allowed")

    if result_any.type not in ("SELECT",):
        raise ValueError(f"Only SELECT and ASK queries are supported; got {result_any.type}")

    # SELECT query
    variables: list[str] = [str(v) for v in (result_any.vars or [])]
    rows: list[dict[str, str | None]] = []
    for row in result_any:
        row_dict: dict[str, str | None] = {}
        for i, var in enumerate(variables):
            val = row[i]
            row_dict[var] = str(val) if val is not None else None
        rows.append(row_dict)

    return SPARQLResult(type="select", variables=variables, results=rows)

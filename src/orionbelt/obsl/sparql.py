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
    warnings: list[str] = field(default_factory=list)


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

    warnings = unbound_variable_warnings(query)
    result = graph.query(query)
    result_any: Any = result

    if result_any.type == "ASK":
        return SPARQLResult(type="ask", boolean=bool(result_any.askAnswer), warnings=warnings)

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

    return SPARQLResult(type="select", variables=variables, results=rows, warnings=warnings)


def unbound_variable_warnings(query: str) -> list[str]:
    """Warn about variables that are ordered by or projected but never bound.

    ``ORDER BY ?lal`` for a query that binds ``?label`` is valid SPARQL: an
    unbound variable compares equal everywhere, so the engine orders nothing
    and says nothing. It is nearly always a typo, so it is reported here as
    a warning rather than left silent. A variable counts as bound when it
    appears in a triple pattern, a ``BIND``, or a ``VALUES`` block anywhere
    in the query. Returns nothing for a query rdflib cannot parse; the
    executor raises for that on its own.
    """
    from rdflib.plugins.sparql import prepareQuery
    from rdflib.plugins.sparql.parserutils import CompValue
    from rdflib.term import Variable

    try:
        algebra = prepareQuery(query).algebra
    except Exception:  # noqa: BLE001 - parse errors surface from graph.query
        return []

    bound: set[str] = set()
    ordered: list[str] = []
    projected: list[str] = []

    def variables_in(value: Any) -> set[str]:
        if isinstance(value, Variable):
            return {str(value)}
        if isinstance(value, CompValue):
            return set().union(*(variables_in(v) for v in value.values()))
        if isinstance(value, (list, tuple, set)):
            return set().union(*(variables_in(v) for v in value))
        if isinstance(value, dict):
            # A VALUES row is {Variable: term}: the variables are the keys.
            return set().union(*(variables_in(v) for kv in value.items() for v in kv))
        return set()

    def walk(node: Any) -> None:
        if isinstance(node, CompValue):
            if node.name == "BGP":
                bound.update(variables_in(node.get("triples")))
            elif node.name == "Extend":
                bound.update(variables_in(node.get("var")))
            elif node.name in ("values", "ToMultiSet"):
                bound.update(variables_in(node.get("res")))
            elif node.name == "OrderCondition":
                ordered.extend(sorted(variables_in(node.get("expr"))))
            elif node.name == "Project":
                projected.extend(str(v) for v in node.get("PV") or [])
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(algebra)
    warnings: list[str] = []
    seen: set[str] = set()
    for name in ordered:
        if name not in bound and name not in seen:
            seen.add(name)
            warnings.append(f"ORDER BY ?{name}: the variable is never bound, so it orders nothing")
    for name in projected:
        if name not in bound and name not in seen:
            seen.add(name)
            warnings.append(f"SELECT ?{name}: the variable is never bound, so the column is empty")
    return warnings

"""OBSL — OrionBelt Semantic Layer RDF vocabulary (Core 0.1)."""

from __future__ import annotations

from orionbelt.obsl.exporter import export_obsl
from orionbelt.obsl.sparql import SPARQLResult, SPARQLUpdateError, execute_sparql

__all__ = ["export_obsl", "execute_sparql", "SPARQLResult", "SPARQLUpdateError"]

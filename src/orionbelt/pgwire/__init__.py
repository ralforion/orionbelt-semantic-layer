"""Postgres v3 wire protocol surface for OBSL.

See design/PLAN_postgres_wire.md. Step 1 ships the connection handshake
and a hardcoded ``SELECT 1`` simple-query responder; later steps wire
the semantic-SQL router, catalog emulation, and the extended protocol.
"""

from __future__ import annotations

from orionbelt.pgwire.server import PgWireServer

__all__ = ["PgWireServer"]

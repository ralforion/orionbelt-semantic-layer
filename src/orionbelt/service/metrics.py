"""Prometheus metrics, served when ``METRICS_ENABLED`` is on.

Off by default. On, ``/metrics`` answers on ``METRICS_HOST``:``METRICS_PORT``
(``0.0.0.0:9090``): a port of its own, not the API's, so the metrics stay off
a public API unless that port is published.

What they report:

- **The DuckDB worker pools** (:mod:`orionbelt.service.duckdb_pool`), read at
  each scrape: per source and worker, whether the worker takes queries, its
  connections by state, the statements started on it, the queries run again
  because it did not start them, and the statements the pool ended. A worker
  is labelled by the address its source lists, ``host:port``.
- **The process**: CPU, memory and open files where the system reports them,
  the Python version, and garbage collection.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

_SERVER: Any = None


class PoolCollector:
    """The DuckDB worker pools' state and counters, read at each scrape."""

    def describe(self) -> list[Any]:
        # Nothing to declare up front: the families depend on the pools there are.
        return []

    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

        from orionbelt.service import duckdb_pool

        up = GaugeMetricFamily(
            "obsl_duckdb_worker_up",
            "1 while the worker takes queries; 0 while the pool keeps it out (health check, "
            "unreachable, shutting down, or a statement unanswered past its deadline)",
            labels=["source", "worker"],
        )
        connections = GaugeMetricFamily(
            "obsl_duckdb_worker_connections",
            "Connections to the worker: in_use (held by a query), idle (kept for the next), "
            "opening (a connect the pool gave up waiting for that has not come back)",
            labels=["source", "worker", "state"],
        )
        statements = CounterMetricFamily(
            "obsl_duckdb_worker_statements",
            "Statements started on the worker",
            labels=["source", "worker"],
        )
        retries = CounterMetricFamily(
            "obsl_duckdb_worker_retries",
            "Queries run again because the worker did not start them, by reason: unreachable, "
            "connect_timeout, session_not_found, rate_limited, server_draining",
            labels=["source", "worker", "reason"],
        )
        ended = CounterMetricFamily(
            "obsl_duckdb_worker_statements_ended",
            "Statements the pool ended, by reason: deadline (unanswered past its timeout) or "
            "unresponsive (the worker stopped answering its health checks)",
            labels=["source", "worker", "reason"],
        )
        for source, workers in sorted(duckdb_pool.snapshots().items()):
            for worker in workers:
                labels = [source, worker["worker"]]
                up.add_metric(labels, 1.0 if worker["healthy"] else 0.0)
                for state in ("in_use", "idle", "opening"):
                    connections.add_metric([*labels, state], float(worker[state]))
                statements.add_metric(labels, float(worker["statements"]))
                for reason, count in sorted(worker["retries"].items()):
                    retries.add_metric([*labels, reason], float(count))
                for reason, count in sorted(worker["ended"].items()):
                    ended.add_metric([*labels, reason], float(count))
        yield from (up, connections, statements, retries, ended)


def start(*, enabled: bool, host: str, port: int) -> None:
    """Serve ``/metrics`` on ``host``:``port`` when ``enabled``; nothing otherwise.

    Raises:
        OSError: The port cannot be bound.
    """
    global _SERVER  # noqa: PLW0603 - one server per process
    if not enabled or _SERVER is not None:
        return
    from prometheus_client import (
        CollectorRegistry,
        GCCollector,
        PlatformCollector,
        ProcessCollector,
        start_http_server,
    )

    registry = CollectorRegistry()
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    registry.register(PoolCollector())
    _SERVER, _ = start_http_server(port, addr=host, registry=registry)
    logger.info("Prometheus metrics on http://%s:%d/metrics", host, port)


def stop() -> None:
    """Stop serving ``/metrics``, at shutdown."""
    global _SERVER  # noqa: PLW0603 - one server per process
    server, _SERVER = _SERVER, None
    if server is not None:
        server.shutdown()
        server.server_close()

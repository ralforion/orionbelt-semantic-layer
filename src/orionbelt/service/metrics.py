"""Prometheus metrics, served when ``METRICS_ENABLED`` is on.

Off by default. On, ``/metrics`` answers on ``METRICS_HOST``:``METRICS_PORT``
(``0.0.0.0:9090``): a port of its own, not the API's, so the metrics stay off
a public API unless that port is published.

What they report:

- **Requests to the REST API**, by method, route (the route's template, such as
  ``/v1/sessions/{session_id}/query/execute``, so one series covers every
  session) and status, and their durations.
- **Queries run on a database**, by the surface that asked (``rest``,
  ``pgwire``, ``flight``; ``other`` for the CLI and internal calls), dialect,
  data source and outcome (``ok``, ``error``: the database refused it, or
  ``unavailable``: it could not be run, such as no connection free), and their
  durations. A result served from the result cache runs no query.
- **The caches and sessions**, read at each scrape: the result cache's and the
  compilation cache's entries, size and lookups by result, and the sessions
  open.
- **The DuckDB worker pools** (:mod:`orionbelt.service.duckdb_pool`), read at
  each scrape: per source and worker, whether the worker takes queries, its
  connections by state, the statements started on it, the queries run again
  because it did not start them, and the statements the pool ended. A worker
  is labelled by the address its source lists, ``host:port``.
- **The process**: CPU, memory and open files where the system reports them,
  the Python version, and garbage collection.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_SERVER: Any = None

#: The surface a query came in through, set where it enters: ``rest``,
#: ``pgwire`` or ``flight``. Threads a query moves to run in a copy of the
#: context it was submitted from.
SURFACE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "obsl_metrics_surface", default="other"
)
#: Query durations, in seconds: from a cached lookup's few milliseconds to a
#: warehouse scan of minutes.
QUERY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)


@dataclass(frozen=True)
class _Recorders:
    """What the instrumented paths record into, created when the metrics start."""

    http_requests: Any
    http_seconds: Any
    queries: Any
    query_seconds: Any


#: ``None`` while the metrics are off: the recording functions then return at
#: once, and ``prometheus_client`` is never imported.
_RECORDERS: _Recorders | None = None


def enabled() -> bool:
    """Whether the metrics are on."""
    return _RECORDERS is not None


def observe_http(method: str, route: str, status: int, seconds: float) -> None:
    """Count a REST request; nothing while the metrics are off."""
    recorders = _RECORDERS
    if recorders is None:
        return
    recorders.http_requests.labels(method, route, str(status)).inc()
    recorders.http_seconds.labels(method, route).observe(seconds)


def observe_query(
    *, dialect: str, source: str, outcome: str, seconds: float, surface: str | None = None
) -> None:
    """Count a query run on a database; nothing while the metrics are off.

    ``surface`` defaults to :data:`SURFACE`, where the query came in.
    """
    recorders = _RECORDERS
    if recorders is None:
        return
    where = surface or SURFACE.get()
    recorders.queries.labels(where, dialect, source, outcome).inc()
    recorders.query_seconds.labels(where, dialect, source).observe(seconds)


class StateCollector:
    """The build, the sessions and the caches, read at each scrape."""

    def __init__(self, *, sessions: Any = None, cache: Any = None) -> None:
        self._sessions = sessions
        self._cache = cache

    def describe(self) -> list[Any]:
        return []

    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

        from orionbelt import __version__

        info = GaugeMetricFamily("obsl_build_info", "The running version", labels=["version"])
        info.add_metric([__version__], 1.0)
        yield info
        if self._sessions is not None:
            yield GaugeMetricFamily(
                "obsl_sessions", "Sessions open", value=float(self._sessions.active_count)
            )
            compiled = self._sessions.compilation_cache.stats()
            yield GaugeMetricFamily(
                "obsl_compilation_cache_entries",
                "Compiled queries kept",
                value=float(compiled.entries),
            )
            yield GaugeMetricFamily(
                "obsl_compilation_cache_bytes",
                "Estimated size of the compiled queries kept",
                value=float(compiled.bytes),
            )
            lookups = CounterMetricFamily(
                "obsl_compilation_cache_lookups",
                "Compilation cache lookups, by result: hit, miss, bypass (not cacheable)",
                labels=["result"],
            )
            for result, count in (
                ("hit", compiled.hits),
                ("miss", compiled.misses),
                ("bypass", compiled.bypasses),
            ):
                lookups.add_metric([result], float(count))
            yield lookups
            yield CounterMetricFamily(
                "obsl_compilation_cache_evictions",
                "Compiled queries evicted to stay within the cache's limits",
                value=float(compiled.evictions),
            )
        stats = self._result_cache_stats()
        if stats is not None:
            backend = GaugeMetricFamily(
                "obsl_result_cache_backend", "The result cache's backend", labels=["backend"]
            )
            backend.add_metric([stats.backend], 1.0)
            yield backend
            yield GaugeMetricFamily(
                "obsl_result_cache_entries", "Results cached", value=float(stats.entry_count)
            )
            yield GaugeMetricFamily(
                "obsl_result_cache_bytes",
                "Size of the results cached",
                value=float(stats.total_size_bytes),
            )
            lookups = CounterMetricFamily(
                "obsl_result_cache_lookups",
                "Result cache lookups, by result: hit, miss",
                labels=["result"],
            )
            lookups.add_metric(["hit"], float(stats.hit_count_total))
            lookups.add_metric(["miss"], float(stats.miss_count_total))
            yield lookups

    def _result_cache_stats(self) -> Any:
        """The result cache's statistics, read on the scrape's thread; ``None`` if none came.

        Never on the server's event loop, where a backend's blocking reads
        would hold up every request. ``Cache.stats`` runs on any thread and
        waits on none of the cache's locks.
        """
        if self._cache is None:
            return None
        try:
            return asyncio.run(self._cache.stats())
        except Exception:  # noqa: BLE001 - a scrape reports what it has
            logger.debug("result cache statistics unavailable for a scrape", exc_info=True)
            return None


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


def start(
    *,
    enabled: bool,
    host: str,
    port: int,
    sessions: Any = None,
    cache: Any = None,
) -> None:
    """Serve ``/metrics`` on ``host``:``port`` when ``enabled``; nothing otherwise.

    ``sessions`` (the session manager) and ``cache`` (the result cache) are
    read at each scrape, on the scrape's thread.

    Raises:
        OSError: The port cannot be bound.
    """
    global _SERVER, _RECORDERS  # noqa: PLW0603 - one server per process
    if not enabled or _SERVER is not None:
        return
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        GCCollector,
        Histogram,
        PlatformCollector,
        ProcessCollector,
        start_http_server,
    )

    registry = CollectorRegistry()
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    registry.register(StateCollector(sessions=sessions, cache=cache))
    registry.register(PoolCollector())
    recorders = _Recorders(
        http_requests=Counter(
            "obsl_http_requests",
            "REST API requests, by method, route template and status",
            ["method", "route", "status"],
            registry=registry,
        ),
        http_seconds=Histogram(
            "obsl_http_request_duration_seconds",
            "REST API request durations, by method and route template",
            ["method", "route"],
            registry=registry,
        ),
        queries=Counter(
            "obsl_queries",
            "Queries run on a database, by surface, dialect, data source and outcome",
            ["surface", "dialect", "source", "outcome"],
            registry=registry,
        ),
        query_seconds=Histogram(
            "obsl_query_duration_seconds",
            "Durations of the queries run on a database, results fetched",
            ["surface", "dialect", "source"],
            buckets=QUERY_BUCKETS,
            registry=registry,
        ),
    )
    _SERVER, _ = start_http_server(port, addr=host, registry=registry)
    _RECORDERS = recorders
    logger.info("Prometheus metrics on http://%s:%d/metrics", host, port)


def stop() -> None:
    """Stop serving ``/metrics`` and recording, at shutdown."""
    global _SERVER, _RECORDERS  # noqa: PLW0603 - one server per process
    server, _SERVER = _SERVER, None
    _RECORDERS = None
    if server is not None:
        server.shutdown()
        server.server_close()

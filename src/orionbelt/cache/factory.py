"""Cache backend factory."""

from __future__ import annotations

import logging

from orionbelt.cache.noop import NoopCache
from orionbelt.cache.protocol import Cache
from orionbelt.settings import Settings

logger = logging.getLogger(__name__)


def build_cache(settings: Settings) -> Cache:
    """Construct the configured cache backend.

    Selection: ``CACHE_BACKEND=noop`` (default) → :class:`NoopCache`;
    ``CACHE_BACKEND=file`` → :class:`FileCache`. Unknown values fall back
    to noop and log a warning rather than failing startup.
    """
    backend = (settings.cache_backend or "noop").strip().lower()
    if backend == "file":
        from orionbelt.cache.file import FileCache

        return FileCache(
            cache_dir=settings.cache_dir,
            max_value_bytes=settings.cache_max_value_bytes,
            max_disk_bytes=settings.cache_max_disk_bytes,
            max_ttl_seconds=settings.cache_max_ttl_seconds,
            sweep_interval_seconds=settings.cache_sweep_interval_seconds,
        )
    if backend != "noop":
        logger.warning("Unknown CACHE_BACKEND=%r — falling back to noop", backend)
    return NoopCache()

"""SQL dialect plugin system for OrionBelt Semantic Layer."""

# Import dialects to trigger registration
import orionbelt.dialect.bigquery as _bigquery  # noqa: F401
import orionbelt.dialect.clickhouse as _clickhouse  # noqa: F401
import orionbelt.dialect.databricks as _databricks  # noqa: F401
import orionbelt.dialect.dremio as _dremio  # noqa: F401
import orionbelt.dialect.duckdb as _duckdb  # noqa: F401
import orionbelt.dialect.mysql as _mysql  # noqa: F401
import orionbelt.dialect.postgres as _postgres  # noqa: F401
import orionbelt.dialect.snowflake as _snowflake  # noqa: F401
from orionbelt.dialect.base import Dialect, DialectCapabilities
from orionbelt.dialect.registry import DialectRegistry

__all__ = [
    "Dialect",
    "DialectCapabilities",
    "DialectRegistry",
]

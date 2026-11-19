"""The name OBSL gives itself on the connections it opens to a data source.

Each database shows it in its own query history: as the User-Agent on
BigQuery, ClickHouse, Databricks and DuckDB (MotherDuck), as
``application_name`` on Postgres, and as the ``program_name`` connection
attribute on MySQL. Snowflake and Dremio get none: Snowflake's
``application`` is reserved for its registered partners, and no Dremio
setting has been checked to reach its job history.
"""

from __future__ import annotations

from orionbelt import __version__

CLIENT_NAME = f"orionbelt-semantic-layer/{__version__}"

"""ob-bigquery — OrionBelt Semantic Layer driver for BigQuery (PEP 249 DB-API 2.0).

Requires the OrionBelt REST API running in single-model mode (MODEL_FILE set).
OBML queries are compiled to SQL via ``POST /v1/query/sql``.

Usage::

    import ob_bigquery

    conn = ob_bigquery.connect(project="my-gcp-project")
    with conn.cursor() as cur:
        cur.execute("select:\\n  dimensions:\\n    - Region\\n  measures:\\n    - Revenue")
        print(cur.fetchall())
"""

from __future__ import annotations

from typing import Any

from google.cloud import bigquery

from ob_bigquery.connection import Connection
from ob_bigquery.exceptions import (
    DataError,
    DatabaseError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
    Warning,
)

# PEP 249 module-level constants
apilevel = "2.0"
threadsafety = 1  # threads may share the module but not connections
paramstyle = "pyformat"  # BigQuery uses %(name)s placeholders


def connect(
    *,
    project: str | None = None,
    credentials: Any = None,
    credentials_file: str | None = None,
    location: str | None = None,
    # OrionBelt parameters
    ob_api_url: str = "http://localhost:8000",
    ob_timeout: int = 30,
) -> Connection:
    """Open a BigQuery connection with OBML support.

    Parameters
    ----------
    project : str, optional
        GCP project ID. If not set, uses Application Default Credentials project.
    credentials : google.auth.credentials.Credentials, optional
        Explicit credentials object. If not set, uses ADC.
    credentials_file : str, optional
        Path to a service account JSON key file. Alternative to setting
        ``GOOGLE_APPLICATION_CREDENTIALS`` env var.
    location : str, optional
        Default dataset location (e.g. ``US``, ``EU``).
    ob_api_url : str
        OrionBelt REST API URL (must be running in single-model mode).
    ob_timeout : int
        HTTP timeout in seconds for OBML compilation.
    """
    if credentials_file is not None and credentials is None:
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(credentials_file)

    client_kwargs: dict[str, Any] = {}
    if project is not None:
        client_kwargs["project"] = project
    if credentials is not None:
        client_kwargs["credentials"] = credentials
    if location is not None:
        client_kwargs["location"] = location

    native = bigquery.Client(**client_kwargs)
    return Connection(
        native,
        ob_api_url=ob_api_url,
        ob_timeout=ob_timeout,
    )


__all__ = [
    "apilevel",
    "threadsafety",
    "paramstyle",
    "connect",
    "Connection",
    "Warning",
    "Error",
    "InterfaceError",
    "DatabaseError",
    "DataError",
    "OperationalError",
    "IntegrityError",
    "InternalError",
    "ProgrammingError",
    "NotSupportedError",
]

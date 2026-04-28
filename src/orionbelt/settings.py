"""Shared settings loaded from environment / .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for OrionBelt REST API server.

    Values are read from environment variables and from a ``.env`` file
    in the working directory.  See ``.env.template`` for all options.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Shared
    log_level: str = "INFO"
    # Log format:
    #   "console"  — pretty-printed for local dev (default)
    #   "json"     — structured JSON for log aggregators (ELK, Datadog, etc.)
    #   "cloudrun" — JSON + disables uvicorn access logs (Cloud Run provides its own)
    log_format: str = "console"

    # REST API
    api_server_host: str = "localhost"
    api_server_port: int = 8000
    port: int | None = None  # Cloud Run injects PORT; takes precedence over api_server_port

    # Public-doc surfaces. Default True preserves current public-demo behaviour.
    # Set EXPOSE_API_DOCS=false on non-demo deployments to disable Swagger UI,
    # ReDoc, and the OpenAPI schema endpoint. EXPOSE_OPENAPI_SCHEMA can be
    # toggled independently to keep /openapi.json live (e.g. for client codegen)
    # while hiding the human-facing /docs and /redoc pages.
    expose_api_docs: bool = True
    expose_openapi_schema: bool = True

    @property
    def effective_port(self) -> int:
        """Return the port to listen on (Cloud Run PORT takes precedence)."""
        return self.port if self.port is not None else self.api_server_port

    # Sessions
    session_ttl_seconds: int = 1800  # 30 min inactivity
    session_max_age_seconds: int = 86400  # 24 h absolute max lifetime
    session_cleanup_interval: int = 60  # seconds between cleanup sweeps
    max_sessions: int = 500  # global concurrent session cap (429 when full)
    max_models_per_session: int = 10  # max models a single session may hold
    disable_session_list: bool = False  # hide GET /sessions endpoint
    session_rate_limit: int = 10  # max POST /sessions per IP per minute
    trusted_proxy_count: int = 0  # number of trusted reverse proxies in front of the app

    # Single-model mode — pre-loaded into every new session.
    # When set, model upload/removal endpoints return 403.
    model_dir: str | None = None  # base directory for MODEL_FILE (set by Docker)
    model_file: str | None = None  # filename or absolute path to OBML YAML

    # Query execution
    query_execute: bool = False  # enable POST /v1/query/execute
    query_default_limit: int = 1000  # max rows when query has no LIMIT
    db_pool_size: int = 5  # connection pool size per dialect

    # Arrow Flight SQL server (requires ob-flight-extension)
    flight_enabled: bool = False  # start gRPC Flight server on FLIGHT_PORT (implies query_execute)
    flight_port: int = 8815
    flight_auth_mode: str = "none"  # "none" or "token"
    flight_api_token: str | None = None
    db_vendor: str = "duckdb"  # default vendor driver for Flight query execution

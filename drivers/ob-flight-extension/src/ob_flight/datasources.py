"""Data sources: the databases this server can connect to, by name.

Connections used to be global per dialect: ``POSTGRES_HOST`` and friends, one
pool per dialect, so one process could not serve two Postgres databases.
Named data sources are declared in the environment::

    OBSL_DATABASES=crm,shop-eu
    DB_CRM_TYPE=postgres
    DB_CRM_DESCRIPTION=CRM: customers, returns
    DB_CRM_POSTGRES_HOST=crm.internal
    DB_SHOP_EU_TYPE=mysql
    DB_SHOP_EU_MYSQL_HOST=eu.shop.internal

Each one reads the dialect's usual variables behind a ``DB_<NAME>_`` prefix. A
variable it does not set falls back to the unprefixed one, so two sources can
share a user and password and differ only in host or database. A dialect
configured the old way, without a name, is still a source, named after the
dialect: that is how every connection worked before, and still works when
``OBSL_DATABASES`` is unset.

The ``DB_<NAME>_`` prefix and the name rules match OrionBelt Analytics
(``database_registry.py`` there), so one ``.env`` can carry the sources of both.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import quote

DATABASES_VARIABLE = "OBSL_DATABASES"


class MotherDuckTokenMissingError(RuntimeError):
    """Raised when a ``md:`` database is configured without a token.

    Its own type so callers can distinguish a configuration error from a
    connection failure — the fix is an env var, not a retry.
    """


class DataSourceConfigError(ValueError):
    """The named data sources in the environment are not usable as written."""


class DataSourceNotFoundError(LookupError):
    """No configured data source has the name asked for."""


#: Settings that shape a connection but are not ``connect()`` arguments, read
#: where OBSL opens the connection (``orionbelt.service.duckdb_source``):
#: DuckDB's per-connection statements (they decide how names resolve), memory,
#: threads and spilling, and the paths its queries may read. For a source on
#: Hardcharger workers (``orionbelt.service.duckdb_pool``): where its queries
#: run, the workers, and who signs in to them, whose grants decide what a
#: query may read.
#: Part of the fingerprint, so sources differing in them get pools of their
#: own, and, as the paths and grants decide what a query may return, cached
#: results too.
_FINGERPRINT_SETTINGS: dict[str, tuple[str, ...]] = {
    "duckdb": (
        "DUCKDB_CONNECTION_SQL",
        "DUCKDB_MEMORY_LIMIT",
        "DUCKDB_THREADS",
        "DUCKDB_SPILL_DIR",
        "DUCKDB_SPILL_MAX_MB",
        "DUCKDB_ALLOWED_PATHS",
        "DUCKDB_BACKEND",
        "HARDCHARGER_WORKERS",
        "HARDCHARGER_USER",
        "HARDCHARGER_PASSWORD",
        "HARDCHARGER_TLS_ROOT_CERTS",
    ),
}

# Environment variable prefixes for vendor credentials
_CREDENTIAL_KEYS: dict[str, list[str]] = {
    "bigquery": [
        "BIGQUERY_PROJECT",
        "BIGQUERY_LOCATION",
        "BIGQUERY_CREDENTIALS_FILE",
    ],
    # DUCKDB_INIT_SQL decides what the source's tables are (an ATTACHed
    # catalog, a view over files), so it counts toward the fingerprint.
    "duckdb": ["DUCKDB_DATABASE", "MOTHERDUCK_ACCESS_TOKEN", "DUCKDB_INIT_SQL"],
    "postgres": [
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DBNAME",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_SCHEMA",
    ],
    "snowflake": [
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_PASSWORD",
        "SNOWFLAKE_DATABASE",
        "SNOWFLAKE_SCHEMA",
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_ROLE",
    ],
    "clickhouse": [
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_USERNAME",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_DATABASE",
    ],
    "dremio": [
        "DREMIO_HOST",
        "DREMIO_PORT",
        "DREMIO_USERNAME",
        "DREMIO_PASSWORD",
    ],
    "databricks": [
        "DATABRICKS_SERVER_HOSTNAME",
        "DATABRICKS_HTTP_PATH",
        "DATABRICKS_ACCESS_TOKEN",
        "DATABRICKS_CATALOG",
        "DATABRICKS_SCHEMA",
    ],
    "mysql": [
        "MYSQL_HOST",
        "MYSQL_PORT",
        "MYSQL_DATABASE",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
    ],
}

# Alternate spellings accepted for a credential env var, tried in order when
# the canonical name is unset.
#
# ``DATABRICKS_TOKEN`` is the name Databricks' own CLI and SDK export, so it
# is what people already have in their environment; ``.env.template`` and
# ``docs/drivers.md`` document ``DATABRICKS_ACCESS_TOKEN``. Every other
# consumer in the repo already accepts both (tests, seed and probe scripts) —
# this router was the sole holdout, which meant an environment written to the
# Databricks convention produced a connection with no token at all and no
# indication why.
#
# The OrionBelt Analytics spellings (``POSTGRES_DATABASE``, ``MYSQL_USERNAME``,
# ``BIGQUERY_PROJECT_ID``, ...) are accepted too, so an ``.env`` written for
# Analytics connects here as well.
_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "DATABRICKS_ACCESS_TOKEN": ("DATABRICKS_TOKEN",),
    # MotherDuck's docs and examples use both ``MOTHERDUCK_TOKEN`` and the
    # lowercase ``motherduck_token`` (the latter is what the DuckDB extension
    # reads straight from the environment). Accepting both means an
    # environment already set up for MotherDuck's own tooling needs no extra
    # configuration; the ``_ACCESS_`` name stays canonical for consistency
    # with every other vendor key here.
    "MOTHERDUCK_ACCESS_TOKEN": (
        "MOTHERDUCK_TOKEN",
        "motherduck_token",
    ),
    "POSTGRES_DBNAME": ("POSTGRES_DATABASE",),
    "POSTGRES_USER": ("POSTGRES_USERNAME",),
    "MYSQL_USER": ("MYSQL_USERNAME",),
    "SNOWFLAKE_USER": ("SNOWFLAKE_USERNAME",),
    "BIGQUERY_PROJECT": ("BIGQUERY_PROJECT_ID",),
    "BIGQUERY_CREDENTIALS_FILE": ("BIGQUERY_CREDENTIALS_PATH",),
    "DUCKDB_DATABASE": ("DUCKDB_DATABASE_PATH",),
}

# Dialect spellings OrionBelt Analytics uses for ``DB_<NAME>_TYPE``.
_TYPE_ALIASES: dict[str, str] = {"postgresql": "postgres"}

#: How a named source logs in: one service account for everyone (the
#: default), or as the signed-in user (``DB_<NAME>_AUTH=user``).
SERVICE_LOGIN = "service"
USER_LOGIN = "user"
#: Dialects that can log in as the user, by exchanging the caller's identity
#: provider token for one issued for the warehouse.
USER_LOGIN_DIALECTS: frozenset[str] = frozenset({"bigquery", "databricks", "snowflake"})
#: Entra ID's on-behalf-of flow (``DB_<NAME>_OAUTH_EXCHANGE=entra_obo``).
ENTRA_OBO = "entra_obo"
#: The standard token exchange, RFC 8693, at the identity provider: Okta,
#: Keycloak (``DB_<NAME>_OAUTH_EXCHANGE=token_exchange``).
TOKEN_EXCHANGE = "token_exchange"
#: Dialects whose user token the identity provider issues, not the warehouse,
#: with the ways OBSL can ask it for one. Each needs OBSL's own client at the
#: identity provider (``OIDC_CLIENT_ID``) and ``DB_<NAME>_OAUTH_SCOPE``.
IDP_ISSUED_EXCHANGES: dict[str, frozenset[str]] = {
    "snowflake": frozenset({ENTRA_OBO, TOKEN_EXCHANGE})
}

# A DuckDB database string beginning with this prefix is MotherDuck, not a
# local file: remote, authenticated, and not subject to the file-lock
# reasoning behind the read-only default.
_MOTHERDUCK_PREFIX = "md:"

# Token parameters MotherDuck accepts on the database string. A read-scaling
# token authenticates against the read replica pool and is spelled
# differently, so a URI already carrying one must not be treated as tokenless.
_MOTHERDUCK_URI_TOKEN_PARAMS = ("motherduck_token", "read_scaling_token")

# Env var name -> connect() kwarg name mapping
_ENV_TO_KWARG: dict[str, str] = {
    "BIGQUERY_PROJECT": "project",
    "BIGQUERY_LOCATION": "location",
    "BIGQUERY_CREDENTIALS_FILE": "credentials_file",
    "DUCKDB_DATABASE": "database",
    "DUCKDB_INIT_SQL": "init_sql",
    "MOTHERDUCK_ACCESS_TOKEN": "motherduck_token",
    "POSTGRES_HOST": "host",
    "POSTGRES_PORT": "port",
    "POSTGRES_DBNAME": "dbname",
    "POSTGRES_USER": "user",
    "POSTGRES_PASSWORD": "password",
    "POSTGRES_SCHEMA": "schema",
    "SNOWFLAKE_ACCOUNT": "account",
    "SNOWFLAKE_USER": "user",
    "SNOWFLAKE_PASSWORD": "password",
    "SNOWFLAKE_DATABASE": "database",
    "SNOWFLAKE_SCHEMA": "schema",
    "SNOWFLAKE_WAREHOUSE": "warehouse",
    "SNOWFLAKE_ROLE": "role",
    "CLICKHOUSE_HOST": "host",
    "CLICKHOUSE_PORT": "port",
    "CLICKHOUSE_USERNAME": "username",
    "CLICKHOUSE_PASSWORD": "password",
    "CLICKHOUSE_DATABASE": "database",
    "DREMIO_HOST": "host",
    "DREMIO_PORT": "port",
    "DREMIO_USERNAME": "username",
    "DREMIO_PASSWORD": "password",
    "DATABRICKS_SERVER_HOSTNAME": "server_hostname",
    "DATABRICKS_HTTP_PATH": "http_path",
    "DATABRICKS_ACCESS_TOKEN": "access_token",
    "DATABRICKS_CATALOG": "catalog",
    "DATABRICKS_SCHEMA": "schema",
    "MYSQL_HOST": "host",
    "MYSQL_PORT": "port",
    "MYSQL_DATABASE": "database",
    "MYSQL_USER": "user",
    "MYSQL_PASSWORD": "password",
}

# The variable whose presence means a dialect is configured without a name.
_PRESENCE_VARIABLES: dict[str, tuple[str, ...]] = {
    "bigquery": ("BIGQUERY_PROJECT",),
    "duckdb": ("DUCKDB_DATABASE", "MOTHERDUCK_ACCESS_TOKEN"),
    "postgres": ("POSTGRES_HOST",),
    "snowflake": ("SNOWFLAKE_ACCOUNT",),
    "clickhouse": ("CLICKHOUSE_HOST",),
    "dremio": ("DREMIO_HOST",),
    "databricks": ("DATABRICKS_SERVER_HOSTNAME",),
    "mysql": ("MYSQL_HOST",),
}

# What may be shown about a source: where it points, never how it signs in.
# A whitelist, so a variable added to a driver later stays hidden until
# someone decides it is safe to show. No host: it says where the database
# runs, which a listing has no reason to tell. No DuckDB path: a MotherDuck
# string may carry its token.
_DISPLAY_VARIABLES: dict[str, tuple[str, ...]] = {
    "bigquery": ("BIGQUERY_PROJECT",),
    "duckdb": (),
    "postgres": ("POSTGRES_DBNAME", "POSTGRES_SCHEMA"),
    "snowflake": ("SNOWFLAKE_DATABASE", "SNOWFLAKE_SCHEMA"),
    "clickhouse": ("CLICKHOUSE_DATABASE",),
    "dremio": (),
    "databricks": ("DATABRICKS_CATALOG", "DATABRICKS_SCHEMA"),
    "mysql": ("MYSQL_DATABASE",),
}

DIALECTS: tuple[str, ...] = tuple(_CREDENTIAL_KEYS)


def env_key(name: str) -> str:
    """The variable prefix part for a source name: ``shop-eu`` -> ``SHOP_EU``."""
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")


def _normalized(name: str) -> str:
    """A name as compared: case, spaces, dashes and underscores do not matter."""
    return re.sub(r"[\s_\-]+", "", name.casefold())


@dataclass(frozen=True)
class DataSource:
    """One database the server can connect to.

    Attributes:
        name: What a person calls it; the dialect's name for an implicit one.
        dialect: One of :data:`DIALECTS`.
        description: What it holds, for a reader choosing between several.
        prefix: The variable prefix of a named source (``DB_CRM_``); empty for
            an implicit one.
    """

    name: str
    dialect: str
    description: str = ""
    prefix: str = ""
    environ: Mapping[str, str] = field(
        default_factory=lambda: os.environ, repr=False, compare=False
    )

    @property
    def named(self) -> bool:
        """Whether it was declared by name rather than configured by dialect."""
        return bool(self.prefix)

    @property
    def login(self) -> str:
        """``user`` when the source logs in as the caller (``DB_<NAME>_AUTH=user``).

        Otherwise ``service``: one account for everyone, as an implicit
        source always does. Validated by :func:`configured_datasources`.
        """
        if not self.prefix:
            return SERVICE_LOGIN
        value = self.environ.get(f"{self.prefix}AUTH", SERVICE_LOGIN).strip().lower()
        return value or SERVICE_LOGIN

    def oauth_client_id(self) -> str | None:
        """``DB_<NAME>_OAUTH_CLIENT_ID``: the client a token exchange names, if it needs one."""
        return self._named_setting("OAUTH_CLIENT_ID")

    def oauth_exchange(self) -> str | None:
        """``DB_<NAME>_OAUTH_EXCHANGE``: how the identity provider is asked for the user's token."""
        value = self._named_setting("OAUTH_EXCHANGE")
        return value.lower() if value else None

    def oauth_scope(self) -> str | None:
        """``DB_<NAME>_OAUTH_SCOPE``: what the identity provider issues the user's token for."""
        return self._named_setting("OAUTH_SCOPE")

    def workforce_provider(self) -> str | None:
        """``DB_<NAME>_WORKFORCE_PROVIDER``: the BigQuery user's workforce pool provider."""
        return self._named_setting("WORKFORCE_PROVIDER")

    def workforce_user_project(self) -> str | None:
        """``DB_<NAME>_WORKFORCE_USER_PROJECT``, else the source's BigQuery project."""
        return self._named_setting("WORKFORCE_USER_PROJECT") or self.getenv("BIGQUERY_PROJECT")

    def oauth_audience(self) -> str | None:
        """``DB_<NAME>_OAUTH_AUDIENCE``: the audience a token exchange asks for."""
        return self._named_setting("OAUTH_AUDIENCE")

    @property
    def idp_issues_token(self) -> bool:
        """Whether its user token comes from the identity provider, through OBSL's own client."""
        return self.login == USER_LOGIN and self.dialect in IDP_ISSUED_EXCHANGES

    def _named_setting(self, setting: str) -> str | None:
        """``DB_<NAME>_<setting>``, stripped; None when unset or for an implicit source."""
        if not self.prefix:
            return None
        return self.environ.get(f"{self.prefix}{setting}", "").strip() or None

    def cache_max_ttl_seconds(self, default: float) -> float:
        """``DB_<NAME>_CACHE_MAX_TTL_SECONDS``, else ``DB_CACHE_MAX_TTL_SECONDS``, else ``default``.

        Caps how long a source that logs in as the user keeps results cached.
        """
        return float(self._pool_setting("CACHE_MAX_TTL_SECONDS", str(default)))

    def snapshot(self) -> DataSource:
        """The source with its settings as they are now, unaffected by later changes.

        A query keyed (cache, pool) by the fingerprint of these settings must
        also connect with them: read live, a change in between would store or
        serve another database's rows under the old key.
        """
        return replace(self, environ=dict(self.environ))

    def getenv(self, variable: str) -> str | None:
        """A setting for this source.

        The prefixed variable, then the unprefixed one, each under its
        canonical name before its aliases: a source's own value always wins
        over a shared default, whatever it is spelled.
        """
        spellings = (variable, *_ENV_ALIASES.get(variable, ()))
        scopes = (self.prefix, "") if self.prefix else ("",)
        for scope in scopes:
            for spelling in spellings:
                value = self.environ.get(f"{scope}{spelling}")
                if value is not None:
                    return value
        return None

    def _raw_credentials(self) -> dict[str, Any]:
        """The ``connect()`` kwargs the environment sets, before any override."""
        creds: dict[str, Any] = {}
        for variable in _CREDENTIAL_KEYS.get(self.dialect, []):
            raw = self.getenv(variable)
            if raw is not None:
                kwarg_name = _ENV_TO_KWARG.get(variable, variable.lower())
                creds[kwarg_name] = int(raw) if variable.endswith("_PORT") else raw
        return creds

    def credentials(self, **overrides: Any) -> dict[str, Any]:
        """Keyword arguments for the dialect's ``connect()``.

        ``overrides`` are applied *before* any dialect-specific
        post-processing, so a caller-supplied value is what that processing
        sees. This matters for MotherDuck: folding the token into the
        database string has to happen after an override may have replaced the
        database, or ``connect()`` would raise for an env-configured ``md:``
        that the caller overrode away — and would pass a tokenless ``md:``
        when the caller supplied one.

        Only variables that are set are included.
        """
        creds = self._raw_credentials()
        creds.update(overrides)
        if self.dialect == "duckdb":
            _apply_motherduck_token(creds)
        return creds

    def pool_size(self) -> int:
        """Idle connections to keep: ``DB_<NAME>_POOL_SIZE``, then ``DB_POOL_SIZE``."""
        return int(self._pool_setting("POOL_SIZE", "5"))

    def user_connection_idle_seconds(self) -> float:
        """How long a user's idle connection waits for their next query, for a source
        that logs in as the user: ``DB_<NAME>_USER_CONNECTION_IDLE_SECONDS``, then
        ``DB_USER_CONNECTION_IDLE_SECONDS``, then 300; 0 keeps none."""
        return float(self._pool_setting("USER_CONNECTION_IDLE_SECONDS", "300"))

    def max_connections(self) -> int:
        """Connections in use at once: ``DB_<NAME>_MAX_CONNECTIONS``, then
        ``DB_MAX_CONNECTIONS``, then 10; 0 for no limit."""
        return int(self._pool_setting("MAX_CONNECTIONS", "10"))

    def acquire_timeout_seconds(self) -> float:
        """How long a query waits for a connection: ``DB_<NAME>_ACQUIRE_TIMEOUT_SECONDS``,
        then ``DB_ACQUIRE_TIMEOUT_SECONDS``, then 30."""
        return float(self._pool_setting("ACQUIRE_TIMEOUT_SECONDS", "30"))

    def _pool_setting(self, setting: str, default: str) -> str:
        raw = self.environ.get(f"{self.prefix}{setting}") if self.prefix else None
        return raw if raw is not None else self.environ.get(f"DB_{setting}", default)

    def fingerprint(self) -> str:
        """Who connects to what, as 16 hex characters.

        A hash over the dialect and every connection setting, secrets
        included, so a changed password is a different identity. The settings
        that shape a connection without being ``connect()`` arguments
        (:data:`_FINGERPRINT_SETTINGS`) count too: a pool hands out
        connections opened with one set of them. Two names
        for the same target with the same credentials share it, and with it
        a pool: the rows they return are the same. For a BigQuery key file
        the file's content counts, not only its path. The hash shows nothing
        of what went into it.
        """
        material: dict[str, Any] = dict(self._raw_credentials())
        if self.login == USER_LOGIN:
            # Never the same pool or cache scope as a service login to the same
            # target. Only added for a user login, so every existing
            # fingerprint, and the cache keys built on it, stay as they were.
            material["AUTH"] = USER_LOGIN
            # Who the user is at the warehouse, and with which role, follows
            # from these: the client names the federation policy a token is
            # exchanged under, the scope the role it carries. Another value
            # shares no token and no cache scope.
            for setting in (
                "OAUTH_CLIENT_ID",
                "OAUTH_EXCHANGE",
                "OAUTH_SCOPE",
                "OAUTH_AUDIENCE",
                "WORKFORCE_PROVIDER",
                "WORKFORCE_USER_PROJECT",
            ):
                value = self._named_setting(setting)
                if value:
                    material[setting] = value
        for variable in _FINGERPRINT_SETTINGS.get(self.dialect, ()):
            value = self.getenv(variable)
            if value is not None:
                material[variable] = value
        key_file = material.get("credentials_file")
        if isinstance(key_file, str):
            try:
                with open(key_file, "rb") as fh:
                    material["credentials_file_sha256"] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                pass  # connect() reports the missing file; the path alone is the identity
        payload = json.dumps([self.dialect, sorted(material.items())], default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def describe(self) -> dict[str, str]:
        """What may be shown about it: name, dialect, description and target.

        Never a host, user, password or token.
        """
        shown = {"name": self.name, "dialect": self.dialect}
        if self.description:
            shown["description"] = self.description
        for variable in _DISPLAY_VARIABLES.get(self.dialect, ()):
            value = self.getenv(variable)
            if value:
                # POSTGRES_DBNAME -> dbname
                shown[variable.split("_", 1)[1].lower()] = value
        return shown


def _apply_motherduck_token(creds: dict[str, Any]) -> None:
    """Fold a MotherDuck token into the ``md:`` database string, in place.

    ``duckdb.connect`` takes no token argument — the token travels as a query
    parameter on the database string — so the credential is resolved here
    rather than at each call site. ``db_executor`` opens DuckDB three
    different ways and all three read their credentials through this
    function, so this is the one seam that covers them.

    Fails loudly when a ``md:`` database has no token. That is deliberate:
    the DuckDB extension falls back to *interactive browser authentication*
    when a token is absent, which on a server does not raise — it **hangs**,
    presenting as a stuck worker rather than a failed connection. It can also
    silently pick up an ambient lowercase ``motherduck_token`` and connect to
    whichever account that belongs to. Passing the token explicitly, always,
    removes both.
    """
    token = creds.pop("motherduck_token", None)
    database = creds.get("database")
    if not isinstance(database, str) or not database.startswith(_MOTHERDUCK_PREFIX):
        return
    if any(f"{name}=" in database for name in _MOTHERDUCK_URI_TOKEN_PARAMS):
        return  # caller embedded a token in DUCKDB_DATABASE already
    if not token:
        raise MotherDuckTokenMissingError(
            f"DUCKDB_DATABASE is {database!r} (MotherDuck) but no token is set. "
            "Set MOTHERDUCK_ACCESS_TOKEN (or the lowercase motherduck_token that "
            "MotherDuck's own CLI uses). Without one the DuckDB extension falls "
            "back to interactive browser auth, which hangs on a server."
        )
    sep = "&" if "?" in database else "?"
    creds["database"] = f"{database}{sep}motherduck_token={quote(str(token), safe='')}"


def _check_idp_issued(name: str, dialect: str, prefix: str, env: Mapping[str, str]) -> None:
    """A source whose user token the identity provider issues names how, and for what.

    Raises:
        DataSourceConfigError: ``OAUTH_EXCHANGE`` is not one this dialect
            supports, or ``OAUTH_SCOPE`` is unset.
    """
    supported = IDP_ISSUED_EXCHANGES[dialect]
    exchange = env.get(f"{prefix}OAUTH_EXCHANGE", "").strip().lower()
    if exchange not in supported:
        raise DataSourceConfigError(
            f"Data source '{name}' ({dialect}) logs in as the user with a token the identity "
            f"provider issues: set {prefix}OAUTH_EXCHANGE to {', '.join(sorted(supported))} "
            f"(found {exchange or 'nothing'!r})"
        )
    if not env.get(f"{prefix}OAUTH_SCOPE", "").strip():
        raise DataSourceConfigError(
            f"Data source '{name}' ({dialect}) logs in as the user: set {prefix}OAUTH_SCOPE to "
            "the scope the identity provider issues its token for "
            "(e.g. api://<Snowflake app ID>/session:role-any)"
        )


#: What a workforce pool provider's resource name begins with.
_WORKFORCE_PROVIDER_PREFIX = "//iam.googleapis.com/locations/global/workforcePools/"


def _check_workforce(name: str, prefix: str, env: Mapping[str, str]) -> None:
    """A BigQuery source that logs in as the user names its workforce pool provider.

    Raises:
        DataSourceConfigError: ``WORKFORCE_PROVIDER`` is unset or not a
            provider's resource name, or no project is billed for the exchange.
    """
    provider = env.get(f"{prefix}WORKFORCE_PROVIDER", "").strip()
    if not provider.startswith(_WORKFORCE_PROVIDER_PREFIX) or "/providers/" not in provider:
        raise DataSourceConfigError(
            f"Data source '{name}' (bigquery) logs in as the user: set "
            f"{prefix}WORKFORCE_PROVIDER to the workforce pool provider, "
            f"{_WORKFORCE_PROVIDER_PREFIX}<pool>/providers/<provider> "
            f"(found {provider or 'nothing'!r})"
        )
    project = (
        env.get(f"{prefix}WORKFORCE_USER_PROJECT", "").strip()
        or env.get(f"{prefix}BIGQUERY_PROJECT", "").strip()
    )
    if not project:
        raise DataSourceConfigError(
            f"Data source '{name}' (bigquery) logs in as the user: set "
            f"{prefix}WORKFORCE_USER_PROJECT, or {prefix}BIGQUERY_PROJECT, to the project "
            "billed for the token exchange"
        )


def configured_datasources(environ: Mapping[str, str] | None = None) -> list[DataSource]:
    """Every data source this server is configured for.

    Named sources first, in the order ``OBSL_DATABASES`` lists them, then each
    dialect configured without a name, unless that dialect also has named
    sources, whose shared defaults its variables then are.

    Raises:
        DataSourceConfigError: A name appears twice, has no letters or digits,
            reads the same variables as another, or its type is missing or
            unknown.
    """
    env = os.environ if environ is None else environ
    sources: list[DataSource] = []
    seen: set[str] = set()
    # Names are told apart two ways, as a person writes them and by the
    # variable prefix they read, and must differ under both: "shop.eu" and
    # "shop/eu" are different names that would read the same DB_SHOP_EU_*.
    prefixes: dict[str, str] = {}

    for raw in env.get(DATABASES_VARIABLE, "").split(","):
        name = raw.strip()
        if not name:
            continue
        key = _normalized(name)
        if key in seen:
            raise DataSourceConfigError(
                f"Data source name '{name}' appears twice in {DATABASES_VARIABLE}"
            )
        seen.add(key)
        if not env_key(name):
            raise DataSourceConfigError(
                f"Data source name '{name}' has no letters or digits to build its "
                "variable prefix from"
            )
        prefix = f"DB_{env_key(name)}_"
        if prefix in prefixes:
            raise DataSourceConfigError(
                f"Data source names '{prefixes[prefix]}' and '{name}' would both "
                f"read {prefix}* variables; rename one"
            )
        prefixes[prefix] = name
        declared = env.get(f"{prefix}TYPE", "").strip().lower()
        dialect = _TYPE_ALIASES.get(declared, declared)
        if not dialect:
            raise DataSourceConfigError(
                f"Data source '{name}' has no type: set {prefix}TYPE to one of "
                f"{', '.join(DIALECTS)}"
            )
        if dialect not in DIALECTS:
            raise DataSourceConfigError(
                f"Data source '{name}' has unsupported type '{declared}' "
                f"({prefix}TYPE); use one of {', '.join(DIALECTS)}"
            )
        login = env.get(f"{prefix}AUTH", SERVICE_LOGIN).strip().lower() or SERVICE_LOGIN
        if login not in (SERVICE_LOGIN, USER_LOGIN):
            raise DataSourceConfigError(
                f"Data source '{name}' has {prefix}AUTH={login!r}; use "
                f"'{SERVICE_LOGIN}' (one account for everyone) or '{USER_LOGIN}' "
                "(as the signed-in user)"
            )
        if login == USER_LOGIN and dialect not in USER_LOGIN_DIALECTS:
            raise DataSourceConfigError(
                f"Data source '{name}' ({dialect}) cannot log in as the user yet "
                f"({prefix}AUTH=user); supported: {', '.join(sorted(USER_LOGIN_DIALECTS))}"
            )
        if login == USER_LOGIN and dialect in IDP_ISSUED_EXCHANGES:
            _check_idp_issued(name, dialect, prefix, env)
        if login == USER_LOGIN and dialect == "bigquery":
            _check_workforce(name, prefix, env)
        sources.append(
            DataSource(
                name=name,
                dialect=dialect,
                description=env.get(f"{prefix}DESCRIPTION", "").strip(),
                prefix=prefix,
                environ=env,
            )
        )

    # A dialect's unprefixed variables are the defaults its named sources
    # share. Once it has a named source they are not a source of their own:
    # listing one would offer a target nobody named.
    named_dialects = {source.dialect for source in sources}
    for dialect in DIALECTS:
        if _normalized(dialect) in seen or dialect in named_dialects:
            continue
        implicit = implicit_datasource(dialect, env)
        if any(implicit.getenv(v) for v in _PRESENCE_VARIABLES[dialect]):
            sources.append(implicit)

    return sources


def implicit_datasource(dialect: str, environ: Mapping[str, str] | None = None) -> DataSource:
    """The source of a dialect configured without a name: its unprefixed variables."""
    return DataSource(
        name=dialect,
        dialect=dialect,
        environ=os.environ if environ is None else environ,
    )


def find_datasource(name: str, sources: list[DataSource]) -> DataSource:
    """The configured source a person means by *name*.

    Case, spaces, dashes and underscores are ignored, so ``Shop EU`` finds
    ``shop-eu``.

    Raises:
        DataSourceNotFoundError: No source has that name; the message lists
            the configured ones.
    """
    wanted = _normalized(name)
    for source in sources:
        if _normalized(source.name) == wanted:
            return source
    listed = ", ".join(source.name for source in sources) or "none"
    raise DataSourceNotFoundError(
        f"No data source named '{name}'. Configured data sources: {listed}"
    )

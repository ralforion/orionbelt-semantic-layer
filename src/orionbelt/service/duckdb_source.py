"""Connections to a DuckDB data source, opened alike on every surface.

REST, pgwire and Flight SQL all open a DuckDB source here, so its settings
apply wherever it is queried:

- ``DUCKDB_INIT_SQL``: statements run once for each DuckDB instance of the
  source, for what its queries share: loading an extension, a ``CREATE
  SECRET`` for object storage or a catalog, an ``ATTACH`` of an Iceberg
  catalog, a view. A source without a database file runs them once for the
  life of the process, so a catalog's OAuth2 secret asks its token server
  once and DuckDB renews the token as it expires; a database file, opened
  for each query, runs them for each query.
- ``DUCKDB_CONNECTION_SQL``: statements run on every connection, for what
  lasts only for the connection that ran it: ``SET search_path``, ``USE``,
  a ``TEMP`` view.
- ``DUCKDB_MEMORY_LIMIT`` and ``DUCKDB_THREADS``: DuckDB's ``memory_limit``
  and ``threads``, of the memory and CPUs DuckDB sees (in a container, the
  container's limits). Unset, the limit is half the memory, not DuckDB's
  80%: what DuckDB holds is not all the process does, and four queries at
  once in a 1 GB container at 80% got the server killed for memory, where at
  50% they spilled and finished.
- ``DUCKDB_SPILL_DIR`` and ``DUCKDB_SPILL_MAX_MB``: where DuckDB writes the
  joins, aggregates and sorts that outgrow its memory limit, and how much.
- ``DUCKDB_ALLOWED_PATHS``: the directories, files and URL prefixes the
  source's queries may read besides its database. A model names files in a
  data object's ``code``, so without a list it could name any file the server
  can read. Unset, a source reads its database alone (a MotherDuck database,
  remote, keeps reading as before); ``*`` lifts the limit. After the setup
  statements, which may still load extensions and attach catalogs, DuckDB's
  ``enable_external_access`` is turned off with these paths allowed. The
  setting is the instance's and cannot be turned back on while it is open,
  so the first source to open a database file decides its paths until the
  server stops, and a source of the file with other paths is refused.

A source with no database file (``DUCKDB_DATABASE`` unset, ``:memory:`` or a
named ``:memory:name``) keeps one DuckDB instance for the life of the
process, and each query runs on a connection of its own to it. The instance
is the source's settings' own: sources with the same settings share it, and
no other connection reaches it by a name. The queries running at once then share one
memory limit, rather than each claiming the default 80% of the container in
an instance of its own, and what the setup statements attach stays attached.
A database file is opened read-only for each query, as before: holding it
open would lock out a process that writes to it. Queries that overlap share
DuckDB's instance of the file, so the sources reading one file must agree on
their memory, thread and spill settings.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from orionbelt.service.db_executor import ExecutionUnavailableError
from orionbelt.service.federated_executor import spill_directory

if TYPE_CHECKING:
    import duckdb


class _Source(Protocol):
    """What is read of a data source: an ``ob_flight`` ``DataSource``, or an
    :class:`_ImplicitSource` where the router predates them."""

    @property
    def name(self) -> str: ...

    @property
    def prefix(self) -> str: ...

    def credentials(self) -> dict[str, Any]: ...

    def getenv(self, variable: str) -> str | None: ...

    def fingerprint(self) -> str: ...


@dataclass(frozen=True)
class _ImplicitSource:
    """A dialect's implicit source through an ob-flight-extension older than named
    data sources (3.0.0): its router offers ``get_credentials`` by dialect alone,
    and its variables are the unprefixed ones."""

    name: str
    prefix: str = ""

    def credentials(self) -> dict[str, Any]:
        from ob_flight.db_router import get_credentials

        credentials: dict[str, Any] = get_credentials(self.name)
        return credentials

    def getenv(self, variable: str) -> str | None:
        return os.environ.get(variable)

    def fingerprint(self) -> str:
        return self.name


#: ``memory_limit`` of a source that sets none: see the module's docstring.
DEFAULT_MEMORY_LIMIT = "50%"


@dataclass(frozen=True)
class DuckDBSettings:
    """How a DuckDB data source is opened.

    Attributes:
        database: The database file, a ``md:`` MotherDuck database, or
            ``:memory:``.
        init_sql: Statements run once for each DuckDB instance
            (``DUCKDB_INIT_SQL``). Never shown: a ``CREATE SECRET`` carries keys.
        connection_sql: Statements run on every connection
            (``DUCKDB_CONNECTION_SQL``). Never shown either.
        memory_limit: DuckDB's ``memory_limit`` (``DUCKDB_MEMORY_LIMIT``),
            :data:`DEFAULT_MEMORY_LIMIT` when unset.
        threads: DuckDB's ``threads`` (``DUCKDB_THREADS``).
        spill_dir: ``DUCKDB_SPILL_DIR`` as configured: a directory, ``off``,
            or ``None`` for the default.
        spill_max_mb: The most DuckDB may spill (``DUCKDB_SPILL_MAX_MB``).
        allowed_paths: What queries may read besides the database
            (``DUCKDB_ALLOWED_PATHS``); ``None`` for no limit.
    """

    database: str
    init_sql: str | None = field(default=None, repr=False)
    connection_sql: str | None = field(default=None, repr=False)
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    threads: int | None = None
    spill_dir: str | None = None
    spill_max_mb: int | None = None
    allowed_paths: tuple[str, ...] | None = ()

    @property
    def in_memory(self) -> bool:
        """Whether the source has no database file."""
        return self.database.startswith(":memory:")


def settings(source: _Source) -> DuckDBSettings:
    """A DuckDB source's settings, from its variables.

    Raises:
        ExecutionUnavailableError: A count is not a positive whole number.
    """
    credentials = source.credentials()
    database = str(credentials.get("database") or ":memory:")
    return DuckDBSettings(
        database=database,
        init_sql=_text(credentials.get("init_sql")),
        connection_sql=_text(source.getenv("DUCKDB_CONNECTION_SQL")),
        memory_limit=_text(source.getenv("DUCKDB_MEMORY_LIMIT")) or DEFAULT_MEMORY_LIMIT,
        threads=_count(source, "DUCKDB_THREADS"),
        spill_dir=_text(source.getenv("DUCKDB_SPILL_DIR")),
        spill_max_mb=_count(source, "DUCKDB_SPILL_MAX_MB"),
        allowed_paths=_allowed_paths(source.getenv("DUCKDB_ALLOWED_PATHS"), database),
    )


def connect(source: _Source | str) -> duckdb.DuckDBPyConnection:
    """A new connection to a DuckDB source, set up.

    The instance's setup statements have run when it was opened (for a
    database file, now), and the connection's run here. A dialect name means
    its implicit source. The caller closes the connection; closing it leaves
    a kept instance open.

    Raises:
        ExecutionUnavailableError: A setting is invalid, the setup statements
            failed, or another source holds the database file with other
            settings.
    """
    datasource = _source(source)
    config = settings(datasource)
    con = _kept_cursor(datasource, config) if config.in_memory else _open_file(datasource, config)
    try:
        prepare(con, datasource, config)
    except Exception:
        con.close()
        raise
    return con


def prepare(con: duckdb.DuckDBPyConnection, source: _Source, config: DuckDBSettings) -> None:
    """Set up a new connection of a set-up instance: its connection statements.

    A Flight cursor is a connection of its own and is set up here too. One
    connection of an instance at a time: run at once, two ``CREATE OR
    REPLACE VIEW``s conflict in DuckDB's catalog (``Catalog write-write
    conflict``).

    Raises:
        ExecutionUnavailableError: The statements failed.
    """
    if config.connection_sql:
        with _setup_lock(config):
            _run(con, source, config.connection_sql, "DUCKDB_CONNECTION_SQL")


def _set_up_instance(
    con: duckdb.DuckDBPyConnection, source: _Source, config: DuckDBSettings
) -> None:
    """Run a new instance's setup statements, then limit the paths it reads.

    Called under the instance's setup lock: DuckDB refuses to set the allowed
    paths of an instance another connection limited in between.

    Raises:
        ExecutionUnavailableError: The setup statements failed.
    """
    if config.init_sql:
        _run(con, source, config.init_sql, "DUCKDB_INIT_SQL")
    _limit_paths(con, config)


def _run(con: duckdb.DuckDBPyConnection, source: _Source, sql: str, variable: str) -> None:
    """Run a source's statements, failing without repeating them.

    Raises:
        ExecutionUnavailableError: They failed.
    """
    import duckdb

    try:
        con.execute(sql)
    except duckdb.Error as exc:
        # ``from None``: the chained error would carry the statements, and
        # with them any key a CREATE SECRET holds, into the logs.
        raise ExecutionUnavailableError(_init_failed(source, exc, variable)) from None


def close_instances() -> None:
    """Close the instances kept for sources without a database file, at shutdown.

    The paths each database file was claimed with go too.
    """
    with _instances_lock:
        instances = list(_instances.values())
        _instances.clear()
        _claims.clear()
    for instance in instances:
        instance.close()


class _Instance:
    """The DuckDB instance a source without a database file keeps, and its spill directory."""

    def __init__(self, source: _Source, config: DuckDBSettings) -> None:
        base = spill_directory(config.spill_dir)
        # A directory of its own: DuckDB deletes a spill directory it created
        # when its instance closes, files of other instances in it included.
        self.spill_dir = _spill_root(source, base, unique=True) if base else None
        try:
            self.root = _open(source, config, read_only=False, spill_dir=self.spill_dir or "")
        except Exception:
            self._remove_spill_dir()
            raise
        try:
            _set_up_instance(self.root, source, config)
        except Exception:
            self.close()
            raise

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self.root.cursor()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.root.close()
        self._remove_spill_dir()

    def _remove_spill_dir(self) -> None:
        if self.spill_dir is not None:
            shutil.rmtree(self.spill_dir, ignore_errors=True)


_instances: dict[DuckDBSettings, _Instance] = {}
_instances_lock = threading.Lock()
_setup_locks: dict[object, threading.Lock] = {}

#: The allowed paths each database file is read with, for the life of the
#: process, by its real path (:func:`_claim_paths`); ``None`` for no limit.
_claims: dict[object, frozenset[str] | None] = {}


def _instance_key(config: DuckDBSettings) -> object:
    """Which DuckDB instance a connection is of: a kept instance's settings, or a database.

    Sources of one database file share DuckDB's instance of it while their
    queries overlap, so it is the file's, by its real path: DuckDB shares it
    between a relative and an absolute spelling of the path.
    """
    if config.in_memory:
        return config
    if config.database.startswith("md:"):
        return config.database
    return os.path.realpath(os.path.expanduser(config.database))


def _setup_lock(config: DuckDBSettings) -> threading.Lock:
    """The lock of one DuckDB instance's setup."""
    with _instances_lock:
        return _setup_locks.setdefault(_instance_key(config), threading.Lock())


def _kept_cursor(source: _Source, config: DuckDBSettings) -> duckdb.DuckDBPyConnection:
    """A new connection to the instance kept for these settings, opened on first use.

    Sources with the same settings read the same rows, so they share one.
    Opened under the instance's setup lock alone, so a slow catalog holds up
    the source's first queries and no other source's. The connection is made
    under the lock too: DuckDB does not promise that ``cursor()`` may be
    called from several threads at once.
    """
    with _setup_lock(config):
        with _instances_lock:
            instance = _instances.get(config)
        if instance is None:
            instance = _Instance(source, config)
            with _instances_lock:
                _instances[config] = instance
        return instance.cursor()


def _open_file(source: _Source, config: DuckDBSettings) -> duckdb.DuckDBPyConnection:
    """A read-only connection to the source's database file (or MotherDuck database)."""
    import duckdb

    _claim_paths(source, config)
    spill_dir: str | None = None
    if config.spill_dir is not None:
        if config.spill_dir.lower() == "off":
            spill_dir = ""
        else:
            # One directory per source, created here and not by DuckDB, which
            # would delete it with every file in it when its instance closes.
            spill_dir = _spill_root(
                source, os.path.join(config.spill_dir, f"obsl-duckdb-{source.fingerprint()}")
            )
    try:
        con = _open(source, config, read_only=True, spill_dir=spill_dir)
    except duckdb.ConnectionException as exc:
        if "different configuration" not in str(exc):
            raise
        raise ExecutionUnavailableError(
            f"Data source '{source.name}' opens {config.database} with other memory, "
            "thread or spill settings than another source reading the same file at "
            "the moment. Queries reading one file at once share one DuckDB instance, "
            "so give every source of the file the same DUCKDB_MEMORY_LIMIT, "
            "DUCKDB_THREADS and DUCKDB_SPILL_DIR."
        ) from None
    try:
        with _setup_lock(config):
            _set_up_instance(con, source, config)
    except Exception:
        con.close()
        raise
    return con


def _open(
    source: _Source, config: DuckDBSettings, *, read_only: bool, spill_dir: str | None
) -> duckdb.DuckDBPyConnection:
    """``duckdb.connect`` with the source's options.

    Raises:
        ExecutionUnavailableError: DuckDB refused an option.
    """
    import duckdb

    options = _duckdb_config(config, spill_dir)
    # A named in-memory database (``:memory:name``) is one DuckDB instance for
    # every connection naming it, whatever its source's settings, allowed
    # paths included. Each kept instance is a database of its own instead;
    # sources with the same settings share one through ``_instances``.
    database = ":memory:" if config.in_memory else config.database
    try:
        return duckdb.connect(database, read_only=read_only, config=options)
    except (duckdb.ParserException, duckdb.SyntaxException) as exc:
        raise ExecutionUnavailableError(
            f"DuckDB refused the settings of data source '{source.name}' "
            f"(DUCKDB_MEMORY_LIMIT, DUCKDB_THREADS, DUCKDB_SPILL_MAX_MB): "
            f"{str(exc).splitlines()[0]}"
        ) from None


def _spill_root(source: _Source, directory: str, *, unique: bool = False) -> str:
    """``directory``, created if missing; with ``unique``, a new directory inside it.

    Raises:
        ExecutionUnavailableError: The directory cannot be written.
    """
    try:
        os.makedirs(directory, exist_ok=True)
        return tempfile.mkdtemp(prefix="obsl-duckdb-", dir=directory) if unique else directory
    except OSError as exc:
        variable = f"{source.prefix}DUCKDB_SPILL_DIR"
        raise ExecutionUnavailableError(
            f"Data source '{source.name}' cannot spill into '{directory}' ({variable}): {exc}"
        ) from None


def _claim_paths(source: _Source, config: DuckDBSettings) -> None:
    """Fix a database file's allowed paths until the server stops, or refuse a source with others.

    Sources of one file share DuckDB's instance of it while their queries
    overlap, and DuckDB's limit is the instance's: the first connection to
    set it would decide what every other source of the file reads, and
    setting it would cut off one reading without a limit. So the first
    source to open a file decides its paths for the life of the process,
    and a source with other paths is refused before it connects. A kept
    instance needs no claim: its settings, paths included, are its own.

    Raises:
        ExecutionUnavailableError: The file is read with other paths.
    """
    wanted = None if config.allowed_paths is None else frozenset(config.allowed_paths)
    with _instances_lock:
        claimed = _claims.setdefault(_instance_key(config), wanted)
    if claimed != wanted:
        raise ExecutionUnavailableError(
            f"Data source '{source.name}' reads {config.database} with other allowed paths "
            f"({source.prefix}DUCKDB_ALLOWED_PATHS) than the source that opened it first. "
            "DuckDB limits the paths of a file for every source reading it at once, so the "
            "first source to open a file decides them until the server restarts: give "
            "every source of the file the same DUCKDB_ALLOWED_PATHS."
        )


def _limit_paths(con: duckdb.DuckDBPyConnection, config: DuckDBSettings) -> None:
    """Let the instance read only its database and the source's allowed paths, from now on.

    Called under the instance's setup lock. An instance already limited was
    limited to these paths: a kept instance by its own settings, a database
    file by the source that claimed it (:func:`_claim_paths`).
    """
    if config.allowed_paths is None:
        return
    access = con.execute("SELECT current_setting('enable_external_access')").fetchall()
    if not access[0][0]:
        return
    files = sorted(path for path in config.allowed_paths if os.path.isfile(path))
    directories = sorted(set(config.allowed_paths).difference(files))
    if directories:
        con.execute(f"SET allowed_directories = {_sql_list(directories)}")
    if files:
        con.execute(f"SET allowed_paths = {_sql_list(files)}")
    con.execute("SET enable_external_access = false")


def _sql_list(values: list[str]) -> str:
    """A SQL list of string literals."""
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"[{quoted}]"


def _allowed_paths(raw: str | None, database: str) -> tuple[str, ...] | None:
    """``DUCKDB_ALLOWED_PATHS`` as a tuple; ``None`` for no limit.

    ``*`` is no limit, as is a MotherDuck database without the setting:
    its data is remote, not in files beside the server.
    """
    text = _text(raw)
    if text is None:
        return None if database.startswith("md:") else ()
    if text == "*":
        return None
    return tuple(path for path in (part.strip() for part in text.split(",")) if path)


def _duckdb_config(config: DuckDBSettings, spill_dir: str | None) -> dict[str, Any]:
    """DuckDB's own options for these settings.

    ``spill_dir`` ``None`` leaves DuckDB's default spill directory (beside a
    database file); ``""`` turns spilling off.
    """
    options: dict[str, Any] = {"memory_limit": config.memory_limit}
    if config.threads is not None:
        options["threads"] = config.threads
    if spill_dir is not None:
        options["temp_directory"] = spill_dir
    if spill_dir != "" and config.spill_max_mb is not None:
        # Whichever directory spills: DuckDB's default beside a database too.
        options["max_temp_directory_size"] = f"{config.spill_max_mb}MiB"
    return options


def _init_failed(source: _Source, exc: duckdb.Error, variable: str) -> str:
    """What failed in a source's statements, without repeating them."""
    import duckdb

    if isinstance(exc, duckdb.ParserException):
        # A parser error quotes the text around the error, which can be a key.
        reason = "DuckDB could not parse them"
    else:
        reason = str(exc).splitlines()[0]
    when = (
        "every connection"
        if variable == "DUCKDB_CONNECTION_SQL"
        else "each new DuckDB instance of the source, and for a database file on each query"
    )
    return (
        f"The setup statements of data source '{source.name}' ({source.prefix}{variable}) "
        f"failed: {reason}. They run on {when}, so write them to run again: "
        "ATTACH IF NOT EXISTS, CREATE OR REPLACE SECRET, CREATE OR REPLACE VIEW."
    )


def _source(source: _Source | str) -> _Source:
    """A data source, or the implicit source of a dialect name."""
    if not isinstance(source, str):
        return source
    try:
        from ob_flight.db_router import implicit_datasource
    except ImportError:
        return _ImplicitSource(source)
    return implicit_datasource(source)


def _text(value: Any) -> str | None:
    """A setting's value, ``None`` when unset or blank."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _count(source: _Source, variable: str) -> int | None:
    """A positive whole-number setting, ``None`` when unset."""
    raw = _text(source.getenv(variable))
    if raw is None:
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise ExecutionUnavailableError(
            f"{source.prefix}{variable} is '{raw}', but must be a whole number above 0"
        )
    return int(raw)


__all__ = [
    "DEFAULT_MEMORY_LIMIT",
    "DuckDBSettings",
    "close_instances",
    "connect",
    "prepare",
    "settings",
]

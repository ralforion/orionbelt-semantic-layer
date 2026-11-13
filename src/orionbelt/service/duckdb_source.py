"""Connections to a DuckDB data source, opened alike on every surface.

REST, pgwire and Flight SQL all open a DuckDB source here, so its settings
apply wherever it is queried:

- ``DUCKDB_INIT_SQL``: statements run on every new connection before its
  query, for what a connection needs to read lake tables and files: loading
  an extension, a ``CREATE SECRET`` for object storage, an ``ATTACH`` of an
  Iceberg catalog, a ``SET search_path``. They run again on each connection,
  so they are written to run twice (``IF NOT EXISTS``, ``OR REPLACE``).
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
  setting is the instance's and cannot be turned back on while it is open.

A source with no database file (``DUCKDB_DATABASE`` unset or ``:memory:``)
keeps one DuckDB instance for the life of the process, and each query runs
on a connection of its own to it. The queries running at once then share one
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
        init_sql: Statements run on every new connection (``DUCKDB_INIT_SQL``).
            Never shown: a ``CREATE SECRET`` carries keys.
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
        memory_limit=_text(source.getenv("DUCKDB_MEMORY_LIMIT")) or DEFAULT_MEMORY_LIMIT,
        threads=_count(source, "DUCKDB_THREADS"),
        spill_dir=_text(source.getenv("DUCKDB_SPILL_DIR")),
        spill_max_mb=_count(source, "DUCKDB_SPILL_MAX_MB"),
        allowed_paths=_allowed_paths(source.getenv("DUCKDB_ALLOWED_PATHS"), database),
    )


def connect(source: _Source | str) -> duckdb.DuckDBPyConnection:
    """A new connection to a DuckDB source, its setup statements run.

    A dialect name means its implicit source. The caller closes the
    connection; closing it leaves a kept instance open.

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
    """Set up a new connection: its setup statements, then the limit on paths.

    One connection of an instance at a time. Run at once, the setup
    statements of two connections conflict in DuckDB's catalog (``Catalog
    write-write conflict`` on a ``CREATE OR REPLACE VIEW``), and DuckDB
    refuses to set the allowed paths of an instance another connection
    restricted in between. A Flight cursor is a connection of its own and is
    set up here too.

    Raises:
        ExecutionUnavailableError: The setup statements failed.
    """
    import duckdb

    with _setup_lock(config):
        if config.init_sql:
            try:
                con.execute(config.init_sql)
            except duckdb.Error as exc:
                # ``from None``: the chained error would carry the statements,
                # and with them any key a CREATE SECRET holds, into the logs.
                raise ExecutionUnavailableError(_init_failed(source, exc)) from None
        _limit_paths(con, source, config)


def close_instances() -> None:
    """Close the instances kept for sources without a database file, at shutdown."""
    with _instances_lock:
        instances = list(_instances.values())
        _instances.clear()
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

#: The paths each instance was limited to (:func:`_limit_paths`), by
#: :func:`_instance_key`; ``None`` for no limit. Written under the instance's
#: setup lock, when a connection limits an instance no connection limited.
_limits: dict[object, frozenset[str] | None] = {}


def _instance_key(config: DuckDBSettings) -> object:
    """Which DuckDB instance a connection is of: a kept instance's settings, or a database.

    Sources of one database file share DuckDB's instance of it while their
    queries overlap, so it is the file's.
    """
    return config if config.in_memory else config.database


def _setup_lock(config: DuckDBSettings) -> threading.Lock:
    """The lock of one DuckDB instance's setup."""
    with _instances_lock:
        return _setup_locks.setdefault(_instance_key(config), threading.Lock())


def _kept_cursor(source: _Source, config: DuckDBSettings) -> duckdb.DuckDBPyConnection:
    """A new connection to the instance kept for these settings, opened on first use.

    Sources with the same settings read the same rows, so they share one.
    The connection is made under the lock too: DuckDB does not promise that
    ``cursor()`` may be called from several threads at once.
    """
    with _instances_lock:
        instance = _instances.get(config)
        if instance is None:
            instance = _instances[config] = _Instance(source, config)
        return instance.cursor()


def _open_file(source: _Source, config: DuckDBSettings) -> duckdb.DuckDBPyConnection:
    """A read-only connection to the source's database file (or MotherDuck database)."""
    import duckdb

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
        return _open(source, config, read_only=True, spill_dir=spill_dir)
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


def _open(
    source: _Source, config: DuckDBSettings, *, read_only: bool, spill_dir: str | None
) -> duckdb.DuckDBPyConnection:
    """``duckdb.connect`` with the source's options.

    Raises:
        ExecutionUnavailableError: DuckDB refused an option.
    """
    import duckdb

    options = _duckdb_config(config, spill_dir)
    try:
        return duckdb.connect(config.database, read_only=read_only, config=options)
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


def _limit_paths(con: duckdb.DuckDBPyConnection, source: _Source, config: DuckDBSettings) -> None:
    """Let the instance read only its database and the source's allowed paths, from now on.

    Called under the instance's setup lock. DuckDB's limit is the instance's,
    and stays as it was first set. A database file that other queries hold
    open is one instance for every source reading it, so a source whose
    paths differ from the ones it was limited to is refused: it would read
    with another source's paths, more or fewer than its own.

    Raises:
        ExecutionUnavailableError: The instance is limited to other paths.
    """
    wanted = None if config.allowed_paths is None else frozenset(config.allowed_paths)
    key = _instance_key(config)
    access = con.execute("SELECT current_setting('enable_external_access')").fetchall()
    if not access[0][0]:
        if key in _limits and _limits[key] == wanted:
            return
        raise ExecutionUnavailableError(
            f"Data source '{source.name}' reads {config.database} with other allowed "
            "paths than another source reading it at the moment. Queries reading one "
            "file at once share one DuckDB instance, which keeps the paths it was "
            "first limited to, so give every source of the file the same "
            "DUCKDB_ALLOWED_PATHS."
        )
    if wanted is None:
        return
    files = sorted(path for path in wanted if os.path.isfile(path))
    directories = sorted(wanted.difference(files))
    if directories:
        con.execute(f"SET allowed_directories = {_sql_list(directories)}")
    if files:
        con.execute(f"SET allowed_paths = {_sql_list(files)}")
    con.execute("SET enable_external_access = false")
    _limits[key] = wanted


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


def _init_failed(source: _Source, exc: duckdb.Error) -> str:
    """What failed in the source's setup statements, without repeating them."""
    import duckdb

    variable = f"{source.prefix}DUCKDB_INIT_SQL"
    if isinstance(exc, duckdb.ParserException):
        # A parser error quotes the text around the error, which can be a key.
        reason = "DuckDB could not parse them"
    else:
        reason = str(exc).splitlines()[0]
    return (
        f"The setup statements of data source '{source.name}' ({variable}) failed: "
        f"{reason}. They run on every new connection, so write them to run again: "
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

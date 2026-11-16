"""What a data source holds: its schemas, tables, columns and keys.

Ported from OrionBelt Analytics' database drivers, which reflect a schema
through SQLAlchemy. OBSL has no SQLAlchemy: it reaches a database through the
native connections ``ob_flight.db_router`` pools, the same ones a query runs
on. So the port keeps OBA's catalog knowledge -- which schemas are the
engine's own, where each engine keeps its comments, views and keys -- and asks
it as plain catalog SQL through :func:`~orionbelt.service.db_executor.execute_sql`.

A schema is read in a handful of queries, one per kind of fact (tables,
columns, primary keys, foreign keys, view bodies), never one per table: a
schema of a hundred tables costs the same five round trips as a schema of one.

Tables and columns are the answer; keys and view bodies only add to it. A
catalog that will not show its constraints to this user (a permissions matter
on several engines) costs the answer its keys, with a warning saying so, not
the whole schema -- the rule OBA's discovery follows for an unreadable table.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from orionbelt.dialect import DialectRegistry
from orionbelt.service.db_executor import ExecutionError, execute_sql

if TYPE_CHECKING:
    from ob_flight.datasources import DataSource

    from orionbelt.dialect.base import Dialect

logger = logging.getLogger(__name__)

#: One row of a catalog query, keyed by the canonical column names below.
Row = dict[str, Any]

#: Runs one catalog statement and returns its rows; :func:`execute_sql` on the source.
Runner = Callable[[str], list[Row]]

TableKind = Literal["table", "view", "materialized_view"]


# ---------------------------------------------------------------------------
# What is returned
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnInfo:
    """A column as the database declares it.

    ``ordinal`` counts from 1 on every engine (Databricks counts from 0);
    ``nullable`` is ``None`` where the catalog does not say.
    """

    name: str
    data_type: str
    ordinal: int
    nullable: bool | None = None
    comment: str | None = None
    primary_key: bool = False


@dataclass(frozen=True)
class ForeignKeyInfo:
    """A foreign key; its columns pair with ``referred_columns`` by position."""

    name: str | None
    columns: tuple[str, ...]
    referred_schema: str | None
    referred_table: str
    referred_columns: tuple[str, ...]


@dataclass
class TableInfo:
    """A table or view and what the catalog knows of it.

    ``kind`` is ``table``, ``view`` or ``materialized_view``.
    """

    schema: str
    name: str
    kind: TableKind
    comment: str | None = None
    columns: list[ColumnInfo] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)
    view_definition: str | None = None


@dataclass
class SchemaInfo:
    """A schema's tables, and what of the catalog could not be read."""

    schema: str
    tables: list[TableInfo] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Catalog SQL per engine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogQuery:
    """A catalog statement and how its result columns map to canonical names.

    Most statements alias their columns to the canonical names themselves.
    ``SHOW`` commands cannot, so ``rename`` maps their columns instead.
    """

    sql: str
    rename: Mapping[str, str] = field(default_factory=dict)


class Catalog:
    """Where one engine keeps the metadata of a schema.

    Each statement returns rows under canonical column names:

    * schemas: ``schema_name``
    * tables: ``table_name``, ``table_type``, ``comment``
    * columns: ``table_name``, ``column_name``, ``data_type``, ``is_nullable``,
      ``ordinal``, ``comment``
    * primary keys: ``table_name``, ``column_name``, ``ordinal``
    * foreign keys: ``constraint_name``, ``table_name``, ``column_name``,
      ``ordinal``, ``referred_schema``, ``referred_table``, ``referred_column``
    * views: ``table_name``, ``definition``

    A statement an engine has no answer for returns ``None``.
    """

    dialect: ClassVar[str]

    def __init__(self, source: DataSource | None = None) -> None:
        self.source = source
        self._sql: Dialect = DialectRegistry.get(self.dialect)

    def lit(self, value: str) -> str:
        """*value* as a string literal of this engine."""
        return self._sql.quote_string_literal(value)

    def ident(self, name: str) -> str:
        """*name* as a quoted identifier of this engine."""
        return self._sql.quote_identifier(name)

    def only(self, column: str, tables: Collection[str] | None) -> str:
        """``AND column IN (...)`` restricting a statement to *tables*; empty for all."""
        if not tables:
            return ""
        return f" AND {column} IN ({', '.join(self.lit(t) for t in sorted(tables))})"

    def schemas(self) -> CatalogQuery:
        raise NotImplementedError

    def tables(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        raise NotImplementedError

    def columns(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        raise NotImplementedError

    def primary_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery | None:
        return None

    def foreign_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery | None:
        return None

    def views(self, schema: str, tables: Collection[str] | None) -> CatalogQuery | None:
        return None


_CATALOGS: dict[str, type[Catalog]] = {}


def _register(cls: type[Catalog]) -> type[Catalog]:
    _CATALOGS[cls.dialect] = cls
    return cls


def _excluded(catalog: Catalog, column: str, names: Collection[str]) -> str:
    return f"{column} NOT IN ({', '.join(catalog.lit(n) for n in sorted(names))})"


@_register
class PostgresCatalog(Catalog):
    """``pg_catalog``, not ``information_schema``.

    ``information_schema`` shows a user only the objects it owns or holds a
    privilege on *by its own role*, hides check-free constraints of other
    owners, and has no view for materialized views or comments.
    """

    dialect = "postgres"
    _SYSTEM = ("information_schema", "pg_catalog", "pg_toast")
    _RELKINDS = "('r', 'p', 'v', 'm', 'f')"

    def _relations(self, schema: str) -> str:
        return (
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = {self.lit(schema)} AND c.relkind IN {self._RELKINDS} "
            "AND NOT c.relispartition"
        )

    def schemas(self) -> CatalogQuery:
        return CatalogQuery(
            "SELECT nspname AS schema_name FROM pg_catalog.pg_namespace "
            f"WHERE {_excluded(self, 'nspname', self._SYSTEM)} "
            "AND nspname NOT LIKE 'pg\\_temp\\_%' AND nspname NOT LIKE 'pg\\_toast\\_temp\\_%' "
            "AND has_schema_privilege(oid, 'USAGE') ORDER BY nspname"
        )

    def tables(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT c.relname AS table_name, "
            "CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' "
            "ELSE 'BASE TABLE' END AS table_type, "
            "obj_description(c.oid, 'pg_class') AS comment "
            f"{self._relations(schema)}{self.only('c.relname', tables)} ORDER BY c.relname"
        )

    def columns(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT c.relname AS table_name, a.attname AS column_name, "
            "format_type(a.atttypid, a.atttypmod) AS data_type, "
            "NOT a.attnotnull AS is_nullable, a.attnum AS ordinal, "
            "col_description(c.oid, a.attnum) AS comment "
            "FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = {self.lit(schema)} AND c.relkind IN {self._RELKINDS} "
            "AND NOT c.relispartition AND a.attnum > 0 AND NOT a.attisdropped"
            f"{self.only('c.relname', tables)} ORDER BY c.relname, a.attnum"
        )

    def primary_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT c.relname AS table_name, a.attname AS column_name, k.ord AS ordinal "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum "
            f"WHERE con.contype = 'p' AND n.nspname = {self.lit(schema)}"
            f"{self.only('c.relname', tables)} ORDER BY c.relname, k.ord"
        )

    def foreign_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT con.conname AS constraint_name, c.relname AS table_name, "
            "a.attname AS column_name, k.ord AS ordinal, rn.nspname AS referred_schema, "
            "rc.relname AS referred_table, ra.attname AS referred_column "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class c ON c.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_catalog.pg_class rc ON rc.oid = con.confrelid "
            "JOIN pg_catalog.pg_namespace rn ON rn.oid = rc.relnamespace "
            "CROSS JOIN LATERAL unnest(con.conkey, con.confkey) "
            "WITH ORDINALITY AS k(attnum, refnum, ord) "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum "
            "JOIN pg_catalog.pg_attribute ra "
            "ON ra.attrelid = con.confrelid AND ra.attnum = k.refnum "
            f"WHERE con.contype = 'f' AND n.nspname = {self.lit(schema)}"
            f"{self.only('c.relname', tables)} ORDER BY c.relname, con.conname, k.ord"
        )

    def views(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        # pg_get_viewdef, not pg_views: it covers materialized views too.
        return CatalogQuery(
            "SELECT c.relname AS table_name, pg_get_viewdef(c.oid, true) AS definition "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE n.nspname = {self.lit(schema)} AND c.relkind IN ('v', 'm')"
            f"{self.only('c.relname', tables)}"
        )


@_register
class DuckDBCatalog(Catalog):
    """DuckDB's ``duckdb_*()`` table functions, kept to the current database.

    ``duckdb_views()`` carries the full ``CREATE VIEW`` in ``sql`` and
    ``duckdb_constraints()`` the key columns as lists, which ``unnest`` pairs
    position by position.
    """

    dialect = "duckdb"
    _SYSTEM = ("information_schema", "pg_catalog")

    def _in(self, schema: str) -> str:
        return f"database_name = current_database() AND schema_name = {self.lit(schema)}"

    def schemas(self) -> CatalogQuery:
        return CatalogQuery(
            "SELECT DISTINCT schema_name FROM duckdb_schemas() "
            "WHERE database_name = current_database() "
            f"AND {_excluded(self, 'schema_name', self._SYSTEM)} ORDER BY schema_name"
        )

    def tables(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT table_name, 'BASE TABLE' AS table_type, comment FROM duckdb_tables() "
            f"WHERE {self._in(schema)}{self.only('table_name', tables)} "
            "UNION ALL SELECT view_name, 'VIEW', comment FROM duckdb_views() "
            f"WHERE NOT internal AND {self._in(schema)}{self.only('view_name', tables)} "
            "ORDER BY table_name"
        )

    def columns(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT table_name, column_name, data_type, is_nullable, "
            "column_index AS ordinal, comment FROM duckdb_columns() "
            f"WHERE {self._in(schema)}{self.only('table_name', tables)} "
            "ORDER BY table_name, column_index"
        )

    def primary_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT table_name, unnest(constraint_column_names) AS column_name, "
            "generate_subscripts(constraint_column_names, 1) AS ordinal "
            "FROM duckdb_constraints() WHERE constraint_type = 'PRIMARY KEY' "
            f"AND {self._in(schema)}{self.only('table_name', tables)}"
        )

    def foreign_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        # A DuckDB foreign key can only refer to a table of its own schema.
        return CatalogQuery(
            "SELECT constraint_name, table_name, "
            "unnest(constraint_column_names) AS column_name, "
            "generate_subscripts(constraint_column_names, 1) AS ordinal, "
            "schema_name AS referred_schema, referenced_table AS referred_table, "
            "unnest(referenced_column_names) AS referred_column "
            "FROM duckdb_constraints() WHERE constraint_type = 'FOREIGN KEY' "
            f"AND {self._in(schema)}{self.only('table_name', tables)}"
        )

    def views(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT view_name AS table_name, sql AS definition FROM duckdb_views() "
            f"WHERE NOT internal AND {self._in(schema)}{self.only('view_name', tables)}"
        )


class _InformationSchemaCatalog(Catalog):
    """The SQL-standard ``information_schema`` of the connection's database.

    Its constraint views pair a foreign key's columns with the columns it
    refers to through ``position_in_unique_constraint``.
    """

    _SYSTEM: ClassVar[tuple[str, ...]] = ("information_schema",)
    _TABLES_COMMENT = "comment"
    _COLUMNS_COMMENT = "comment"
    _DATA_TYPE = "data_type"

    def _where(self, schema: str, alias: str = "") -> str:
        return f"{alias}table_schema = {self.lit(schema)}"

    def schemas(self) -> CatalogQuery:
        return CatalogQuery(
            "SELECT schema_name FROM information_schema.schemata "
            f"WHERE {_excluded(self, 'schema_name', self._SYSTEM)} ORDER BY schema_name"
        )

    def tables(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT table_name, table_type, "
            f"{self._TABLES_COMMENT} AS comment FROM information_schema.tables "
            f"WHERE {self._where(schema)}{self.only('table_name', tables)} ORDER BY table_name"
        )

    def columns(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            f"SELECT table_name, column_name, {self._DATA_TYPE} AS data_type, is_nullable, "
            f"ordinal_position AS ordinal, {self._COLUMNS_COMMENT} AS comment "
            f"FROM information_schema.columns WHERE {self._where(schema)}"
            f"{self.only('table_name', tables)} ORDER BY table_name, ordinal_position"
        )

    def primary_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery | None:
        return CatalogQuery(
            "SELECT k.table_name AS table_name, k.column_name AS column_name, "
            "k.ordinal_position AS ordinal "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage k "
            "ON k.constraint_schema = tc.constraint_schema "
            "AND k.constraint_name = tc.constraint_name AND k.table_name = tc.table_name "
            f"WHERE tc.constraint_type = 'PRIMARY KEY' AND {self._where(schema, 'tc.')}"
            f"{self.only('tc.table_name', tables)}"
        )

    def foreign_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery | None:
        return CatalogQuery(
            "SELECT rc.constraint_name AS constraint_name, k.table_name AS table_name, "
            "k.column_name AS column_name, k.ordinal_position AS ordinal, "
            "pk.table_schema AS referred_schema, pk.table_name AS referred_table, "
            "pk.column_name AS referred_column "
            "FROM information_schema.referential_constraints rc "
            "JOIN information_schema.key_column_usage k "
            "ON k.constraint_schema = rc.constraint_schema "
            "AND k.constraint_name = rc.constraint_name "
            "JOIN information_schema.key_column_usage pk "
            "ON pk.constraint_schema = rc.unique_constraint_schema "
            "AND pk.constraint_name = rc.unique_constraint_name "
            "AND pk.ordinal_position = k.position_in_unique_constraint "
            f"WHERE {self._where(schema, 'k.')}{self.only('k.table_name', tables)}"
        )

    def views(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT table_name, view_definition AS definition FROM information_schema.views "
            f"WHERE {self._where(schema)}{self.only('table_name', tables)}"
        )


@_register
class SnowflakeCatalog(_InformationSchemaCatalog):
    """The current database's ``information_schema``, and ``SHOW`` for keys.

    Snowflake's ``information_schema`` lists constraints but not their
    columns; ``SHOW PRIMARY KEYS`` / ``SHOW IMPORTED KEYS`` do, as OBA reads them.
    """

    dialect = "snowflake"
    _SYSTEM = ("INFORMATION_SCHEMA",)

    def _schema_ref(self, schema: str) -> str:
        # Unqualified, as information_schema is: the connection's current
        # database. SNOWFLAKE_DATABASE is written as a person spells it, and
        # quoting it would make it case-sensitive.
        return self.ident(schema)

    def primary_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            f"SHOW PRIMARY KEYS IN SCHEMA {self._schema_ref(schema)}",
            rename={"key_sequence": "ordinal"},
        )

    def foreign_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            f"SHOW IMPORTED KEYS IN SCHEMA {self._schema_ref(schema)}",
            rename={
                "fk_name": "constraint_name",
                "fk_table_name": "table_name",
                "fk_column_name": "column_name",
                "key_sequence": "ordinal",
                "pk_schema_name": "referred_schema",
                "pk_table_name": "referred_table",
                "pk_column_name": "referred_column",
            },
        )


@_register
class MySQLCatalog(_InformationSchemaCatalog):
    """MySQL's ``information_schema``, where a schema is a database.

    ``KEY_COLUMN_USAGE`` names the referred table and column of a foreign key
    on the key's own rows, and ``COLUMN_TYPE`` keeps the length and precision
    ``DATA_TYPE`` drops.
    """

    dialect = "mysql"
    _SYSTEM = ("information_schema", "mysql", "performance_schema", "sys")
    _TABLES_COMMENT = "table_comment"
    _COLUMNS_COMMENT = "column_comment"
    _DATA_TYPE = "column_type"

    def primary_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT table_name AS table_name, column_name AS column_name, "
            "ordinal_position AS ordinal FROM information_schema.key_column_usage "
            f"WHERE constraint_name = 'PRIMARY' AND {self._where(schema)}"
            f"{self.only('table_name', tables)}"
        )

    def foreign_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT constraint_name AS constraint_name, table_name AS table_name, "
            "column_name AS column_name, ordinal_position AS ordinal, "
            "referenced_table_schema AS referred_schema, "
            "referenced_table_name AS referred_table, "
            "referenced_column_name AS referred_column "
            "FROM information_schema.key_column_usage "
            f"WHERE referenced_table_name IS NOT NULL AND {self._where(schema)}"
            f"{self.only('table_name', tables)}"
        )


@_register
class DatabricksCatalog(_InformationSchemaCatalog):
    """The current catalog's Unity Catalog ``information_schema``.

    ``full_data_type`` keeps the precision and element types ``data_type``
    drops; ``ordinal_position`` counts from 0, which :func:`inspect_schema`
    renumbers.
    """

    dialect = "databricks"
    _DATA_TYPE = "full_data_type"


@_register
class DremioCatalog(_InformationSchemaCatalog):
    """Dremio's ``INFORMATION_SCHEMA``: no comments, no keys.

    A Dremio schema is a dotted source or space path (``lake.sales``); its
    ``sys`` schemas are the engine's own.
    """

    dialect = "dremio"

    def schemas(self) -> CatalogQuery:
        return CatalogQuery(
            "SELECT SCHEMA_NAME AS schema_name FROM INFORMATION_SCHEMA.SCHEMATA "
            "WHERE SCHEMA_NAME <> 'INFORMATION_SCHEMA' AND SCHEMA_NAME <> 'sys' "
            "AND SCHEMA_NAME NOT LIKE 'sys.%' ORDER BY SCHEMA_NAME"
        )

    def tables(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT TABLE_NAME AS table_name, TABLE_TYPE AS table_type "
            'FROM INFORMATION_SCHEMA."TABLES" '
            f"WHERE TABLE_SCHEMA = {self.lit(schema)}{self.only('TABLE_NAME', tables)} "
            "ORDER BY TABLE_NAME"
        )

    def columns(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, "
            "DATA_TYPE AS data_type, IS_NULLABLE AS is_nullable, "
            "ORDINAL_POSITION AS ordinal FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_SCHEMA = {self.lit(schema)}{self.only('TABLE_NAME', tables)} "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION"
        )

    def primary_keys(self, schema: str, tables: Collection[str] | None) -> None:
        return None

    def foreign_keys(self, schema: str, tables: Collection[str] | None) -> None:
        return None

    def views(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT TABLE_NAME AS table_name, VIEW_DEFINITION AS definition "
            "FROM INFORMATION_SCHEMA.VIEWS "
            f"WHERE TABLE_SCHEMA = {self.lit(schema)}{self.only('TABLE_NAME', tables)}"
        )


@_register
class ClickHouseCatalog(Catalog):
    """ClickHouse's ``system`` tables, where a schema is a database.

    ClickHouse has no foreign keys. Its primary key is the sparse index
    prefix of the sorting key, not a uniqueness constraint; it is reported
    as the table declares it.
    """

    dialect = "clickhouse"
    _SYSTEM = ("system", "INFORMATION_SCHEMA", "information_schema")

    def schemas(self) -> CatalogQuery:
        return CatalogQuery(
            "SELECT name AS schema_name FROM system.databases "
            f"WHERE {_excluded(self, 'name', self._SYSTEM)} ORDER BY name"
        )

    def tables(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT name AS table_name, engine AS table_type, comment FROM system.tables "
            f"WHERE database = {self.lit(schema)} AND NOT is_temporary"
            f"{self.only('name', tables)} ORDER BY name"
        )

    def columns(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        # A nullable string is usually LowCardinality(Nullable(String)): Nullable
        # cannot wrap LowCardinality, so it sits one level in.
        return CatalogQuery(
            "SELECT table AS table_name, name AS column_name, type AS data_type, "
            "startsWith(type, 'Nullable(') OR startsWith(type, 'LowCardinality(Nullable(') "
            "AS is_nullable, position AS ordinal, comment "
            f"FROM system.columns WHERE database = {self.lit(schema)}"
            f"{self.only('table', tables)} ORDER BY table, position"
        )

    def primary_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        # system.columns flags a key's columns, but its position is the column's
        # place in the table. The declared order is system.tables.primary_key, as
        # SQL ("b, a", "toDate(ts), id"): a column ranks by where its name first
        # appears there, backquoted or not.
        return CatalogQuery(
            "SELECT c.table AS table_name, c.name AS column_name, "
            "indexOf(arrayMap(x -> replaceAll(x, '`', ''), "
            "extractAll(t.primary_key, '`[^`]+`|[A-Za-z_][A-Za-z0-9_]*')), c.name) AS ordinal "
            "FROM system.columns AS c INNER JOIN system.tables AS t "
            "ON t.database = c.database AND t.name = c.table "
            f"WHERE c.database = {self.lit(schema)} AND c.is_in_primary_key"
            f"{self.only('c.table', tables)}"
        )

    def views(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT name AS table_name, as_select AS definition FROM system.tables "
            f"WHERE database = {self.lit(schema)} AND engine IN ('View', 'MaterializedView')"
            f"{self.only('name', tables)}"
        )


@_register
class BigQueryCatalog(_InformationSchemaCatalog):
    """Each dataset's own ``INFORMATION_SCHEMA``; the region's for the list of datasets.

    BigQuery keeps column descriptions in ``COLUMN_FIELD_PATHS`` and has
    unenforced primary and foreign keys. A foreign key is resolved within its
    own dataset, the only one the statement reads.
    """

    dialect = "bigquery"
    _SYSTEM = ("INFORMATION_SCHEMA", "information_schema")

    def _view(self, schema: str, view: str) -> str:
        return f"{self.ident(schema)}.INFORMATION_SCHEMA.{view}"

    def schemas(self) -> CatalogQuery:
        location = self.source.getenv("BIGQUERY_LOCATION") if self.source else None
        project = self.source.getenv("BIGQUERY_PROJECT") if self.source else None
        if location:
            scope = self.ident(f"region-{location.lower()}")
        elif project:
            scope = self.ident(project)
        else:
            scope = self.ident("region-us")
        return CatalogQuery(
            f"SELECT schema_name FROM {scope}.INFORMATION_SCHEMA.SCHEMATA "
            f"WHERE {_excluded(self, 'schema_name', self._SYSTEM)} ORDER BY schema_name"
        )

    def tables(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT table_name, table_type, CAST(NULL AS STRING) AS comment "
            f"FROM {self._view(schema, 'TABLES')} WHERE TRUE"
            f"{self.only('table_name', tables)} ORDER BY table_name"
        )

    def columns(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT c.table_name AS table_name, c.column_name AS column_name, "
            "c.data_type AS data_type, c.is_nullable AS is_nullable, "
            "c.ordinal_position AS ordinal, p.description AS comment "
            f"FROM {self._view(schema, 'COLUMNS')} c "
            f"LEFT JOIN {self._view(schema, 'COLUMN_FIELD_PATHS')} p "
            "ON p.table_name = c.table_name AND p.column_name = c.column_name "
            "AND p.field_path = c.column_name WHERE TRUE"
            f"{self.only('c.table_name', tables)} ORDER BY c.table_name, c.ordinal_position"
        )

    def primary_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT k.table_name AS table_name, k.column_name AS column_name, "
            "k.ordinal_position AS ordinal "
            f"FROM {self._view(schema, 'TABLE_CONSTRAINTS')} tc "
            f"JOIN {self._view(schema, 'KEY_COLUMN_USAGE')} k "
            "ON k.constraint_name = tc.constraint_name AND k.table_name = tc.table_name "
            f"WHERE tc.constraint_type = 'PRIMARY KEY'{self.only('tc.table_name', tables)}"
        )

    def foreign_keys(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        # CONSTRAINT_COLUMN_USAGE names the referred columns of a foreign key;
        # the referred table's own primary key gives their order.
        return CatalogQuery(
            "SELECT k.constraint_name AS constraint_name, k.table_name AS table_name, "
            "k.column_name AS column_name, k.ordinal_position AS ordinal, "
            "ccu.table_schema AS referred_schema, ccu.table_name AS referred_table, "
            "ccu.column_name AS referred_column "
            f"FROM {self._view(schema, 'TABLE_CONSTRAINTS')} tc "
            f"JOIN {self._view(schema, 'KEY_COLUMN_USAGE')} k "
            "ON k.constraint_name = tc.constraint_name AND k.table_name = tc.table_name "
            f"JOIN {self._view(schema, 'CONSTRAINT_COLUMN_USAGE')} ccu "
            "ON ccu.constraint_name = tc.constraint_name "
            f"JOIN {self._view(schema, 'KEY_COLUMN_USAGE')} pk "
            "ON pk.table_name = ccu.table_name AND pk.column_name = ccu.column_name "
            "AND pk.ordinal_position = k.position_in_unique_constraint "
            f"JOIN {self._view(schema, 'TABLE_CONSTRAINTS')} pkc "
            "ON pkc.constraint_name = pk.constraint_name AND pkc.table_name = pk.table_name "
            "AND pkc.constraint_type = 'PRIMARY KEY' "
            f"WHERE tc.constraint_type = 'FOREIGN KEY'{self.only('tc.table_name', tables)}"
        )

    def views(self, schema: str, tables: Collection[str] | None) -> CatalogQuery:
        return CatalogQuery(
            "SELECT table_name, view_definition AS definition "
            f"FROM {self._view(schema, 'VIEWS')} WHERE TRUE{self.only('table_name', tables)}"
        )


#: The dialects whose catalogs can be read.
SUPPORTED_DIALECTS: tuple[str, ...] = tuple(sorted(_CATALOGS))


class InspectionNotSupportedError(Exception):
    """The source's dialect has no catalog reader."""


def catalog_for(dialect: str, source: DataSource | None = None) -> Catalog:
    """The catalog reader of *dialect*.

    Raises:
        InspectionNotSupportedError: No reader exists for the dialect.
    """
    cls = _CATALOGS.get(dialect)
    if cls is None:
        raise InspectionNotSupportedError(
            f"Schema inspection is not available for dialect '{dialect}'; "
            f"supported: {', '.join(SUPPORTED_DIALECTS)}"
        )
    return cls(source)


# ---------------------------------------------------------------------------
# Reading a schema
# ---------------------------------------------------------------------------


def runner_for(source: DataSource) -> Runner:
    """Runs catalog statements on *source*, rows keyed by lower-cased column name."""

    def run(sql: str) -> list[Row]:
        result = execute_sql(sql, dialect=source.dialect, source=source)
        names = [column.name.lower() for column in result.columns]
        return [dict(zip(names, row, strict=True)) for row in result.rows]

    return run


def _rows(run: Runner, query: CatalogQuery) -> list[Row]:
    rows = run(query.sql)
    if not query.rename:
        return rows
    return [{query.rename.get(key, key): value for key, value in row.items()} for row in rows]


def _text(value: Any) -> str | None:
    """A catalog string, ``None`` for an absent or empty one."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _flag(value: Any) -> bool | None:
    """A catalog yes/no in any of the spellings engines use."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    return str(value).strip().upper() in {"YES", "Y", "TRUE", "T", "1"}


def _kind(table_type: Any) -> TableKind:
    """``table``, ``view`` or ``materialized_view`` from an engine's table type."""
    raw = str(table_type or "").upper().replace("_", " ")
    if "MATERIALIZED" in raw:
        return "materialized_view"
    if "VIEW" in raw:
        return "view"
    return "table"


def _ordinal(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def list_schemas(source: DataSource, *, run: Runner | None = None) -> list[str]:
    """The user schemas of *source*, without the engine's own.

    Raises:
        InspectionNotSupportedError: The source's dialect has no catalog reader.
        ExecutionUnavailableError: The source cannot be connected to.
        ExecutionError: The catalog query failed.
    """
    catalog = catalog_for(source.dialect, source)
    rows = _rows(run or runner_for(source), catalog.schemas())
    return [name for row in rows if (name := _text(row.get("schema_name")))]


def inspect_schema(
    source: DataSource,
    schema: str,
    *,
    tables: Collection[str] | None = None,
    include_views: bool = True,
    view_definitions: bool = False,
    run: Runner | None = None,
) -> SchemaInfo | None:
    """The tables of *schema* in *source*, with their columns and keys.

    Args:
        source: The data source to read.
        schema: The schema (a database on MySQL and ClickHouse, a dataset on
            BigQuery), spelled as :func:`list_schemas` returns it.
        tables: Only these tables; all when ``None`` or empty.
        include_views: Whether views and materialized views are listed.
        view_definitions: Whether a view's SQL is read too.
        run: Runs a statement; the source's own connection when ``None``.

    Returns:
        The schema, or ``None`` when none of its tables is found (``tables``
        or not) and it is not one of the source's schemas.

    Raises:
        InspectionNotSupportedError: The source's dialect has no catalog reader.
        ExecutionUnavailableError: The source cannot be connected to.
        ExecutionError: The table or column query failed. A key or view query
            that fails only adds a warning.
    """
    run = run or runner_for(source)
    catalog = catalog_for(source.dialect, source)
    wanted = set(tables) if tables else None
    info = SchemaInfo(schema=schema)

    by_name: dict[str, TableInfo] = {}
    for row in _rows(run, catalog.tables(schema, wanted)):
        name = _text(row.get("table_name"))
        if name is None:
            continue
        kind = _kind(row.get("table_type"))
        if kind != "table" and not include_views:
            continue
        comment = _text(row.get("comment"))
        if kind != "table" and comment is not None and comment.upper() == "VIEW":
            comment = None  # MySQL's TABLE_COMMENT of every view
        by_name[name] = TableInfo(schema=schema, name=name, kind=kind, comment=comment)

    if not by_name:
        # An empty answer, filtered or not, is an unknown schema or an empty one.
        if schema not in list_schemas(source, run=run):
            return None
        return info

    columns: dict[str, list[tuple[int, Row]]] = {}
    for row in _rows(run, catalog.columns(schema, wanted)):
        table = _text(row.get("table_name"))
        if table in by_name:
            columns.setdefault(table, []).append((_ordinal(row.get("ordinal")), row))

    primary = _read_primary_keys(run, catalog, schema, wanted, info.warnings)
    for table, entries in columns.items():
        keys = primary.get(table, [])
        entries.sort(key=lambda entry: entry[0])
        by_name[table].primary_key = keys
        by_name[table].columns = [
            ColumnInfo(
                name=str(row.get("column_name")),
                data_type=_text(row.get("data_type")) or "",
                ordinal=position,
                nullable=_flag(row.get("is_nullable")),
                comment=_text(row.get("comment")),
                primary_key=str(row.get("column_name")) in keys,
            )
            for position, (_, row) in enumerate(entries, start=1)
        ]

    for table, fks in _read_foreign_keys(run, catalog, schema, wanted, info.warnings).items():
        if table in by_name:
            by_name[table].foreign_keys = fks

    if include_views and view_definitions:
        for table, definition in _read_views(run, catalog, schema, wanted, info.warnings).items():
            if table in by_name:
                by_name[table].view_definition = definition

    info.tables = [by_name[name] for name in sorted(by_name)]
    return info


def _optional(run: Runner, query: CatalogQuery | None, what: str, warnings: list[str]) -> list[Row]:
    """Rows of a statement whose failure costs the answer only *what*."""
    if query is None:
        return []
    try:
        return _rows(run, query)
    except ExecutionError as exc:
        logger.warning("Schema inspection could not read %s: %s", what, exc)
        warnings.append(f"Could not read {what}: {exc}")
        return []


def _read_primary_keys(
    run: Runner,
    catalog: Catalog,
    schema: str,
    tables: Collection[str] | None,
    warnings: list[str],
) -> dict[str, list[str]]:
    query = catalog.primary_keys(schema, tables)
    grouped: dict[str, list[tuple[int, str]]] = {}
    for row in _optional(run, query, "primary keys", warnings):
        table, column = _text(row.get("table_name")), _text(row.get("column_name"))
        if table and column:
            grouped.setdefault(table, []).append((_ordinal(row.get("ordinal")), column))
    return {table: [c for _, c in sorted(cols)] for table, cols in grouped.items()}


def _read_foreign_keys(
    run: Runner,
    catalog: Catalog,
    schema: str,
    tables: Collection[str] | None,
    warnings: list[str],
) -> dict[str, list[ForeignKeyInfo]]:
    query = catalog.foreign_keys(schema, tables)
    # A key is told apart by its name; an unnamed one by what it refers to.
    keys: dict[tuple[str, str], list[Row]] = {}
    for row in _optional(run, query, "foreign keys", warnings):
        table = _text(row.get("table_name"))
        referred = _text(row.get("referred_table"))
        if not table or not referred or not _text(row.get("column_name")):
            continue
        name = _text(row.get("constraint_name")) or f"{referred}#"
        keys.setdefault((table, name), []).append(row)

    found: dict[str, list[ForeignKeyInfo]] = {}
    for (table, _), rows in sorted(keys.items()):
        rows.sort(key=lambda r: _ordinal(r.get("ordinal")))
        first = rows[0]
        found.setdefault(table, []).append(
            ForeignKeyInfo(
                name=_text(first.get("constraint_name")),
                columns=tuple(str(r["column_name"]) for r in rows),
                referred_schema=_text(first.get("referred_schema")),
                referred_table=str(first["referred_table"]),
                referred_columns=tuple(str(r.get("referred_column") or "") for r in rows),
            )
        )
    return found


def _read_views(
    run: Runner,
    catalog: Catalog,
    schema: str,
    tables: Collection[str] | None,
    warnings: list[str],
) -> dict[str, str | None]:
    query = catalog.views(schema, tables)
    return {
        name: _text(row.get("definition"))
        for row in _optional(run, query, "view definitions", warnings)
        if (name := _text(row.get("table_name")))
    }

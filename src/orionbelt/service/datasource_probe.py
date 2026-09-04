"""Probe the configured datasource for the tables and columns a model declares.

Validation is otherwise entirely offline. :mod:`orionbelt.parser.validator`
resolves a model against *itself* - every reference it checks names something
declared in the same document - and the physical binding is the one part it
cannot reach: a data object's ``database`` / ``schema`` / ``code``, and each
column's ``code``, are opaque strings on the way to codegen. So a model can be
valid in every structural sense and still name a table that was dropped or a
column that was renamed, and the first thing to say so is the warehouse, at
query time, to whoever ran the query rather than to whoever wrote the model.

This module closes that gap on request. Two things make the shape of the probe:

*   It asks for the declared columns **by name, quoted exactly as codegen
    quotes them** - ``SELECT "amount", "order_date" FROM "sales" LIMIT 0`` -
    rather than reading ``information_schema``. That proves the columns are
    addressable *through the same connection and the same spelling the
    compiled query will use*, which a catalog read does not, and it settles
    identifier case without this module having to carry a table of which
    engines fold it: the engine answers.
*   ``LIMIT 0`` means the round trip costs a plan and no scan, and the result
    still carries a full schema, so column types come back from the same
    request that proves the columns exist.

Only when that fails does a second ``SELECT *`` run, to tell a missing table
from a missing column and to name which column. The happy path is one round
trip per data object.

Findings use the same :class:`SemanticError` shape as the offline validator,
so a caller that already renders validation errors renders these unchanged.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from orionbelt.dialect.registry import DialectRegistry, UnsupportedDialectError
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import DataType

if TYPE_CHECKING:
    from orionbelt.models.semantic import DataObject, DataObjectColumn, SemanticModel

logger = logging.getLogger(__name__)

#: How the probe describes a type it is willing to compare. Deliberately coarse:
#: the question is whether a column still holds the *kind* of value the model
#: says it does, not whether ``int32`` widened to ``int64``. A narrower
#: comparison would fire on every engine that stores an OBML ``int`` as
#: ``NUMBER(38,0)`` or a ``timestamp`` as ``TIMESTAMP_NTZ``, which is noise
#: rather than drift.
_NUMBER = "number"
_DATETIME = "datetime"
_STRING = "string"
_BOOLEAN = "boolean"
_BINARY = "binary"

#: Declared OBML type -> the family a physical column must belong to. ``json``
#: sits with ``string`` because most engines have no JSON type and the ones
#: that do accept a text column in the same position.
_DECLARED_FAMILY: dict[DataType, str] = {
    DataType.STRING: _STRING,
    DataType.JSON: _STRING,
    DataType.INT: _NUMBER,
    DataType.FLOAT: _NUMBER,
    DataType.DATE: _DATETIME,
    DataType.TIME: _DATETIME,
    DataType.TIME_TZ: _DATETIME,
    DataType.TIMESTAMP: _DATETIME,
    DataType.TIMESTAMP_TZ: _DATETIME,
    DataType.BOOLEAN: _BOOLEAN,
}


def _arrow_family(arrow_type: Any) -> str | None:
    """Family for a PyArrow type, or ``None`` when it should not be compared.

    ``None`` is the answer for a null-typed column and for anything this does
    not recognise: a nested or extension type carries no claim the model made,
    and guessing at one would report a mismatch that is really an omission
    here. A ``LIMIT 0`` fetch is where null-typed columns turn up, so that
    branch is load-bearing rather than defensive.
    """
    import pyarrow as pa

    try:
        if pa.types.is_null(arrow_type):
            return None
        if pa.types.is_boolean(arrow_type):
            return _BOOLEAN
        if (
            pa.types.is_integer(arrow_type)
            or pa.types.is_floating(arrow_type)
            or pa.types.is_decimal(arrow_type)
        ):
            return _NUMBER
        if (
            pa.types.is_timestamp(arrow_type)
            or pa.types.is_date(arrow_type)
            or pa.types.is_time(arrow_type)
        ):
            return _DATETIME
        if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
            return _STRING
        if pa.types.is_binary(arrow_type) or pa.types.is_large_binary(arrow_type):
            return _BINARY
    except (AttributeError, TypeError):
        # Mirrors ``db_executor._arrow_type_to_hint``: ADBC hands back
        # OpaqueType subclasses that do not implement the full DataType
        # protocol, and the ``pa.types.is_*`` helpers raise on them rather
        # than returning False.
        return None
    return None


def _hint_family(type_hint: str) -> str | None:
    """Family for the coarse PEP 249 hint, or ``None`` when undecidable.

    ``db_executor`` buckets a driver type code into one of four strings, and
    ``"string"`` is both a real answer and the fallback for every type code it
    does not recognise - a BOOLEAN column lands there, and so does anything
    exotic. It therefore refutes nothing, and reading it as a claim would
    report a mismatch on a correct model whenever the Arrow path happened to
    be unavailable. The other three buckets are positive classifications.
    """
    if type_hint in (_NUMBER, _DATETIME, _BINARY):
        return type_hint
    return None


def _families(result: Any) -> list[str | None]:
    """Family per result column, in the order the driver returned them.

    Prefers the Arrow schema, which distinguishes a boolean from a string;
    the coarse PEP 249 hint cannot. Seven of the eight drivers are
    Arrow-native, so the fallback is the exception.
    """
    schema = result.arrow_schema
    if schema is not None:
        try:
            return [_arrow_family(field.type) for field in schema]
        except ImportError:  # pragma: no cover - a schema implies pyarrow
            pass
    return [_hint_family(col.type_hint) for col in result.columns]


def _physical_columns(obj: DataObject) -> list[DataObjectColumn]:
    """The columns of *obj* that name a physical column.

    A computed column defines itself with an inline ``expression`` and its
    ``code`` is ignored at codegen, so asking the warehouse for it would
    report every computed column as missing.
    """
    return [col for col in obj.columns.values() if col.code and not col.is_computed]


def _qualified_name(obj: DataObject) -> str:
    """Unquoted ``database.schema.code``, for a message a human reads."""
    parts = [p for p in (obj.database, obj.schema_name, obj.code) if p]
    return ".".join(parts) if parts else obj.code


def _select_columns(dialect: Any, obj: DataObject, columns: list[DataObjectColumn]) -> str:
    """``SELECT <declared columns> FROM <table> LIMIT 0`` for *obj*.

    The column list is quoted through the dialect, which is what makes a pass
    meaningful: it is the spelling a compiled query will use, so the engine is
    answering the question the model actually asks of it.
    """
    projection = ", ".join(dialect.quote_identifier(col.code) for col in columns)
    table = dialect.format_table_ref(obj.database, obj.schema_name, obj.code)
    return f"SELECT {projection} FROM {table} LIMIT 0"


def _select_star(dialect: Any, obj: DataObject) -> str:
    """``SELECT * FROM <table> LIMIT 0`` for *obj*."""
    table = dialect.format_table_ref(obj.database, obj.schema_name, obj.code)
    return f"SELECT * FROM {table} LIMIT 0"


def _type_findings(
    obj_name: str,
    columns: list[DataObjectColumn],
    families: list[str | None],
) -> list[SemanticError]:
    """Compare each declared ``abstractType`` against the type that came back.

    Matched by position: the projection was built from *columns* in this order,
    so column *i* of the result is column *i* of the model. Matching by name
    instead would have to survive engines that fold the output label, and there
    is nothing to gain from it.
    """
    findings: list[SemanticError] = []
    for col, actual in zip(columns, families, strict=False):
        declared = _DECLARED_FAMILY.get(col.abstract_type)
        if declared is None or actual is None or declared == actual:
            continue
        findings.append(
            SemanticError(
                code="DATASOURCE_TYPE_MISMATCH",
                message=(
                    f"Column '{col.code}' on data object '{obj_name}' is declared "
                    f"'{col.abstract_type}' but the datasource returns a {actual} column."
                ),
                path=f"dataObjects.{obj_name}.columns.{col.name}.abstractType",
                hint=(
                    f"Change abstractType to one of the '{declared}' family that matches "
                    f"the column, or cast it in the source view."
                ),
                context={
                    "dataObject": obj_name,
                    "column": col.name,
                    "code": col.code,
                    "declaredType": str(col.abstract_type),
                    "actualFamily": actual,
                },
            )
        )
    return findings


def _column_findings(
    obj_name: str,
    obj: DataObject,
    columns: list[DataObjectColumn],
    present: list[str],
    families: list[str | None],
    projection_error: str,
) -> list[SemanticError]:
    """Explain why the projection failed, given the columns ``SELECT *`` found.

    ``present`` and *families* come from the star probe, so they describe the
    table as it really is. A declared column absent from it is missing; one
    that matches only when case is ignored is the more interesting answer,
    because the projection just proved the engine will not accept the spelling
    the model uses.
    """
    findings: list[SemanticError] = []
    by_name = dict(zip(present, families, strict=False))
    folded = {name.casefold(): name for name in present}
    surviving: list[DataObjectColumn] = []
    surviving_families: list[str | None] = []

    for col in columns:
        if col.code in by_name:
            surviving.append(col)
            surviving_families.append(by_name[col.code])
            continue
        actual_name = folded.get(col.code.casefold())
        if actual_name is None:
            findings.append(
                SemanticError(
                    code="DATASOURCE_COLUMN_MISSING",
                    message=(
                        f"Column '{col.code}' does not exist on {_qualified_name(obj)} "
                        f"(data object '{obj_name}')."
                    ),
                    path=f"dataObjects.{obj_name}.columns.{col.name}.code",
                    hint="Correct the column's 'code', or drop the column from the model.",
                    context={"dataObject": obj_name, "column": col.name, "code": col.code},
                )
            )
            continue
        findings.append(
            SemanticError(
                code="DATASOURCE_COLUMN_CASE",
                message=(
                    f"Column '{col.code}' on {_qualified_name(obj)} is spelled "
                    f"'{actual_name}' in the datasource, and this engine rejected "
                    f"the model's spelling."
                ),
                path=f"dataObjects.{obj_name}.columns.{col.name}.code",
                hint=f"Set the column's 'code' to '{actual_name}'.",
                context={
                    "dataObject": obj_name,
                    "column": col.name,
                    "code": col.code,
                    "actualCode": actual_name,
                },
            )
        )
        surviving.append(col)
        surviving_families.append(by_name[actual_name])

    if not findings:
        # The table is readable and every declared column is there under the
        # exact spelling the model uses, yet the projection was rejected. The
        # cause is something this function cannot name - a column-level
        # permission, a type the engine will not project, a driver quirk - so
        # the driver's own words are the useful thing to return.
        findings.append(
            SemanticError(
                code="DATASOURCE_PROBE_FAILED",
                message=(
                    f"Reading the declared columns of data object '{obj_name}' failed, "
                    f"though {_qualified_name(obj)} exists and every column was found: "
                    f"{projection_error}"
                ),
                path=f"dataObjects.{obj_name}",
                context={"dataObject": obj_name, "error": projection_error},
            )
        )
        return findings

    return findings + _type_findings(obj_name, surviving, surviving_families)


def _probe_object(
    dialect: Any, dialect_name: str, obj_name: str, obj: DataObject
) -> list[SemanticError]:
    """Probe one data object. Raises ``ExecutionUnavailableError`` to the caller."""
    from orionbelt.service.db_executor import ExecutionError, execute_sql

    columns = _physical_columns(obj)

    if not columns:
        # Nothing to project, so the table's own existence is the whole
        # question - a data object whose columns are all computed still has
        # to come FROM somewhere.
        try:
            execute_sql(_select_star(dialect, obj), dialect=dialect_name)
        except ExecutionError as exc:
            return [_table_missing(obj_name, obj, str(exc))]
        return []

    try:
        result = execute_sql(_select_columns(dialect, obj, columns), dialect=dialect_name)
    except ExecutionError as exc:
        projection_error = str(exc)
    else:
        return _type_findings(obj_name, columns, _families(result))

    # The projection failed. A second probe separates a missing table from a
    # missing column, and returns the column list needed to say which.
    try:
        star = execute_sql(_select_star(dialect, obj), dialect=dialect_name)
    except ExecutionError as exc:
        return [_table_missing(obj_name, obj, str(exc))]

    return _column_findings(
        obj_name,
        obj,
        columns,
        [col.name for col in star.columns],
        _families(star),
        projection_error,
    )


def _table_missing(obj_name: str, obj: DataObject, error: str) -> SemanticError:
    """The table a data object maps to could not be read."""
    return SemanticError(
        code="DATASOURCE_TABLE_MISSING",
        message=(
            f"Data object '{obj_name}' maps to {_qualified_name(obj)}, which the "
            f"datasource could not read: {error}"
        ),
        path=f"dataObjects.{obj_name}.code",
        hint="Correct the data object's 'database', 'schema' and 'code', or create the table.",
        context={
            "dataObject": obj_name,
            "table": _qualified_name(obj),
            "error": error,
        },
    )


def _ensure_arrow() -> None:
    """Import pyarrow, when installed, before any probe runs.

    ``db_executor`` takes a driver's Arrow path only when pyarrow is *already*
    in ``sys.modules``: it will not pull a heavy dependency in behind a query's
    back. That is the right default for executing a query, where the coarse
    PEP 249 hint carries enough type information for the response. It is the
    wrong one here, because that coarse bucket folds BOOLEAN and every type
    code the driver does not recognise into ``"string"`` — which
    :func:`_hint_family` correctly refuses to treat as a claim, so the type
    check quietly stops running. Which types a probe can compare would then
    depend on whether something else in the process happened to import pyarrow
    first, and the same model would validate differently under the API and the
    CLI. Importing it up front is what makes the comparison deterministic.
    """
    with contextlib.suppress(ImportError):
        import pyarrow  # noqa: F401


def probe_datasource(model: SemanticModel, *, dialect: str) -> list[SemanticError]:
    """Check every data object in *model* against the configured datasource.

    Returns one finding per problem, in data object declaration order, or an
    empty list when the model matches the warehouse. The caller decides what a
    finding means; nothing here raises for a mismatch.

    Objects sourced by ``nestedIn`` are skipped: their rows come from unnesting
    an array on another object, so their columns belong to that array's element
    type rather than to any table of their own, and projecting the declared
    codes against the fallback ``code`` table would report drift that is not
    there.
    """
    from orionbelt.service.db_executor import ExecutionUnavailableError

    _ensure_arrow()
    try:
        impl = DialectRegistry.get(dialect)
    except UnsupportedDialectError:
        return [
            SemanticError(
                code="DATASOURCE_UNSUPPORTED_DIALECT",
                message=f"Cannot probe the datasource: unsupported dialect '{dialect}'.",
                hint="Pass a supported dialect, or set the model's settings.defaultDialect.",
                context={"dialect": dialect},
            )
        ]

    findings: list[SemanticError] = []
    for obj_name, obj in model.data_objects.items():
        if obj.nested_in is not None:
            continue
        try:
            findings.extend(_probe_object(impl, dialect, obj_name, obj))
        except ExecutionUnavailableError as exc:
            # No connection, no driver, or no credentials. Every remaining
            # object would fail the same way, so stop rather than repeat it
            # once per data object, and say plainly that the check did not run.
            findings.append(
                SemanticError(
                    code="DATASOURCE_UNAVAILABLE",
                    message=f"The datasource check could not run: {exc}",
                    hint=(
                        "Configure the vendor connection (DB_VENDOR and its credentials), "
                        "or validate without the datasource check."
                    ),
                    context={"dialect": dialect, "error": str(exc)},
                )
            )
            break
        except Exception as exc:  # noqa: BLE001 - a probe must not fail validation
            # Anything else - a dialect that refuses to render the reference,
            # a driver raising outside its documented contract. Report it
            # against the object rather than losing the rest of the model.
            logger.warning("Datasource probe failed for '%s': %s", obj_name, exc)
            findings.append(
                SemanticError(
                    code="DATASOURCE_PROBE_FAILED",
                    message=f"Probing data object '{obj_name}' failed: {exc}",
                    path=f"dataObjects.{obj_name}",
                    context={"dataObject": obj_name, "error": str(exc)},
                )
            )
    return findings

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

*   It asks for the declared columns **as the compiled query reads them** -
    ``SELECT "Sales"."amount", ("Sales"."amount" * 0.8) FROM "sales" AS "Sales"
    LIMIT 0`` - rather than reading ``information_schema``. The statement is
    built from the compiler's own pieces: each column from its column builder,
    so a computed column is its expression inlined, and a ``nestedIn`` object's
    FROM clause from the planners' unnest fragment, so its fields are read from
    the parent's array the way the engine spells it. That proves the columns
    can be read *through the same connection and the same SQL the compiled
    query will use*, which a catalog read does not, and it settles identifier
    case without this module having to carry a table of which engines fold it:
    the engine answers.
*   ``LIMIT 0`` means the round trip costs a plan and no scan, and the result
    still carries a full schema, so column types come back from the same
    request that proves the columns exist.

Only when that fails do narrower probes run: the physical columns alone, then
a ``SELECT *`` to tell a missing table from a missing column and to name which
column, and each computed column and nested field on its own. The happy path is
one round trip per data object.

What the engine cannot answer, the probe cannot either. Snowflake reads a nested
field as a path into a semi-structured value and MySQL through ``JSON_TABLE``:
on both, a field the elements do not have reads as NULL rather than failing,
and the value is cast to the declared type, so neither a missing field nor its
type can be told apart from a correct one there.

Findings use the same :class:`SemanticError` shape as the offline validator,
so a caller that already renders validation errors renders these unchanged.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from orionbelt.dialect.registry import DialectRegistry, UnsupportedDialectError
from orionbelt.models.errors import SemanticError
from orionbelt.models.semantic import DataType

if TYPE_CHECKING:
    from collections.abc import Callable

    from ob_flight.datasources import DataSource

    from orionbelt.ast.nodes import Expr
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
    DataType.DECIMAL: _NUMBER,
    DataType.DATE: _DATETIME,
    DataType.TIME: _DATETIME,
    DataType.TIME_TZ: _DATETIME,
    DataType.TIMESTAMP: _DATETIME,
    DataType.TIMESTAMP_TZ: _DATETIME,
    DataType.BOOLEAN: _BOOLEAN,
}


#: Opaque vendor type names that are positively text, as opposed to names
#: ``coarse_hint_from_type_name`` merely fails to place. It answers ``"string"``
#: for both, so without this list a Postgres ``uuid`` or ``xml`` column stays
#: undecidable and a ``float`` declared over it passes. Every entry is a type
#: whose values *are* text; a name not here (``tsvector``, a vendor type nobody
#: has seen) still refutes nothing.
_OPAQUE_STRING_NAMES = frozenset(
    {
        "uuid",
        "xml",
        "json",
        "jsonb",
        "inet",
        "cidr",
        "macaddr",
        "macaddr8",
        "citext",
        "name",
        "bpchar",
        "character",
        "character varying",
        "varchar",
        "text",
    }
)


def _structural_family(arrow_type: Any) -> str | None:
    """Family from PyArrow's own type predicates, or ``None`` if none match."""
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
            # An interval is not a point in time, but "datetime" is the bucket
            # this codebase puts it in, and ADBC's Postgres driver hands a real
            # ``INTERVAL`` back as ``month_day_nano_interval`` rather than
            # wrapping it. Without these two the column is unclassified and a
            # ``timestamp`` declared over it is never checked.
            or pa.types.is_interval(arrow_type)
            or pa.types.is_duration(arrow_type)
        ):
            return _DATETIME
        if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
            return _STRING
        if pa.types.is_binary(arrow_type) or pa.types.is_large_binary(arrow_type):
            return _BINARY
    except (AttributeError, TypeError):
        # ADBC hands back extension subclasses that do not implement the full
        # DataType protocol, and the ``pa.types.is_*`` helpers raise on them
        # rather than returning False.
        return None
    return None


def _arrow_family(arrow_type: Any) -> str | None:
    """Family for a PyArrow type, or ``None`` when it should not be compared.

    ``None`` is the answer for a null-typed column and for anything neither
    route below places: an unplaced type carries no claim the model made, and
    guessing would report a mismatch that is really an omission here. A
    ``LIMIT 0`` fetch is where null-typed columns turn up, so that branch is
    load-bearing rather than defensive.

    Two routes, because a driver has two ways of handing back a type PyArrow
    has no native spelling for, and they need opposite treatment:

    ``arrow.opaque``
        ADBC's escape hatch for a vendor type it cannot convert - Postgres
        ``NUMERIC``, ``MONEY``, ``uuid``, ``tsvector``. Its storage is a
        *carrier*, almost always a string, and says nothing about the value:
        classifying ``NUMERIC`` by its storage would call it text. Only the
        vendor ``type_name`` means anything, so that is what is read.

    A canonical extension (``arrow.json``, ...)
        The opposite. Its storage *is* the representation - Postgres ``json``
        and ``jsonb`` both arrive as ``arrow.json`` over string storage - so
        the storage type is exactly the right thing to classify, and it keeps
        working for canonical extensions that do not exist yet.
    """
    family = _structural_family(arrow_type)
    if family is not None:
        return family

    type_name = getattr(arrow_type, "type_name", None)
    if isinstance(type_name, str):
        if type_name.lower() in _OPAQUE_STRING_NAMES:
            return _STRING
        from orionbelt.service.db_executor import coarse_hint_from_type_name

        # Coarse, so it goes through ``_hint_family`` for the reason the PEP 249
        # path does: ``"string"`` is that mapping's unknown bucket as well as
        # its answer for text, and an unknown must not refute a declaration.
        return _hint_family(coarse_hint_from_type_name(type_name))

    storage = getattr(arrow_type, "storage_type", None)
    if storage is not None:
        return _structural_family(storage)
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


@dataclass(frozen=True)
class _Kind:
    """What came back for one column: its family, and whether a number is an integer.

    ``integer`` is ``None`` where the result does not say: for a column that is
    not a number, and for a number known only by the coarse PEP 249 hint or a
    vendor type name. The distinction matters for one comparison only, a
    boolean on an engine that answers one as an integer (:func:`_refutes`).
    """

    family: str | None
    integer: bool | None = None


def _arrow_integer(arrow_type: Any) -> bool | None:
    """Whether an Arrow number type is an integer; ``None`` when it does not say."""
    import pyarrow as pa

    try:
        if pa.types.is_integer(arrow_type):
            return True
        if pa.types.is_floating(arrow_type) or pa.types.is_decimal(arrow_type):
            return False
    except (AttributeError, TypeError):
        # The extension subclasses :func:`_structural_family` describes.
        return None
    return None


def _kinds(result: Any) -> list[_Kind]:
    """What came back for each result column, in the order the driver returned them.

    Prefers the Arrow schema, which distinguishes a boolean from a string and an
    integer from a decimal; the coarse PEP 249 hint can do neither. Seven of the
    eight drivers are Arrow-native, so the fallback is the exception.
    """
    schema = result.arrow_schema
    if schema is not None:
        try:
            return [_Kind(_arrow_family(f.type), _arrow_integer(f.type)) for f in schema]
        except ImportError:  # pragma: no cover - a schema implies pyarrow
            pass
    return [_Kind(_hint_family(col.type_hint)) for col in result.columns]


@dataclass(frozen=True)
class _Selected:
    """A declared column and the expression a compiled query reads it as."""

    column: DataObjectColumn
    expr: Expr


def _columns_of(selected: list[_Selected]) -> list[DataObjectColumn]:
    return [item.column for item in selected]


def _selected_columns(
    model: SemanticModel, obj_name: str, obj: DataObject
) -> tuple[list[_Selected], list[_Selected]]:
    """The physical and the computed columns of *obj*, each as a query reads it.

    Every expression comes from the compiler's own column builder
    (:func:`~orionbelt.compiler.resolution.make_column_expr`), so a computed
    column is its expression inlined and a nested field is read the way its
    engine spells it, exactly as in a compiled query. Read without the query
    time zone: the question is whether the column can be read at all.

    Two computed columns are left out. One whose expression reads another data
    object, which a query joins first and the probe, reading this object alone,
    cannot. And one whose expression does not parse: offline validation already
    reports it (``INVALID_COLUMN_EXPRESSION``), and there is no SQL to send.
    """
    from orionbelt.compiler.resolution import ResolutionError, make_column_expr

    physical: list[_Selected] = []
    computed: list[_Selected] = []
    for col in obj.columns.values():
        if not col.is_computed:
            if col.code:
                expr = make_column_expr(model, obj_name, col.name, in_query_timezone=False)
                physical.append(_Selected(col, expr))
            continue
        if model.column_reference_objects(obj_name, col.name):
            continue
        try:
            expr = make_column_expr(model, obj_name, col.name, in_query_timezone=False)
        except ResolutionError:
            continue
        computed.append(_Selected(col, expr))
    return physical, computed


def _from_chain(dialect: Any, model: SemanticModel, obj_name: str) -> list[str]:
    """The data objects a FROM clause names to read *obj_name*, outermost first.

    Just the object itself for one read from its own table. A ``nestedIn``
    object is unnested out of its parent, so the chain runs from the first
    ancestor with a table down to it, the order a planner unnests them in. On a
    dialect without a FROM-clause unnest (Dremio) a query reads the object's
    ``code`` table instead, and so does the probe.
    """
    chain = [obj_name]
    obj = model.data_objects[obj_name]
    while obj.nested_in is not None and dialect.capabilities.supports_from_unnest:
        parent = obj.nested_in.data_object
        if parent in chain or parent not in model.data_objects:
            break  # a loop or a dangling parent, both refused by offline validation
        chain.insert(0, parent)
        obj = model.data_objects[parent]
    return chain


def _qualified_name(obj: DataObject) -> str:
    """Unquoted ``database.schema.code``, for a message a human reads."""
    parts = [p for p in (obj.database, obj.schema_name, obj.code) if p]
    return ".".join(parts) if parts else obj.code


def _table_ref(dialect: Any, obj: DataObject) -> str:
    return str(dialect.format_table_ref(obj.database, obj.schema_name, obj.code))


def _select_sql(
    dialect: Any, model: SemanticModel, chain: list[str], selected: list[_Selected]
) -> str:
    """``SELECT <expressions> FROM <chain> LIMIT 0``, built the way a query is.

    The FROM clause names the first object's table, aliased by the object's
    name as the planners alias it, and unnests each later object out of the one
    before it with the planners' own fragment
    (:func:`~orionbelt.compiler.nested.build_unnest`). The dialect renders the
    whole statement, so the engine is asked what a compiled query would ask it,
    in the spelling that query would use. ``SELECT 1`` when nothing is
    selected: the question is then only whether the FROM clause can be read.

    ``LIMIT 0`` means the round trip costs a plan and no scan, and the result
    still carries a full schema, so the types come back from the same request.
    """
    from orionbelt.ast.builder import QueryBuilder
    from orionbelt.ast.nodes import JoinType as ASTJoinType
    from orionbelt.ast.nodes import Literal
    from orionbelt.compiler.graph import JoinStep
    from orionbelt.compiler.nested import build_unnest
    from orionbelt.models.semantic import Cardinality

    builder = QueryBuilder().from_(_table_ref(dialect, model.data_objects[chain[0]]), chain[0])
    for parent, child in itertools.pairwise(chain):
        step = JoinStep(
            from_object=parent,
            to_object=child,
            from_columns=[],
            to_columns=[],
            join_type=ASTJoinType.LEFT,
            cardinality=Cardinality.MANY_TO_ONE,
            nested=True,
        )
        builder.unnest(build_unnest(step, model.data_objects[child]))
    for i, item in enumerate(selected, start=1):
        builder.select_aliased(item.expr, f"c{i}")
    if not selected:
        builder.select(Literal(value=1))
    return str(dialect.compile(builder.limit(0).build()))


def _select_star(dialect: Any, obj: DataObject) -> str:
    """``SELECT * FROM <table> LIMIT 0`` for *obj*."""
    table = dialect.format_table_ref(obj.database, obj.schema_name, obj.code)
    return f"SELECT * FROM {table} LIMIT 0"


def _refutes(dialect: Any, declared: str, actual: _Kind) -> bool:
    """Whether a column that came back as *actual* contradicts the *declared* family.

    An unplaced type refutes nothing, and neither does the same family. Nor does
    an integer behind a boolean where the engine answers one that way
    (:attr:`~orionbelt.dialect.base.Dialect.booleans_as_integers`): MySQL has no
    boolean type, and a ClickHouse comparison is a ``UInt8``. A float or a
    decimal there is still drift, as query execution leaves it a number; a
    number of unknown kind refutes nothing.
    """
    if actual.family is None or actual.family == declared:
        return False
    if declared == _BOOLEAN and actual.family == _NUMBER and dialect.booleans_as_integers:
        return actual.integer is False
    return True


def _type_findings(
    dialect: Any,
    obj_name: str,
    columns: list[DataObjectColumn],
    kinds: list[_Kind],
) -> list[SemanticError]:
    """Compare each declared ``abstractType`` against the type that came back.

    Matched by position: the projection was built from *columns* in this order,
    so column *i* of the result is column *i* of the model. Matching by name
    instead would have to survive engines that fold the output label, and there
    is nothing to gain from it.
    """
    findings: list[SemanticError] = []
    for col, kind in zip(columns, kinds, strict=False):
        declared = _DECLARED_FAMILY.get(col.abstract_type)
        if declared is None or not _refutes(dialect, declared, kind):
            continue
        actual = kind.family
        if col.is_computed:
            subject = f"Computed column '{col.name}'"
            fix = f"change its expression to return {col.abstract_type}"
        else:
            subject = f"Column '{col.code}'"
            fix = f"cast '{col.code}' to {col.abstract_type} in the source view"
        findings.append(
            SemanticError(
                code="DATASOURCE_TYPE_MISMATCH",
                message=(
                    f"{subject} on data object '{obj_name}' is declared "
                    f"'{col.abstract_type}' but the datasource returns a {actual} column."
                ),
                path=f"dataObjects.{obj_name}.columns.{col.name}.abstractType",
                # Names the family that came back, not the declared one. The
                # declared family is what the model already says, so offering it
                # as the correction reads as "change float to a number type" -
                # advice to make no change at all.
                hint=f"Either change abstractType to a '{actual}' type, or {fix}.",
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
    dialect: Any,
    obj_name: str,
    obj: DataObject,
    columns: list[DataObjectColumn],
    present: list[str],
    kinds: list[_Kind],
    projection_error: str,
) -> list[SemanticError]:
    """Explain why the projection failed, given the columns ``SELECT *`` found.

    ``present`` and *kinds* come from the star probe, so they describe the
    table as it really is. A declared column absent from it is missing; one
    that matches only when case is ignored is the more interesting answer,
    because the projection just proved the engine will not accept the spelling
    the model uses.
    """
    findings: list[SemanticError] = []
    by_name = dict(zip(present, kinds, strict=False))
    folded = {name.casefold(): name for name in present}
    surviving: list[DataObjectColumn] = []
    surviving_kinds: list[_Kind] = []

    for col in columns:
        if col.code in by_name:
            surviving.append(col)
            surviving_kinds.append(by_name[col.code])
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
        surviving_kinds.append(by_name[actual_name])

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

    return findings + _type_findings(dialect, obj_name, surviving, surviving_kinds)


class _Prober:
    """The probes of one data object, run on its data source."""

    def __init__(
        self,
        dialect: Any,
        dialect_name: str,
        model: SemanticModel,
        obj_name: str,
        source: DataSource | None,
    ) -> None:
        self.dialect = dialect
        self.dialect_name = dialect_name
        self.model = model
        self.obj_name = obj_name
        self.obj = model.data_objects[obj_name]
        self.source = source
        self.chain = _from_chain(dialect, model, obj_name)

    def _execute(self, sql: str) -> Any:
        from orionbelt.service.db_executor import execute_sql

        return execute_sql(sql, dialect=self.dialect_name, source=self.source)

    def _select(self, selected: list[_Selected], chain: list[str] | None = None) -> Any:
        """Select *selected* over *chain*, by default this object's. Raises on failure."""
        chain = self.chain if chain is None else chain
        return self._execute(_select_sql(self.dialect, self.model, chain, selected))

    def probe(self) -> list[SemanticError]:
        """Every finding for the object. Raises ``ExecutionUnavailableError``.

        One statement selects every column the probe can evaluate, so a model
        that matches its warehouse costs one round trip per data object. Only
        when it fails do narrower probes run, to say which part failed.
        """
        from orionbelt.service.db_executor import ExecutionError

        if self.obj.nested_in is not None and len(self.chain) == 1 and not self.obj.code:
            # No FROM-clause unnest on this dialect and no table to fall back
            # to: queries refuse the object here before any SQL runs.
            return []
        physical, computed = _selected_columns(self.model, self.obj_name, self.obj)
        try:
            result = self._select(physical + computed)
        except ExecutionError as exc:
            error = str(exc)
        else:
            return _type_findings(
                self.dialect, self.obj_name, _columns_of(physical + computed), _kinds(result)
            )

        if len(self.chain) > 1:
            findings, readable = self._nested_findings(physical)
        else:
            findings, readable = self._table_findings(physical, computed, error)
        if not readable:
            return findings
        findings += self._isolated(computed, _expression_failed)
        if all(f.code == "DATASOURCE_TYPE_MISMATCH" for f in findings):
            # Every part read on its own, yet the statement reading them together
            # failed. Nothing here can name the cause, so return the driver's words.
            findings.append(_probe_failed(self.obj_name, error))
        return findings

    def _table_findings(
        self, physical: list[_Selected], computed: list[_Selected], error: str
    ) -> tuple[list[SemanticError], bool]:
        """The physical columns' findings, and whether the table can be read.

        Probed apart from the computed columns. When they fail, a ``SELECT *``
        separates a missing table from a missing column and returns the column
        list needed to say which.
        """
        from orionbelt.service.db_executor import ExecutionError

        physical_error: str | None = None
        if physical and computed:
            try:
                result = self._select(physical)
            except ExecutionError as exc:
                physical_error = str(exc)
            else:
                findings = _type_findings(
                    self.dialect, self.obj_name, _columns_of(physical), _kinds(result)
                )
                return findings, True
        elif physical:
            physical_error = error  # the statement that failed read these alone
        try:
            star = self._execute(_select_star(self.dialect, self.obj))
        except ExecutionError as exc:
            return [_table_missing(self.obj_name, self.obj, str(exc))], False
        if physical_error is None:
            return [], True
        findings = _column_findings(
            self.dialect,
            self.obj_name,
            self.obj,
            _columns_of(physical),
            [col.name for col in star.columns],
            _kinds(star),
            physical_error,
        )
        return findings, True

    def _nested_findings(self, physical: list[_Selected]) -> tuple[list[SemanticError], bool]:
        """The fields' findings for a nested object, and whether its rows can be read.

        A ``SELECT 1`` over the same FROM clause asks whether the element rows
        can be read at all. When they cannot, the parent's own FROM clause says
        whose fault it is: if the parent cannot be read either, the parent's
        probe reports that, and reporting it here too would name the wrong object.
        """
        from orionbelt.service.db_executor import ExecutionError

        try:
            self._select([])
        except ExecutionError as exc:
            unnest_error = str(exc)
        else:
            return self._isolated(physical, _field_missing), True
        try:
            self._select([], self.chain[:-1])
        except ExecutionError:
            return [], False
        return [_unnest_failed(self.obj_name, self.obj, unnest_error)], False

    def _isolated(
        self,
        selected: list[_Selected],
        failed: Callable[[str, DataObject, DataObjectColumn, str], SemanticError],
    ) -> list[SemanticError]:
        """Each of *selected* probed on its own: *failed* for one the engine rejects."""
        from orionbelt.service.db_executor import ExecutionError

        findings: list[SemanticError] = []
        for item in selected:
            try:
                result = self._select([item])
            except ExecutionError as exc:
                findings.append(failed(self.obj_name, self.obj, item.column, str(exc)))
            else:
                findings += _type_findings(
                    self.dialect, self.obj_name, [item.column], _kinds(result)
                )
        return findings


def _field_missing(
    obj_name: str, obj: DataObject, col: DataObjectColumn, error: str
) -> SemanticError:
    """A nested object's field the engine could not read from the elements."""
    array = f"{obj.nested_in.data_object}.{obj.nested_in.column}" if obj.nested_in else obj_name
    return SemanticError(
        code="DATASOURCE_COLUMN_MISSING",
        message=(
            f"Field '{col.code}' could not be read from the elements of {array} "
            f"(data object '{obj_name}'): {error}"
        ),
        path=f"dataObjects.{obj_name}.columns.{col.name}.code",
        hint="Correct the column's 'code', or drop the column from the model.",
        context={"dataObject": obj_name, "column": col.name, "code": col.code, "error": error},
    )


def _expression_failed(
    obj_name: str, obj: DataObject, col: DataObjectColumn, error: str
) -> SemanticError:
    """A computed column whose expression the engine rejected."""
    return SemanticError(
        code="DATASOURCE_EXPRESSION_FAILED",
        message=(
            f"Computed column '{col.name}' on data object '{obj_name}' could not be "
            f"evaluated by the datasource: {error}"
        ),
        path=f"dataObjects.{obj_name}.columns.{col.name}.expression",
        hint=(
            "Correct the expression for this database: check the columns it reads "
            "and that the database has every function it calls."
        ),
        context={"dataObject": obj_name, "column": col.name, "error": error},
    )


def _unnest_failed(obj_name: str, obj: DataObject, error: str) -> SemanticError:
    """A nested object whose parent's array column could not be unnested."""
    assert obj.nested_in is not None
    parent, column = obj.nested_in.data_object, obj.nested_in.column
    return SemanticError(
        code="DATASOURCE_UNNEST_FAILED",
        message=(
            f"Data object '{obj_name}' unnests column '{column}' of '{parent}', which "
            f"the datasource could not read as an array: {error}"
        ),
        path=f"dataObjects.{obj_name}.nestedIn.column",
        hint=f"Correct nestedIn.column, or check that '{column}' is an array column of '{parent}'.",
        context={"dataObject": obj_name, "parent": parent, "column": column, "error": error},
    )


def _probe_failed(obj_name: str, error: str) -> SemanticError:
    """The object's statement failed, though every part of it read on its own."""
    return SemanticError(
        code="DATASOURCE_PROBE_FAILED",
        message=(
            f"Reading data object '{obj_name}' failed, though each of its columns "
            f"could be read on its own: {error}"
        ),
        path=f"dataObjects.{obj_name}",
        context={"dataObject": obj_name, "error": error},
    )


def _unavailable(
    dialect: str, exc: Exception, path: str | None = None, source: str | None = None
) -> SemanticError:
    context = {"dialect": dialect, "error": str(exc)}
    if source is not None:
        context["dataSource"] = source
    return SemanticError(
        code="DATASOURCE_UNAVAILABLE",
        message=f"The datasource check could not run: {exc}",
        path=path,
        hint=(
            "Configure the vendor connection (DB_VENDOR and its credentials), "
            "or validate without the datasource check."
        ),
        context=context,
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


def probe_datasource(
    model: SemanticModel, *, dialect: str, follow_sources: bool = False
) -> list[SemanticError]:
    """Check every data object in *model* against the configured datasource.

    Returns one finding per problem, in data object declaration order, or an
    empty list when the model matches the warehouse. The caller decides what a
    finding means; nothing here raises for a mismatch.

    A model whose data objects are read from several data sources has each
    probed on its own source (:func:`_probe_each_source`), as its queries read
    it; with *follow_sources*, in that source's dialect, else in *dialect*.

    An object sourced by ``nestedIn`` is read as a query reads it: unnested out
    of its parent's array column, on the parent's source, so its fields are
    checked against the array's elements rather than against a table of its own.
    """
    from orionbelt.service.datasource_selection import reads_several_sources, resolve_datasource
    from orionbelt.service.db_executor import ExecutionUnavailableError, ensure_arrow

    ensure_arrow()
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

    if reads_several_sources(model):
        return _probe_each_source(model, dialect, follow_sources)
    try:
        source = resolve_datasource(model, dialect)
    except ExecutionUnavailableError as exc:
        return [_unavailable(dialect, exc)]
    return _probe_objects(impl, dialect, model, list(model.data_objects), source)


def _probe_each_source(
    model: SemanticModel, dialect: str, follow_sources: bool
) -> list[SemanticError]:
    """Each data object probed on the data source its queries read it from.

    Its ``dataSource``, else ``settings.defaultDataSource``, else the
    dialect's own connection; a ``nestedIn`` object's is the source of the
    ancestor whose table it is unnested from. A source is probed in its own dialect with
    *follow_sources*, as a query on it is compiled, else in *dialect*, which
    then reports a source of another one, as for a model read from one. A
    source that cannot be reached is reported once, at the path naming it, and
    the other sources are probed all the same.
    """
    from orionbelt.service.datasource_selection import (
        resolve_named_source,
        source_dialect,
        source_key,
    )
    from orionbelt.service.db_executor import ExecutionUnavailableError

    default = model.settings.default_data_source if model.settings else None
    # Grouped as the registry compares names, in declaration order.
    groups: dict[str | None, tuple[str | None, list[str]]] = {}
    for name in model.data_objects:
        named = model.data_objects[model.unnest_root(name)].data_source or default
        key = source_key(named) if named is not None else None
        groups.setdefault(key, (named, []))[1].append(name)

    findings: list[SemanticError] = []
    for source_name, objects in groups.values():
        source_dialect_name = (follow_sources and source_dialect(source_name)) or dialect
        try:
            source = resolve_named_source(source_name, source_dialect_name, model)
        except ExecutionUnavailableError as exc:
            # Where the source is named, or would be: an object naming none
            # is read from the dialect's own, which can be ambiguous too.
            path = next(
                (
                    f"dataObjects.{o}.dataSource"
                    for o in objects
                    if model.data_objects[o].data_source
                ),
                "settings.defaultDataSource"
                if source_name is not None
                else f"dataObjects.{objects[0]}.dataSource",
            )
            findings.append(_unavailable(source_dialect_name, exc, path, source_name))
            continue
        impl = DialectRegistry.get(source_dialect_name)
        findings.extend(_probe_objects(impl, source_dialect_name, model, objects, source))
    return findings


def _probe_objects(
    impl: Any, dialect: str, model: SemanticModel, objects: list[str], source: DataSource | None
) -> list[SemanticError]:
    """The findings for *objects* of *model*, each probed on *source*."""
    from orionbelt.service.db_executor import ExecutionUnavailableError

    findings: list[SemanticError] = []
    for obj_name in objects:
        try:
            findings.extend(_Prober(impl, dialect, model, obj_name, source).probe())
        except ExecutionUnavailableError as exc:
            # No connection, no driver, or no credentials. Every remaining
            # object would fail the same way, so stop rather than repeat it
            # once per data object, and say plainly that the check did not run.
            findings.append(_unavailable(dialect, exc))
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

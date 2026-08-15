"""Computed columns that read a column of another data object.

A computed column names a sibling with ``{Column}`` and a column of another
data object with the qualified ``{[Data Object].[Column]}`` form measure
expressions already use. Reading the second form means the plan has to join
that object, which is what these tests pin down: the expression is inlined
wherever the column is referenced, so an unjoined alias is broken SQL rather
than a wrong number.
"""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import ResolutionError
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QuerySelect,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

PIPELINE = CompilationPipeline()

ALL_DIALECTS = [
    "bigquery",
    "clickhouse",
    "databricks",
    "dremio",
    "duckdb",
    "mysql",
    "postgres",
    "snowflake",
]

# Sales joins to Store and to Address. ``Store.Zip Matches`` compares its own
# zip to the address's — two objects the *fact* reaches, not each other.
# ``Address.Zip 5`` is computed too, so the reference chain crosses objects
# twice. ``Warehouse`` is joined from nothing: the unreachable case.
MODEL_YAML = """\
version: 1.0

dataObjects:
  Store:
    code: STORE
    database: WH
    schema: PUBLIC
    columns:
      Store Key: {code: S_STORE_SK, abstractType: int, primaryKey: true}
      Store Zip: {code: S_ZIP, abstractType: string}
      Zip Matches:
        expression: "SUBSTRING({Store Zip}, 1, 5) = {[Address].[Zip 5]}"
        abstractType: boolean
      Zip Differs:
        expression: "NOT {Zip Matches}"
        abstractType: boolean

  Address:
    code: CUSTOMER_ADDRESS
    database: WH
    schema: PUBLIC
    columns:
      Address Key: {code: CA_ADDRESS_SK, abstractType: int, primaryKey: true}
      Zip: {code: CA_ZIP, abstractType: string}
      Zip 5:
        expression: "SUBSTRING({Zip}, 1, 5)"
        abstractType: string

  Warehouse:
    code: WAREHOUSE
    database: WH
    schema: PUBLIC
    columns:
      Warehouse Key: {code: W_WAREHOUSE_SK, abstractType: int, primaryKey: true}
      Warehouse Zip: {code: W_ZIP, abstractType: string}

  Sales:
    code: STORE_SALES
    database: WH
    schema: PUBLIC
    columns:
      Sold Store Key: {code: SS_STORE_SK, abstractType: int}
      Sold Address Key: {code: SS_ADDR_SK, abstractType: int}
      Amount: {code: SS_EXT_SALES_PRICE, abstractType: float}
      Local Amount:
        expression: "CASE WHEN {[Store].[Zip Matches]} THEN {Amount} ELSE 0 END"
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Store
        columnsFrom: [Sold Store Key]
        columnsTo: [Store Key]
      - joinType: many-to-one
        joinTo: Address
        columnsFrom: [Sold Address Key]
        columnsTo: [Address Key]

dimensions:
  Store Zip: {dataObject: Store, column: Store Zip, resultType: string}
  Zip Matches: {dataObject: Store, column: Zip Matches, resultType: boolean}
  Zip Differs: {dataObject: Store, column: Zip Differs, resultType: boolean}

measures:
  Sales Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
  Local Sales Amount:
    columns: [{dataObject: Sales, column: Local Amount}]
    resultType: float
    aggregation: sum
  Matched Zip Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    filters:
      - column: {dataObject: Store, column: Zip Matches}
        operator: equals
        values: [{dataType: boolean, valueBoolean: true}]
  Context Matched Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    filterContext:
      mode: RELATIVE
      include:
        - {field: Zip Matches, op: "=", value: true}
  Qualified Context Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
    filterContext:
      mode: RELATIVE
      include:
        - {field: Store.Zip Matches, op: "=", value: true}
"""


def _load(yaml_str: str = MODEL_YAML) -> SemanticModel:
    raw, source_map = TrackedLoader().load_string(yaml_str)
    model, result = ReferenceResolver().resolve(raw, source_map)
    assert result.valid, result.errors
    assert SemanticValidator().validate(model) == []
    return model


def _errors(yaml_str: str) -> list[tuple[str, str]]:
    """Validation errors as ``(code, message)`` pairs, resolver then validator."""
    raw, source_map = TrackedLoader().load_string(yaml_str)
    model, result = ReferenceResolver().resolve(raw, source_map)
    found = [(e.code, e.message) for e in result.errors]
    return found or [(e.code, e.message) for e in SemanticValidator().validate(model)]


class TestReferenceDiscovery:
    """``SemanticModel.column_reference_objects`` — what a column pulls in."""

    def test_plain_column_reads_nothing(self) -> None:
        assert _load().column_reference_objects("Store", "Store Zip") == set()

    def test_sibling_only_expression_reads_nothing(self) -> None:
        assert _load().column_reference_objects("Address", "Zip 5") == set()

    def test_qualified_reference_is_reported(self) -> None:
        assert _load().column_reference_objects("Store", "Zip Matches") == {"Address"}

    def test_reference_through_a_sibling_is_reported(self) -> None:
        """``Zip Differs`` reads ``Zip Matches``, which is what reaches Address."""
        assert _load().column_reference_objects("Store", "Zip Differs") == {"Address"}

    def test_owning_object_never_appears(self) -> None:
        assert "Sales" not in _load().column_reference_objects("Sales", "Local Amount")

    def test_unknown_column_reads_nothing(self) -> None:
        assert _load().column_reference_objects("Store", "No Such") == set()


class TestJoinEmission:
    """The referenced object is joined wherever the column is used."""

    def test_dimension_joins_the_referenced_object(self) -> None:
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=["Zip Matches"], measures=["Sales Amount"])),
            _load(),
            "postgres",
        ).sql
        assert 'JOIN "PUBLIC"."CUSTOMER_ADDRESS" AS "Address"' in sql
        assert 'SUBSTRING("Store"."S_ZIP", 1, 5) = SUBSTRING("Address"."CA_ZIP", 1, 5)' in sql

    def test_where_on_a_qualified_computed_column_joins_it(self) -> None:
        sql = PIPELINE.compile(
            QueryObject(
                select=QuerySelect(dimensions=["Store Zip"], measures=["Sales Amount"]),
                where=[
                    QueryFilter(field="Store.Zip Matches", op=FilterOperator.EQUALS, value=False)
                ],
            ),
            _load(),
            "postgres",
        ).sql
        assert 'JOIN "PUBLIC"."CUSTOMER_ADDRESS" AS "Address"' in sql
        assert '"Address"."CA_ZIP"' in sql

    def test_where_on_a_computed_dimension_joins_it(self) -> None:
        sql = PIPELINE.compile(
            QueryObject(
                select=QuerySelect(dimensions=["Store Zip"], measures=["Sales Amount"]),
                where=[QueryFilter(field="Zip Matches", op=FilterOperator.EQUALS, value=True)],
            ),
            _load(),
            "postgres",
        ).sql
        assert 'JOIN "PUBLIC"."CUSTOMER_ADDRESS" AS "Address"' in sql

    def test_measure_over_a_computed_column_joins_it(self) -> None:
        """The measure's own column is computed from another object's column."""
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=[], measures=["Local Sales Amount"])),
            _load(),
            "postgres",
        ).sql
        assert 'JOIN "PUBLIC"."STORE" AS "Store"' in sql
        assert 'JOIN "PUBLIC"."CUSTOMER_ADDRESS" AS "Address"' in sql

    def test_measure_filter_on_a_computed_column_joins_it(self) -> None:
        """The filter is inlined as a CASE WHEN, so it needs the join too."""
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=[], measures=["Matched Zip Amount"])),
            _load(),
            "postgres",
        ).sql
        assert 'JOIN "PUBLIC"."CUSTOMER_ADDRESS" AS "Address"' in sql
        assert '"Address"."CA_ZIP"' in sql

    def test_filter_context_include_joins_it(self) -> None:
        """``filterContext.include`` is resolved by the filter wrapper, which
        runs after join planning and builds its CTE over the joins planned
        here — so the dependency has to be declared before planning, not when
        the wrapper inlines the expression."""
        result = PIPELINE.compile(
            QueryObject(
                select=QuerySelect(dimensions=["Store Zip"], measures=["Context Matched Amount"])
            ),
            _load(),
            "postgres",
        )
        wrapper = result.sql[result.sql.index('"fc_0" AS (') :]
        assert 'JOIN "PUBLIC"."CUSTOMER_ADDRESS" AS "Address"' in wrapper
        assert '"Address"."CA_ZIP"' in wrapper
        assert any(ref.endswith(".CUSTOMER_ADDRESS") for ref in result.physical_tables), (
            result.physical_tables
        )

    def test_qualified_filter_context_include_joins_it(self) -> None:
        """The wrapper accepts a qualified ``DataObject.Column`` include as
        well as a dimension name; both have to be collected before planning."""
        result = PIPELINE.compile(
            QueryObject(
                select=QuerySelect(dimensions=["Store Zip"], measures=["Qualified Context Amount"])
            ),
            _load(),
            "postgres",
        )
        wrapper = result.sql[result.sql.index('"fc_0" AS (') :]
        assert 'JOIN "PUBLIC"."CUSTOMER_ADDRESS" AS "Address"' in wrapper
        assert '"Address"."CA_ZIP"' in wrapper
        assert any(ref.endswith(".CUSTOMER_ADDRESS") for ref in result.physical_tables), (
            result.physical_tables
        )

    def test_object_joined_once_for_two_referencing_columns(self) -> None:
        sql = PIPELINE.compile(
            QueryObject(
                select=QuerySelect(
                    dimensions=["Zip Matches", "Zip Differs"], measures=["Sales Amount"]
                )
            ),
            _load(),
            "postgres",
        ).sql
        assert sql.count('AS "Address"') == 1

    def test_raw_field_joins_the_referenced_object(self) -> None:
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(fields=["Store.Zip Matches"])),
            _load(),
            "postgres",
        ).sql
        assert '"Address"."CA_ZIP"' in sql
        assert 'JOIN "PUBLIC"."CUSTOMER_ADDRESS" AS "Address"' in sql

    @pytest.mark.parametrize("dialect_name", ALL_DIALECTS)
    def test_compiles_per_dialect(self, dialect_name: str) -> None:
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=["Zip Matches"], measures=["Sales Amount"])),
            _load(),
            dialect_name,
        ).sql
        assert "CUSTOMER_ADDRESS" in sql, f"{dialect_name}: referenced object not joined"


class TestUnreachableReference:
    """A reference the query's joins cannot reach is refused, not emitted."""

    UNREACHABLE = MODEL_YAML.replace(
        'expression: "SUBSTRING({Store Zip}, 1, 5) = {[Address].[Zip 5]}"',
        'expression: "{Store Zip} = {[Warehouse].[Warehouse Zip]}"',
    )

    def test_dimension_on_an_unreachable_reference_errors(self) -> None:
        with pytest.raises(ResolutionError) as excinfo:
            PIPELINE.compile(
                QueryObject(
                    select=QuerySelect(dimensions=["Zip Matches"], measures=["Sales Amount"])
                ),
                _load(self.UNREACHABLE),
                "postgres",
            )
        unreachable = [e for e in excinfo.value.errors if e.code == "UNREACHABLE_REQUIRED_OBJECT"]
        assert unreachable, [e.code for e in excinfo.value.errors]
        assert "Warehouse" in unreachable[0].message

    def test_filter_on_an_unreachable_reference_is_skipped(self) -> None:
        """Same contract as a filter whose own object is unreachable: dropped,
        never emitted against an alias the FROM chain does not bind."""
        sql = PIPELINE.compile(
            QueryObject(
                select=QuerySelect(dimensions=["Store Zip"], measures=["Sales Amount"]),
                where=[QueryFilter(field="Zip Matches", op=FilterOperator.EQUALS, value=True)],
            ),
            _load(self.UNREACHABLE),
            "postgres",
        ).sql
        assert "WAREHOUSE" not in sql
        assert "WHERE" not in sql


class TestJoinKeys:
    """A computed column may be a join key — but only while it stays local.

    ``build_join_condition`` inlines the expression into the ON clause, so a
    key that reads another data object names an alias the join itself is about
    to introduce: unbound at best, circular when that object is reachable only
    through this join. There is nothing downstream to repair it with.
    """

    # Sales joins to Store on a key computed from Store's *own* column.
    LOCAL_KEY = """\
version: 1.0

dataObjects:
  Address:
    code: CUSTOMER_ADDRESS
    database: WH
    schema: PUBLIC
    columns:
      Zip: {code: CA_ZIP, abstractType: string}

  Store:
    code: STORE
    database: WH
    schema: PUBLIC
    columns:
      Store Zip: {code: S_ZIP, abstractType: string}
      Store Name: {code: S_NAME, abstractType: string}
      Zip 5:
        expression: "SUBSTRING({Store Zip}, 1, 5)"
        abstractType: string

  Sales:
    code: STORE_SALES
    database: WH
    schema: PUBLIC
    columns:
      Sold Zip: {code: SS_ZIP, abstractType: string}
      Amount: {code: SS_EXT_SALES_PRICE, abstractType: float}
    joins:
      - joinType: many-to-one
        joinTo: Store
        columnsFrom: [Sold Zip]
        columnsTo: [Zip 5]

dimensions:
  Store Name: {dataObject: Store, column: Store Name, resultType: string}

measures:
  Sales Amount:
    columns: [{dataObject: Sales, column: Amount}]
    resultType: float
    aggregation: sum
"""

    # The same model with the key reading Address instead of a sibling.
    CROSS_OBJECT_KEY = LOCAL_KEY.replace(
        'expression: "SUBSTRING({Store Zip}, 1, 5)"',
        'expression: "COALESCE({Store Zip}, {[Address].[Zip]})"',
    )

    def test_local_computed_join_key_still_inlines(self) -> None:
        sql = PIPELINE.compile(
            QueryObject(select=QuerySelect(dimensions=["Store Name"], measures=["Sales Amount"])),
            _load(self.LOCAL_KEY),
            "postgres",
        ).sql
        assert 'ON "Sales"."SS_ZIP" = SUBSTRING("Store"."S_ZIP", 1, 5)' in sql

    def test_cross_object_join_key_rejected(self) -> None:
        errors = _errors(self.CROSS_OBJECT_KEY)
        assert [code for code, _ in errors] == ["CROSS_OBJECT_JOIN_KEY"]
        message = errors[0][1]
        assert "Zip 5" in message and "'Address'" in message

    def test_cross_object_join_key_refused_at_load(self) -> None:
        """The refusal has to land where models are loaded, since that is what
        every serving surface goes through."""
        from orionbelt.service.model_store import ModelStore

        with pytest.raises(Exception) as excinfo:  # noqa: B017 — ModelValidationError
            ModelStore().load_model(self.CROSS_OBJECT_KEY)
        codes = {e.code for e in getattr(excinfo.value, "errors", [])}
        assert "CROSS_OBJECT_JOIN_KEY" in codes, codes


class TestValidation:
    """References that name nothing are rejected at validation time."""

    def test_unknown_data_object_rejected(self) -> None:
        errors = _errors(MODEL_YAML.replace("{[Address].[Zip 5]}", "{[Nope].[Zip 5]}"))
        assert [code for code, _ in errors] == ["UNKNOWN_DATA_OBJECT_IN_EXPRESSION"]

    def test_unknown_column_in_a_known_object_rejected(self) -> None:
        errors = _errors(MODEL_YAML.replace("{[Address].[Zip 5]}", "{[Address].[No Such]}"))
        assert [code for code, _ in errors] == ["UNKNOWN_COLUMN_IN_EXPRESSION"]
        assert "Address" in errors[0][1]

    def test_cycle_across_objects_rejected(self) -> None:
        """The compiler's cycle guard is keyed on (object, column); the
        validator has to see the same graph or the cycle reaches codegen,
        where the fallback emits a computed column's empty ``code``."""
        cyclic = MODEL_YAML.replace(
            '      Zip 5:\n        expression: "SUBSTRING({Zip}, 1, 5)"',
            '      Zip 5:\n        expression: "SUBSTRING({[Store].[Zip Matches]}, 1, 5)"',
        )
        errors = _errors(cyclic)
        assert [code for code, _ in errors] == ["CYCLIC_COMPUTED_COLUMN"]
        walk = errors[0][1].rsplit(": ", 1)[1].split(" -> ")
        assert walk[0] == walk[-1]
        assert "Store.Zip Matches" in walk and "Address.Zip 5" in walk

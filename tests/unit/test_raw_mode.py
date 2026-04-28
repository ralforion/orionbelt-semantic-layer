"""Tests for raw query mode (`select.fields`)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

# Make sure all dialects are registered
import orionbelt.dialect  # noqa: F401
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import QueryResolver, ResolutionError
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    SortDirection,
)
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from tests.conftest import SAMPLE_MODEL_YAML


def _load_model(yaml_content: str = SAMPLE_MODEL_YAML) -> SemanticModel:
    loader = TrackedLoader()
    resolver = ReferenceResolver()
    raw, source_map = loader.load_string(yaml_content)
    model, result = resolver.resolve(raw, source_map)
    assert result.valid, f"Model errors: {[e.message for e in result.errors]}"
    return model


# ---------------------------------------------------------------------------
# QueryObject validation
# ---------------------------------------------------------------------------


class TestQueryObjectValidation:
    def test_raw_mode_with_dimensions_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot be combined with select.dimensions"):
            QueryObject(
                select=QuerySelect(
                    fields=["Customers.Country"],
                    dimensions=["Customer Country"],
                ),
            )

    def test_raw_mode_with_measures_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot be combined with select.measures"):
            QueryObject(
                select=QuerySelect(
                    fields=["Orders.Amount"],
                    measures=["Total Revenue"],
                ),
            )

    def test_raw_mode_with_having_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot be combined with having"):
            QueryObject(
                select=QuerySelect(fields=["Orders.Amount"]),
                having=[QueryFilter(field="Total Revenue", op=FilterOperator.GT, value=0)],
            )

    def test_raw_mode_with_dimensions_exclude_rejected(self) -> None:
        with pytest.raises(ValidationError, match="cannot be combined with dimensionsExclude"):
            QueryObject(
                select=QuerySelect(fields=["Customers.Country"]),
                dimensions_exclude=True,
            )

    def test_distinct_without_fields_rejected(self) -> None:
        with pytest.raises(ValidationError, match="select.distinct is only valid in raw mode"):
            QueryObject(
                select=QuerySelect(
                    dimensions=["Customer Country"],
                    measures=["Total Revenue"],
                    distinct=True,
                ),
            )

    def test_is_raw_property(self) -> None:
        agg = QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        raw = QuerySelect(fields=["Customers.Country"])
        assert agg.is_raw is False
        assert raw.is_raw is True


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class TestRawModeResolver:
    def test_resolves_qualified_field(self) -> None:
        model = _load_model()
        query = QueryObject(select=QuerySelect(fields=["Customers.Country"]))
        resolved = QueryResolver().resolve(query, model)
        assert resolved.is_raw is True
        assert len(resolved.fields) == 1
        f = resolved.fields[0]
        assert f.object_name == "Customers"
        assert f.column_name == "Country"
        assert f.source_column == "COUNTRY"
        assert f.alias == "Customers.Country"

    def test_resolves_multiple_fields_across_joined_objects(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                fields=[
                    "Customers.Country",
                    "Orders.Order ID",
                    "Orders.Amount",
                ]
            ),
        )
        resolved = QueryResolver().resolve(query, model)
        assert resolved.is_raw is True
        # Orders → Customers is m:1, so Orders is the base (most joins)
        assert resolved.base_object == "Orders"
        assert {"Customers", "Orders"} <= resolved.required_objects
        assert len(resolved.join_steps) == 1

    def test_distinct_propagates(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(fields=["Customers.Country"], distinct=True),
        )
        resolved = QueryResolver().resolve(query, model)
        assert resolved.distinct is True

    def test_unknown_object_errors(self) -> None:
        model = _load_model()
        query = QueryObject(select=QuerySelect(fields=["Bogus.Country"]))
        with pytest.raises(ResolutionError) as exc_info:
            QueryResolver().resolve(query, model)
        codes = [e.code for e in exc_info.value.errors]
        assert "RAW_FIELD_UNKNOWN_OBJECT" in codes

    def test_unknown_column_errors(self) -> None:
        model = _load_model()
        query = QueryObject(select=QuerySelect(fields=["Customers.Bogus"]))
        with pytest.raises(ResolutionError) as exc_info:
            QueryResolver().resolve(query, model)
        codes = [e.code for e in exc_info.value.errors]
        assert "RAW_FIELD_UNKNOWN_COLUMN" in codes

    def test_invalid_ref_format_errors(self) -> None:
        model = _load_model()
        query = QueryObject(select=QuerySelect(fields=["Country"]))
        with pytest.raises(ResolutionError) as exc_info:
            QueryResolver().resolve(query, model)
        codes = [e.code for e in exc_info.value.errors]
        assert "RAW_FIELD_INVALID_REF" in codes


# ---------------------------------------------------------------------------
# End-to-end SQL generation
# ---------------------------------------------------------------------------


class TestRawModeCompilation:
    def test_single_object_single_field(self) -> None:
        model = _load_model()
        query = QueryObject(select=QuerySelect(fields=["Customers.Country"]))
        result = CompilationPipeline().compile(query, model, dialect_name="postgres")
        sql = result.sql
        assert "GROUP BY" not in sql
        assert "DISTINCT" not in sql
        assert '"Customers"."COUNTRY"' in sql
        assert 'AS "Customers.Country"' in sql

    def test_distinct_emits_select_distinct(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(fields=["Customers.Country"], distinct=True),
        )
        sql = CompilationPipeline().compile(query, model, dialect_name="postgres").sql
        assert "SELECT DISTINCT" in sql

    def test_join_across_objects(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(
                fields=["Customers.Country", "Orders.Order ID", "Orders.Amount"],
            ),
        )
        sql = CompilationPipeline().compile(query, model, dialect_name="postgres").sql
        assert "GROUP BY" not in sql
        assert "LEFT JOIN" in sql
        assert '"Customers"."COUNTRY"' in sql
        assert '"Orders"."ORDER_ID"' in sql
        assert '"Orders"."AMOUNT"' in sql

    def test_where_filter_qualified(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(fields=["Orders.Order ID", "Orders.Amount"]),
            where=[
                QueryFilter(field="Orders.Amount", op=FilterOperator.GT, value=100),
            ],
        )
        sql = CompilationPipeline().compile(query, model, dialect_name="postgres").sql
        assert "WHERE" in sql
        assert "> 100" in sql

    def test_where_filter_via_dimension(self) -> None:
        # Dimension-name filters still work in raw mode (resolved to columns).
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(fields=["Orders.Order ID", "Customers.Country"]),
            where=[
                QueryFilter(field="Customer Country", op=FilterOperator.EQ, value="DE"),
            ],
        )
        sql = CompilationPipeline().compile(query, model, dialect_name="postgres").sql
        assert "WHERE" in sql
        assert "'DE'" in sql

    def test_order_by_field_alias(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(fields=["Orders.Order ID", "Orders.Amount"]),
            order_by=[
                QueryOrderBy(field="Orders.Amount", direction=SortDirection.DESC),
            ],
        )
        sql = CompilationPipeline().compile(query, model, dialect_name="postgres").sql
        assert "ORDER BY" in sql
        assert "DESC" in sql

    def test_limit_propagates(self) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(fields=["Customers.Country"]),
            limit=42,
        )
        sql = CompilationPipeline().compile(query, model, dialect_name="postgres").sql
        assert "LIMIT 42" in sql

    @pytest.mark.parametrize(
        "dialect_name",
        [
            "bigquery",
            "clickhouse",
            "databricks",
            "dremio",
            "duckdb",
            "mysql",
            "postgres",
            "snowflake",
        ],
    )
    def test_compiles_on_all_dialects(self, dialect_name: str) -> None:
        model = _load_model()
        query = QueryObject(
            select=QuerySelect(fields=["Customers.Country", "Orders.Amount"], distinct=True),
            limit=5,
        )
        result = CompilationPipeline().compile(query, model, dialect_name=dialect_name)
        sql = result.sql
        assert "SELECT DISTINCT" in sql
        assert "GROUP BY" not in sql
        # Sanity: dialect resolved
        assert DialectRegistry.get(dialect_name) is not None

    def test_explain_reports_raw_planner(self) -> None:
        model = _load_model()
        query = QueryObject(select=QuerySelect(fields=["Customers.Country"], distinct=True))
        result = CompilationPipeline().compile(query, model, dialect_name="postgres")
        assert result.explain is not None
        assert result.explain.planner == "Raw"
        assert "DISTINCT" in result.explain.planner_reason


class TestRawModeMultiFact:
    """Multi-fact raw queries (raw CFL) — UNION ALL with NULL padding."""

    MULTI_FACT_YAML = """\
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Name:
        code: NAME
        abstractType: string

  Orders:
    code: ORDERS
    columns:
      Order ID:
        code: ORDER_ID
        abstractType: string
      Order Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Order Customer ID]
        columnsTo: [Customer ID]

  Returns:
    code: RETURNS
    columns:
      Return ID:
        code: RETURN_ID
        abstractType: string
      Return Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Refund:
        code: REFUND
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom: [Return Customer ID]
        columnsTo: [Customer ID]

dimensions:
  Customer Name:
    dataObject: Customers
    column: Name
    resultType: string
"""

    def test_multi_fact_resolution_marks_cfl(self) -> None:
        model = _load_model(self.MULTI_FACT_YAML)
        query = QueryObject(
            select=QuerySelect(
                fields=["Customers.Name", "Orders.Order ID", "Returns.Return ID"],
            ),
        )
        resolved = QueryResolver().resolve(query, model)
        assert resolved.is_raw is True
        assert resolved.requires_cfl is True

    def test_multi_fact_compiles_as_union_all(self) -> None:
        model = _load_model(self.MULTI_FACT_YAML)
        query = QueryObject(
            select=QuerySelect(
                fields=[
                    "Customers.Name",
                    "Orders.Order ID",
                    "Orders.Amount",
                    "Returns.Return ID",
                    "Returns.Refund",
                ],
            ),
        )
        result = CompilationPipeline().compile(query, model, dialect_name="postgres")
        sql = result.sql
        assert "WITH" in sql
        assert "composite_raw_01" in sql
        assert "UNION ALL" in sql
        # Each leg projects every field — non-applicable ones as typed NULL casts.
        # The Orders leg can't project Returns columns; the Returns leg can't
        # project Orders columns.
        assert sql.count("NULL") >= 4
        # GROUP BY must be absent — raw mode never aggregates.
        assert "GROUP BY" not in sql

    def test_multi_fact_distinct_emits_outer_select_distinct(self) -> None:
        model = _load_model(self.MULTI_FACT_YAML)
        query = QueryObject(
            select=QuerySelect(
                fields=["Customers.Name", "Orders.Order ID", "Returns.Return ID"],
                distinct=True,
            ),
        )
        sql = CompilationPipeline().compile(query, model, dialect_name="postgres").sql
        # Outer query carries the DISTINCT, not each leg.
        assert "SELECT DISTINCT" in sql
        assert "UNION ALL" in sql

    def test_multi_fact_explain_reports_legs(self) -> None:
        model = _load_model(self.MULTI_FACT_YAML)
        query = QueryObject(
            select=QuerySelect(
                fields=["Customers.Name", "Orders.Order ID", "Returns.Return ID"],
            ),
        )
        result = CompilationPipeline().compile(query, model, dialect_name="postgres")
        assert result.explain is not None
        assert result.explain.planner == "Raw"
        leg_sources = {leg.measure_source for leg in result.explain.cfl_legs}
        assert leg_sources == {"Orders", "Returns"}

    @pytest.mark.parametrize(
        "dialect_name",
        [
            "bigquery",
            "clickhouse",
            "databricks",
            "dremio",
            "duckdb",
            "mysql",
            "postgres",
            "snowflake",
        ],
    )
    def test_multi_fact_compiles_on_all_dialects(self, dialect_name: str) -> None:
        model = _load_model(self.MULTI_FACT_YAML)
        query = QueryObject(
            select=QuerySelect(
                fields=["Customers.Name", "Orders.Amount", "Returns.Refund"],
                distinct=True,
            ),
            limit=10,
        )
        result = CompilationPipeline().compile(query, model, dialect_name=dialect_name)
        sql = result.sql
        assert "UNION ALL" in sql
        assert "GROUP BY" not in sql
        assert "LIMIT 10" in sql

    def test_multi_fact_with_where_and_order_by(self) -> None:
        model = _load_model(self.MULTI_FACT_YAML)
        query = QueryObject(
            select=QuerySelect(
                fields=[
                    "Customers.Name",
                    "Orders.Amount",
                    "Returns.Refund",
                ],
            ),
            where=[
                QueryFilter(field="Customer Name", op=FilterOperator.EQ, value="Alice"),
            ],
            order_by=[
                QueryOrderBy(field="Customers.Name", direction=SortDirection.ASC),
            ],
            limit=50,
        )
        sql = CompilationPipeline().compile(query, model, dialect_name="postgres").sql
        # Filter on Customers.Name applies to both legs (Customers is reachable from each).
        assert "WHERE" in sql
        assert "'Alice'" in sql
        # ORDER BY must reference the CTE alias, not the underlying table.
        assert "ORDER BY" in sql
        assert '"Customers.Name"' in sql
        assert "LIMIT 50" in sql

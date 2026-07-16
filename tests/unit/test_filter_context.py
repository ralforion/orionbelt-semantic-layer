"""Tests for Phase 2: Filter Context via CTE Isolation."""

from __future__ import annotations

import pytest

from orionbelt.compiler.filter_wrap import (
    _compute_effective_filters,
    _effective_grain_dims,
    _resolve_include_filters,
)
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.compiler.resolution import (
    QueryResolver,
    ResolvedFilter,
    ResolvedMeasure,
)
from orionbelt.models.query import QueryObject
from orionbelt.models.semantic import (
    FilterContext,
    FilterContextFilter,
    FilterContextMode,
    Measure,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

YAML_WITH_FILTER_CONTEXT = """\
version: "1"
dataObjects:
  Orders:
    database: db
    schema: public
    code: orders
    columns:
      Region:
        code: region
        abstractType: string
      Product:
        code: product
        abstractType: string
      Amount:
        code: amount
        abstractType: float
      Color:
        code: color
        abstractType: string
      Year:
        code: year
        abstractType: int
    joins:
      - joinType: many-to-one
        joinTo: Products
        columnsFrom: [Product]
        columnsTo: [Product Name]
  Products:
    database: db
    schema: public
    code: products
    columns:
      Product Name:
        code: product_name
        abstractType: string
      Category:
        code: category
        abstractType: string
dimensions:
  Region:
    dataObject: Orders
    column: Region
  Product:
    dataObject: Orders
    column: Product
  Color:
    dataObject: Orders
    column: Color
  Year:
    dataObject: Orders
    column: Year
  Category:
    dataObject: Products
    column: Category
measures:
  Revenue:
    aggregation: sum
    columns:
      - dataObject: Orders
        column: Amount
  Revenue No Color:
    aggregation: sum
    columns:
      - dataObject: Orders
        column: Amount
    filterContext:
      mode: RELATIVE
      exclude: [Color]
  Unfiltered Revenue:
    aggregation: sum
    columns:
      - dataObject: Orders
        column: Amount
    filterContext:
      mode: FIXED
  Revenue Year Only:
    aggregation: sum
    columns:
      - dataObject: Orders
        column: Amount
    filterContext:
      mode: RELATIVE
      keepOnly: [Year]
  Revenue Red:
    aggregation: sum
    columns:
      - dataObject: Orders
        column: Amount
    filterContext:
      mode: FIXED
      include:
        - field: Color
          op: equals
          value: Red
  Region Revenue Unfiltered:
    aggregation: sum
    columns:
      - dataObject: Orders
        column: Amount
    grain:
      mode: FIXED
      include: [Region]
    filterContext:
      mode: FIXED
  Grand Total Unfiltered:
    aggregation: sum
    columns:
      - dataObject: Orders
        column: Amount
    grain:
      mode: FIXED
    filterContext:
      mode: FIXED
"""


def _load_model(yaml_str: str = YAML_WITH_FILTER_CONTEXT):
    loader = TrackedLoader()
    raw, source_map = loader.load_string(yaml_str)
    resolver = ReferenceResolver()
    model, result = resolver.resolve(raw, source_map)
    assert not result.errors, [e.message for e in result.errors]
    return model


# ---------------------------------------------------------------------------
# 1. Pydantic Model Tests
# ---------------------------------------------------------------------------


class TestFilterContextModel:
    def test_default_mode_is_relative(self):
        fc = FilterContext()
        assert fc.mode == FilterContextMode.RELATIVE

    def test_fixed_mode(self):
        fc = FilterContext(mode=FilterContextMode.FIXED)
        assert fc.mode == FilterContextMode.FIXED

    def test_fixed_with_exclude_raises(self):
        with pytest.raises(ValueError, match="FIXED cannot have 'exclude'"):
            FilterContext(mode=FilterContextMode.FIXED, exclude=["Color"])

    def test_relative_with_exclude(self):
        fc = FilterContext(mode=FilterContextMode.RELATIVE, exclude=["Color"])
        assert fc.exclude == ["Color"]

    def test_keep_only_alias(self):
        fc = FilterContext(keep_only=["Year"])
        assert fc.keep_only == ["Year"]

    def test_include_filters(self):
        fc = FilterContext(include=[FilterContextFilter(field="Color", op="equals", value="Red")])
        assert len(fc.include) == 1
        assert fc.include[0].field == "Color"

    def test_measure_filter_context_field(self):
        m = Measure(
            name="test",
            aggregation="sum",
            filter_context=FilterContext(mode=FilterContextMode.FIXED),
        )
        assert m.filter_context is not None
        assert m.filter_context.mode == FilterContextMode.FIXED

    def test_measure_filter_context_none_by_default(self):
        m = Measure(name="test", aggregation="sum")
        assert m.filter_context is None


# ---------------------------------------------------------------------------
# 2. Filter Computation Tests
# ---------------------------------------------------------------------------


class TestComputeEffectiveFilters:
    def _make_filter(self, fields: set[str]) -> ResolvedFilter:
        from orionbelt.ast.nodes import Literal

        return ResolvedFilter(
            expression=Literal(value="dummy"),
            referenced_fields=frozenset(fields),
        )

    def test_fixed_mode_removes_all(self):
        fc = FilterContext(mode=FilterContextMode.FIXED)
        filters = [self._make_filter({"Year"}), self._make_filter({"Color"})]
        result = _compute_effective_filters(fc, filters)
        assert result == []

    def test_relative_keeps_all(self):
        fc = FilterContext(mode=FilterContextMode.RELATIVE)
        filters = [self._make_filter({"Year"}), self._make_filter({"Color"})]
        result = _compute_effective_filters(fc, filters)
        assert len(result) == 2

    def test_exclude_removes_matching(self):
        fc = FilterContext(exclude=["Color"])
        filters = [self._make_filter({"Year"}), self._make_filter({"Color"})]
        result = _compute_effective_filters(fc, filters)
        assert len(result) == 1
        assert "Year" in result[0].referenced_fields

    def test_exclude_multiple(self):
        fc = FilterContext(exclude=["Color", "Year"])
        filters = [self._make_filter({"Year"}), self._make_filter({"Color"})]
        result = _compute_effective_filters(fc, filters)
        assert result == []

    def test_keep_only_keeps_matching(self):
        fc = FilterContext(keep_only=["Year"])
        filters = [
            self._make_filter({"Year"}),
            self._make_filter({"Color"}),
            self._make_filter({"Region"}),
        ]
        result = _compute_effective_filters(fc, filters)
        assert len(result) == 1
        assert "Year" in result[0].referenced_fields

    def test_exclude_partial_match(self):
        fc = FilterContext(exclude=["Color"])
        filters = [self._make_filter({"Color", "Year"})]
        result = _compute_effective_filters(fc, filters)
        assert result == []


# ---------------------------------------------------------------------------
# 3. Effective Grain Tests
# ---------------------------------------------------------------------------


class TestEffectiveGrainDims:
    def test_no_grain_override_returns_query_dims(self):
        m = ResolvedMeasure(
            name="test",
            aggregation="sum",
            expression=None,  # type: ignore[arg-type]
        )
        result = _effective_grain_dims(m, ["Region", "Product"])
        assert result == ["Region", "Product"]

    def test_with_grain_override(self):
        m = ResolvedMeasure(
            name="test",
            aggregation="sum",
            expression=None,  # type: ignore[arg-type]
            effective_grain=["Region"],
        )
        result = _effective_grain_dims(m, ["Region", "Product"])
        assert result == ["Region"]

    def test_empty_grain_override(self):
        m = ResolvedMeasure(
            name="test",
            aggregation="sum",
            expression=None,  # type: ignore[arg-type]
            effective_grain=[],
        )
        result = _effective_grain_dims(m, ["Region", "Product"])
        assert result == []


# ---------------------------------------------------------------------------
# 4. Resolution Tests
# ---------------------------------------------------------------------------


class TestQueryResolverFilterContext:
    def test_filter_context_stored_on_resolved_measure(self):
        model = _load_model()
        query = QueryObject.model_validate(
            {"select": {"dimensions": ["Region"], "measures": ["Revenue No Color"]}}
        )
        resolver = QueryResolver()
        resolved = resolver.resolve(query, model)
        m = resolved.measures[0]
        assert m.filter_context is not None
        assert m.filter_context.mode == FilterContextMode.RELATIVE
        assert m.filter_context.exclude == ["Color"]

    def test_no_filter_context_is_none(self):
        model = _load_model()
        query = QueryObject.model_validate(
            {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}}
        )
        resolver = QueryResolver()
        resolved = resolver.resolve(query, model)
        assert resolved.measures[0].filter_context is None

    def test_has_filter_context_property(self):
        model = _load_model()
        query = QueryObject.model_validate(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Revenue No Color"],
                }
            }
        )
        resolver = QueryResolver()
        resolved = resolver.resolve(query, model)
        assert resolved.has_filter_context is True

    def test_no_filter_context_property(self):
        model = _load_model()
        query = QueryObject.model_validate(
            {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}}
        )
        resolver = QueryResolver()
        resolved = resolver.resolve(query, model)
        assert resolved.has_filter_context is False

    def test_referenced_fields_on_resolved_filter(self):
        model = _load_model()
        query = QueryObject.model_validate(
            {
                "select": {"dimensions": ["Region"], "measures": ["Revenue"]},
                "where": [{"field": "Year", "op": "equals", "value": 2024}],
            }
        )
        resolver = QueryResolver()
        resolved = resolver.resolve(query, model)
        assert len(resolved.where_filters) == 1
        assert "Year" in resolved.where_filters[0].referenced_fields

    def test_grain_plus_filter_context(self):
        model = _load_model()
        query = QueryObject.model_validate(
            {
                "select": {
                    "dimensions": ["Region", "Product"],
                    "measures": ["Region Revenue Unfiltered"],
                }
            }
        )
        resolver = QueryResolver()
        resolved = resolver.resolve(query, model)
        m = resolved.measures[0]
        assert m.filter_context is not None
        assert m.filter_context.mode == FilterContextMode.FIXED
        assert m.grain_override is not None
        assert m.effective_grain == ["Region"]


# ---------------------------------------------------------------------------
# 5. YAML Parsing Tests
# ---------------------------------------------------------------------------


class TestFilterContextYamlParsing:
    def test_parse_filter_context_from_yaml(self):
        model = _load_model()
        m = model.measures["Revenue No Color"]
        assert m.filter_context is not None
        assert m.filter_context.exclude == ["Color"]

    def test_parse_fixed_filter_context(self):
        model = _load_model()
        m = model.measures["Unfiltered Revenue"]
        assert m.filter_context is not None
        assert m.filter_context.mode == FilterContextMode.FIXED

    def test_parse_keep_only(self):
        model = _load_model()
        m = model.measures["Revenue Year Only"]
        assert m.filter_context is not None
        assert m.filter_context.keep_only == ["Year"]

    def test_parse_include_filters(self):
        model = _load_model()
        m = model.measures["Revenue Red"]
        assert m.filter_context is not None
        assert len(m.filter_context.include) == 1
        assert m.filter_context.include[0].field == "Color"
        assert m.filter_context.include[0].op == "equals"
        assert m.filter_context.include[0].value == "Red"

    def test_unknown_field_in_exclude(self):
        yaml = """\
version: "1"
dataObjects:
  T:
    database: db
    schema: s
    code: t
    columns:
      A:
        code: a
        abstractType: int
dimensions:
  Dim A:
    dataObject: T
    column: A
measures:
  Bad:
    aggregation: sum
    columns:
      - dataObject: T
        column: A
    filterContext:
      exclude: [NonExistent]
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml)
        resolver = ReferenceResolver()
        _, result = resolver.resolve(raw, source_map)
        assert any(e.code == "UNKNOWN_FILTER_CONTEXT_FIELD" for e in result.errors)

    def test_unknown_field_in_include(self):
        yaml = """\
version: "1"
dataObjects:
  T:
    database: db
    schema: s
    code: t
    columns:
      A:
        code: a
        abstractType: int
dimensions:
  Dim A:
    dataObject: T
    column: A
measures:
  Bad:
    aggregation: sum
    columns:
      - dataObject: T
        column: A
    filterContext:
      include:
        - field: NonExistent
          op: equals
          value: 1
"""
        loader = TrackedLoader()
        raw, source_map = loader.load_string(yaml)
        resolver = ReferenceResolver()
        _, result = resolver.resolve(raw, source_map)
        assert any(e.code == "UNKNOWN_FILTER_CONTEXT_FIELD" for e in result.errors)


# ---------------------------------------------------------------------------
# 6. SQL Compilation Tests — Strategy C (same grain, different filters)
# ---------------------------------------------------------------------------


class TestFilterContextSQL:
    def _compile(self, query_dict: dict, dialect: str = "postgres") -> str:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject.model_validate(query_dict)
        result = pipeline.compile(query, model, dialect)
        return result.sql

    def test_exclude_filter_creates_cte(self):
        sql = self._compile(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Revenue No Color"],
                },
                "where": [
                    {"field": "Year", "op": "equals", "value": 2024},
                    {"field": "Color", "op": "equals", "value": "Red"},
                ],
            }
        )
        assert "main" in sql.lower()
        assert "fc_0" in sql.lower()
        assert "LEFT JOIN" in sql.upper() or "left join" in sql.lower()

    def test_fixed_mode_no_where(self):
        sql = self._compile(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Unfiltered Revenue"],
                },
                "where": [{"field": "Year", "op": "equals", "value": 2024}],
            }
        )
        assert "main" in sql.lower()
        assert "fc_0" in sql.lower()

    def test_keep_only_preserves_matching(self):
        sql = self._compile(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Revenue Year Only"],
                },
                "where": [
                    {"field": "Year", "op": "equals", "value": 2024},
                    {"field": "Color", "op": "equals", "value": "Red"},
                ],
            }
        )
        assert "fc_0" in sql.lower()

    def test_include_adds_filter(self):
        sql = self._compile(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Revenue Red"],
                },
            }
        )
        assert "fc_0" in sql.lower()
        assert "'Red'" in sql

    def test_no_filter_context_no_wrap(self):
        sql = self._compile(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue"],
                },
                "where": [{"field": "Year", "op": "equals", "value": 2024}],
            }
        )
        assert "main" not in sql.lower() or "fc_0" not in sql.lower()

    def test_grain_plus_filter_creates_cte(self):
        sql = self._compile(
            {
                "select": {
                    "dimensions": ["Region", "Product"],
                    "measures": ["Revenue", "Region Revenue Unfiltered"],
                },
                "where": [{"field": "Year", "op": "equals", "value": 2024}],
            }
        )
        assert "fc_0" in sql.lower()

    def test_scalar_cross_join(self):
        sql = self._compile(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Grand Total Unfiltered"],
                },
                "where": [{"field": "Year", "op": "equals", "value": 2024}],
            }
        )
        assert "CROSS JOIN" in sql.upper() or "cross join" in sql.lower()


# ---------------------------------------------------------------------------
# 7. Multi-Dialect SQL Tests
# ---------------------------------------------------------------------------


class TestFilterContextDialects:
    DIALECTS = [
        "postgres",
        "bigquery",
        "snowflake",
        "duckdb",
        "clickhouse",
        "mysql",
        "databricks",
        "dremio",
    ]

    def _compile(self, dialect: str) -> str:
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject.model_validate(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Revenue No Color"],
                },
                "where": [
                    {"field": "Year", "op": "equals", "value": 2024},
                    {"field": "Color", "op": "equals", "value": "Red"},
                ],
            }
        )
        return pipeline.compile(query, model, dialect).sql

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_filter_context_all_dialects(self, dialect: str):
        sql = self._compile(dialect)
        assert "fc_0" in sql.lower()
        assert "main" in sql.lower()

    @pytest.mark.parametrize("dialect", DIALECTS)
    def test_fixed_filter_context_all_dialects(self, dialect: str):
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject.model_validate(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Unfiltered Revenue"],
                },
                "where": [{"field": "Year", "op": "equals", "value": 2024}],
            }
        )
        result = pipeline.compile(query, model, dialect)
        assert "fc_0" in result.sql.lower()


# ---------------------------------------------------------------------------
# 8. Include Filter Resolution Tests
# ---------------------------------------------------------------------------


class TestIncludeFilterResolution:
    def test_resolve_include_by_dimension_name(self):
        model = _load_model()
        fc = FilterContext(
            mode=FilterContextMode.FIXED,
            include=[FilterContextFilter(field="Color", op="equals", value="Red")],
        )
        results = _resolve_include_filters(fc, model)
        assert len(results) == 1
        assert "Color" in results[0].referenced_fields

    def test_resolve_include_unknown_field_skipped(self):
        model = _load_model()
        fc = FilterContext(
            mode=FilterContextMode.FIXED,
            include=[FilterContextFilter(field="NonExistent", op="equals", value="x")],
        )
        results = _resolve_include_filters(fc, model)
        assert results == []


# ---------------------------------------------------------------------------
# 9. ExplainPlan Tests
# ---------------------------------------------------------------------------


class TestFilterContextExplain:
    def test_explain_has_filter_context(self):
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject.model_validate(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Revenue No Color"],
                },
                "where": [{"field": "Color", "op": "equals", "value": "Red"}],
            }
        )
        result = pipeline.compile(query, model, "postgres")
        assert result.explain is not None
        assert result.explain.has_filter_context is True

    def test_explain_no_filter_context(self):
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject.model_validate(
            {"select": {"dimensions": ["Region"], "measures": ["Revenue"]}}
        )
        result = pipeline.compile(query, model, "postgres")
        assert result.explain is not None
        assert result.explain.has_filter_context is False


# ---------------------------------------------------------------------------
# 10. Edge Cases
# ---------------------------------------------------------------------------


class TestFilterContextEdgeCases:
    def test_multiple_isolated_measures_different_contexts(self):
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject.model_validate(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": [
                        "Revenue",
                        "Revenue No Color",
                        "Unfiltered Revenue",
                    ],
                },
                "where": [
                    {"field": "Year", "op": "equals", "value": 2024},
                    {"field": "Color", "op": "equals", "value": "Red"},
                ],
            }
        )
        result = pipeline.compile(query, model, "postgres")
        assert "fc_0" in result.sql.lower()
        assert "fc_1" in result.sql.lower()

    def test_filter_context_without_query_filters(self):
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject.model_validate(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Revenue No Color"],
                },
            }
        )
        result = pipeline.compile(query, model, "postgres")
        assert "fc_0" in result.sql.lower()

    def test_exclude_nonexistent_filter_is_noop(self):
        model = _load_model()
        pipeline = CompilationPipeline()
        query = QueryObject.model_validate(
            {
                "select": {
                    "dimensions": ["Region"],
                    "measures": ["Revenue", "Revenue No Color"],
                },
                "where": [{"field": "Year", "op": "equals", "value": 2024}],
            }
        )
        result = pipeline.compile(query, model, "postgres")
        assert result.sql_valid or not result.warnings

"""Tests for static model filters — WHERE conditions applied to every query."""

from __future__ import annotations

import pytest

from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver
from orionbelt.parser.validator import SemanticValidator

PIPELINE = CompilationPipeline()
LOADER = TrackedLoader()
RESOLVER = ReferenceResolver()
VALIDATOR = SemanticValidator()


def _load_model(yaml_str: str):
    raw, sm = LOADER.load_string(yaml_str)
    model, result = RESOLVER.resolve(raw, sm)
    assert result.valid, f"Model has errors: {result.errors}"
    return model


BASE_MODEL = """\
version: 1.0

dataObjects:
  Customers:
    code: CUSTOMERS
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Customer ID:
        code: CUSTOMER_ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string
      Region:
        code: REGION
        abstractType: string

  Orders:
    code: ORDERS
    database: WAREHOUSE
    schema: PUBLIC
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
      Status:
        code: STATUS
        abstractType: string
      Order Date:
        code: ORDER_DATE
        abstractType: date
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Order Customer ID
        columnsTo:
          - Customer ID

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string
  Order Date:
    dataObject: Orders
    column: Order Date
    resultType: date

measures:
  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
"""


def _model_with_filters(filters_yaml: str) -> str:
    return BASE_MODEL + f"\nfilters:\n{filters_yaml}"


class TestStaticFilterCompilation:
    """Static filters generate correct WHERE clauses."""

    def test_single_equals_filter(self):
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Status
    operator: equals
    value: completed""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "\"STATUS\" = 'completed'" in result.sql

    def test_filter_combined_with_query_filter(self):
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Status
    operator: equals
    value: completed""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"]),
            where=[{"field": "Customer Country", "op": "equals", "value": "Germany"}],
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "\"STATUS\" = 'completed'" in result.sql
        assert "\"COUNTRY\" = 'Germany'" in result.sql

    def test_duplicate_query_filter_skipped(self):
        """Query-time filter identical to a static filter is not duplicated."""
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Status
    operator: equals
    value: completed""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"]),
            where=[{"field": "Orders.Status", "op": "equals", "value": "completed"}],
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert result.sql.count("\"STATUS\" = 'completed'") == 1

    def test_multiple_static_filters_and(self):
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Status
    operator: equals
    value: completed
  - dataObject: Customers
    column: Region
    operator: equals
    value: EMEA""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "\"STATUS\" = 'completed'" in result.sql
        assert "\"REGION\" = 'EMEA'" in result.sql

    def test_in_list_filter(self):
        yaml = _model_with_filters("""\
  - dataObject: Customers
    column: Region
    operator: in
    values:
      - EMEA
      - APAC""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "'EMEA'" in result.sql
        assert "'APAC'" in result.sql

    def test_not_equals_filter(self):
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Status
    operator: "!="
    value: cancelled""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "\"STATUS\" <> 'cancelled'" in result.sql

    def test_is_not_null_filter(self):
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Status
    operator: is_not_null""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert '"STATUS" IS NOT NULL' in result.sql

    def test_date_gte_filter(self):
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Order Date
    operator: ">="
    value: "2026-01-01\"""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "\"ORDER_DATE\" >= '2026-01-01'" in result.sql

    def test_date_between_filter(self):
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Order Date
    operator: between
    values:
      - "2026-01-01"
      - "2026-12-31\"""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "'2026-01-01'" in result.sql
        assert "'2026-12-31'" in result.sql
        assert "BETWEEN" in result.sql

    def test_bare_yaml_date_coerced(self):
        """ruamel.yaml parses bare 2026-01-01 as datetime.date — we coerce to string."""
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Order Date
    operator: ">="
    value: 2026-01-01""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "\"ORDER_DATE\" >= '2026-01-01'" in result.sql

    def test_bare_yaml_timestamp_coerced(self):
        """ruamel.yaml parses bare timestamps — we coerce to ISO string."""
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Order Date
    operator: ">="
    value: 2026-01-01 14:30:00""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "'2026-01-01" in result.sql

    def test_iso_timestamp_with_timezone(self):
        """ISO 8601 timestamp with timezone (ruamel.yaml TimeStamp) is coerced."""
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Order Date
    operator: ">="
    value: 2026-01-01T14:30:00+02:00""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "'2026-01-01T14:30:00+02:00'" in result.sql

    def test_iso_timestamp_utc(self):
        """ISO 8601 timestamp with Z suffix is coerced."""
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Order Date
    operator: ">="
    value: 2026-01-01T00:00:00Z""")
        model = _load_model(yaml)
        query = QueryObject(
            select=QuerySelect(dimensions=["Customer Country"], measures=["Total Revenue"])
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "'2026-01-01T00:00:00+00:00'" in result.sql

    def test_filter_auto_joins_dimension_table(self):
        """A static filter on a dimension table auto-extends the join path."""
        yaml = _model_with_filters("""\
  - dataObject: Customers
    column: Region
    operator: equals
    value: EMEA""")
        model = _load_model(yaml)
        query = QueryObject(select=QuerySelect(measures=["Total Revenue"]))
        result = PIPELINE.compile(query, model, "postgres")
        assert "\"REGION\" = 'EMEA'" in result.sql
        assert "LEFT JOIN" in result.sql


class TestStaticFilterValidation:
    """Static filter validation catches invalid references."""

    def test_unknown_data_object(self):
        yaml = _model_with_filters("""\
  - dataObject: Unknown
    column: Foo
    operator: equals
    value: bar""")
        raw, sm = LOADER.load_string(yaml)
        _model, result = RESOLVER.resolve(raw, sm)
        assert not result.valid
        codes = [e.code for e in result.errors]
        assert "UNKNOWN_FILTER_DATA_OBJECT" in codes

    def test_unknown_column(self):
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: NonExistent
    operator: equals
    value: bar""")
        raw, sm = LOADER.load_string(yaml)
        _model, result = RESOLVER.resolve(raw, sm)
        assert not result.valid
        codes = [e.code for e in result.errors]
        assert "UNKNOWN_FILTER_COLUMN" in codes

    def test_filters_not_a_list(self):
        yaml = BASE_MODEL + "\nfilters:\n  bad: value\n"
        raw, sm = LOADER.load_string(yaml)
        _model, result = RESOLVER.resolve(raw, sm)
        assert not result.valid
        codes = [e.code for e in result.errors]
        assert "FILTER_PARSE_ERROR" in codes


class TestStaticFilterOnModel:
    """Static filters are stored on the SemanticModel."""

    def test_model_has_filters(self):
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Status
    operator: equals
    value: completed""")
        model = _load_model(yaml)
        assert len(model.filters) == 1
        assert model.filters[0].data_object == "Orders"
        assert model.filters[0].column == "Status"
        assert model.filters[0].operator == "equals"
        assert model.filters[0].value == "completed"

    def test_model_without_filters(self):
        model = _load_model(BASE_MODEL)
        assert model.filters == []

    def test_multiple_filters(self):
        yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Status
    operator: equals
    value: completed
  - dataObject: Customers
    column: Region
    operator: equals
    value: EMEA""")
        model = _load_model(yaml)
        assert len(model.filters) == 2


class TestStaticFilterAPI:
    """Static filters appear in API schema response."""

    @pytest.mark.anyio
    async def test_schema_includes_filters(self):
        import httpx

        from orionbelt.api.app import create_app
        from orionbelt.api.deps import init_session_manager, reset_session_manager
        from orionbelt.service.session_manager import SessionManager
        from orionbelt.settings import Settings

        settings = Settings(session_ttl_seconds=3600, session_cleanup_interval=9999)
        app = create_app(settings=settings)
        mgr = SessionManager(
            ttl_seconds=settings.session_ttl_seconds,
            cleanup_interval=settings.session_cleanup_interval,
        )
        init_session_manager(mgr)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                r = await client.post("/v1/sessions")
                sid = r.json()["session_id"]

                yaml = _model_with_filters("""\
  - dataObject: Orders
    column: Status
    operator: equals
    value: completed""")
                r = await client.post(
                    f"/v1/sessions/{sid}/models",
                    json={"model_yaml": yaml},
                )
                assert r.status_code == 201
                mid = r.json()["model_id"]

                r = await client.get(f"/v1/sessions/{sid}/models/{mid}/schema")
                assert r.status_code == 200
                schema = r.json()
                assert len(schema["filters"]) == 1
                assert schema["filters"][0]["data_object"] == "Orders"
                assert schema["filters"][0]["column"] == "Status"
                assert schema["filters"][0]["operator"] == "equals"
                assert schema["filters"][0]["value"] == "completed"
        finally:
            reset_session_manager()


class TestQueryFilterDateValues:
    """Query-time filters accept date and timestamp values."""

    def test_query_filter_date_string(self):
        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Order Date"], measures=["Total Revenue"]),
            where=[{"field": "Order Date", "op": ">=", "value": "2026-01-01"}],
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "'2026-01-01'" in result.sql

    def test_query_filter_date_object_coerced(self):
        """Python datetime.date objects are coerced to ISO strings in QueryFilter."""
        from datetime import date

        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Order Date"], measures=["Total Revenue"]),
            where=[{"field": "Order Date", "op": ">=", "value": date(2026, 1, 1)}],
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "'2026-01-01'" in result.sql

    def test_query_filter_datetime_object_coerced(self):
        """Python datetime.datetime objects are coerced to ISO strings in QueryFilter."""
        from datetime import datetime

        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Order Date"], measures=["Total Revenue"]),
            where=[{"field": "Order Date", "op": ">=", "value": datetime(2026, 1, 1, 14, 30)}],
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "'2026-01-01T14:30:00'" in result.sql

    def test_query_filter_between_date_objects(self):
        """Date objects in list values (for between) are coerced."""
        from datetime import date

        model = _load_model(BASE_MODEL)
        query = QueryObject(
            select=QuerySelect(dimensions=["Order Date"], measures=["Total Revenue"]),
            where=[
                {
                    "field": "Order Date",
                    "op": "between",
                    "value": [date(2026, 1, 1), date(2026, 12, 31)],
                }
            ],
        )
        result = PIPELINE.compile(query, model, "postgres")
        assert "'2026-01-01'" in result.sql
        assert "'2026-12-31'" in result.sql
        assert "BETWEEN" in result.sql

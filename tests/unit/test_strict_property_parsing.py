"""Strict-parsing tests for OBML (v2.7.2+) — unknown YAML keys on any OBML
object or QueryObject must be rejected with the ``UNKNOWN_PROPERTY`` error
code instead of being silently dropped.

The bug this guards against: a measure declared with ``filtter:`` (typo) used
to validate clean and compile to SQL with no filter applied — the exact class
of bug a semantic layer is supposed to prevent.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orionbelt.models.query import (
    QueryFilter,
    QueryObject,
    QueryOrderBy,
    QuerySelect,
    Subquery,
    UsePathName,
)
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver


def _resolve(yaml_text: str) -> tuple[list[str], list[str]]:
    """Return (error_codes, error_paths) from resolving the given OBML YAML."""
    loader = TrackedLoader()
    raw, source_map = loader.load_string(yaml_text)
    _, result = ReferenceResolver().resolve(raw, source_map)
    return (
        [e.code for e in result.errors],
        [e.path or "" for e in result.errors],
    )


_BASE = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    columns:
      ID:
        code: ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
        numClass: additive
dimensions:
  Order ID:
    dataObject: Orders
    column: ID
    resultType: string
measures:
  Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    aggregation: sum
    resultType: float
"""


class TestOBMLStrictParsing:
    def test_unknown_top_level_key(self) -> None:
        codes, _ = _resolve(_BASE + "datObjects: {}\n")  # typo, but doesn't override the real key
        assert "UNKNOWN_PROPERTY" in codes

    def test_unknown_data_object_key(self) -> None:
        yaml = _BASE.replace(
            "  Orders:\n    code: ORDERS",
            "  Orders:\n    code: ORDERS\n    descriptio: oops",
        )
        codes, paths = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes
        assert any("dataObjects.Orders" in p for p in paths)

    def test_unknown_column_key(self) -> None:
        yaml = _BASE.replace(
            "      ID:\n        code: ID\n        abstractType: string",
            "      ID:\n        code: ID\n        abstractType: string\n        sqlTpe: VARCHAR",
        )
        codes, paths = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes
        assert any("dataObjects.Orders.columns.ID" in p for p in paths)

    def test_unknown_join_key(self) -> None:
        yaml = (
            _BASE
            + """\
  Customers:
    code: CUSTOMERS
    columns:
      CID:
        code: CID
        abstractType: string
"""
        )
        # Now add a join with a typo on Orders
        yaml = yaml.replace(
            "  Orders:\n    code: ORDERS\n    columns:",
            "  Orders:\n    code: ORDERS\n    joins:\n"
            "      - joinType: many-to-one\n"
            "        joinTo: Customers\n"
            "        columnsFrom: [ID]\n"
            "        columnsTo: [CID]\n"
            "        secondry: true\n"
            "    columns:",
        )
        codes, paths = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes
        assert any("dataObjects.Orders.joins" in p for p in paths)

    def test_unknown_dimension_key(self) -> None:
        old = "  Order ID:\n    dataObject: Orders\n    column: ID\n    resultType: string"
        yaml = _BASE.replace(old, old + "\n    formt: '%s'")
        codes, paths = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes
        assert any("dimensions.Order ID" in p for p in paths)

    def test_unknown_measure_key(self) -> None:
        old = "    aggregation: sum\n    resultType: float\n"
        new = old + "    filtter:\n      operator: equals\n      values: []\n"
        codes, paths = _resolve(_BASE.replace(old, new))
        assert "UNKNOWN_PROPERTY" in codes
        assert any("measures.Revenue" in p for p in paths)

    def test_unknown_measure_filter_key(self) -> None:
        yaml = _BASE.replace(
            "    aggregation: sum\n    resultType: float\n",
            "    aggregation: sum\n    resultType: float\n"
            "    filters:\n"
            "      - column:\n          dataObject: Orders\n          column: Amount\n"
            "        operator: gt\n"
            "        oprator: gt\n",  # typo
        )
        codes, _ = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes

    def test_unknown_filter_context_key(self) -> None:
        yaml = _BASE.replace(
            "    aggregation: sum\n    resultType: float\n",
            "    aggregation: sum\n    resultType: float\n"
            "    filterContext:\n      mod: FIXED\n",  # typo: mod instead of mode
        )
        codes, _ = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes

    def test_unknown_grain_key(self) -> None:
        yaml = _BASE.replace(
            "    aggregation: sum\n    resultType: float\n",
            "    aggregation: sum\n    resultType: float\n"
            "    grain:\n      mode: FIXED\n      includ: [Order ID]\n",  # typo
        )
        codes, _ = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes

    def test_unknown_metric_key(self) -> None:
        yaml = (
            _BASE
            + """\
metrics:
  Double Revenue:
    expression: "{[Revenue]} * 2"
    formt: '%f'
"""
        )
        codes, paths = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes
        assert any("metrics.Double Revenue" in p for p in paths)

    def test_unknown_model_filter_key(self) -> None:
        yaml = (
            _BASE
            + """\
filters:
  - dataObject: Orders
    column: Amount
    operator: gt
    valeu: 0
"""
        )
        codes, paths = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes
        assert any("filters[0]" in p for p in paths)

    def test_unknown_settings_key(self) -> None:
        yaml = (
            _BASE
            + """\
settings:
  defaultTimezone: UTC
  defaultDialct: postgres
"""
        )
        codes, _ = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes

    def test_unknown_example_key(self) -> None:
        yaml = (
            _BASE
            + """\
examples:
  - name: ex1
    description: an example
    quary: {}
"""
        )
        codes, _ = _resolve(yaml)
        assert "UNKNOWN_PROPERTY" in codes

    def test_valid_model_has_no_unknown_property_errors(self) -> None:
        codes, _ = _resolve(_BASE)
        assert "UNKNOWN_PROPERTY" not in codes


class TestQueryObjectStrictParsing:
    """Pydantic ``extra='forbid'`` catches typos on the QueryObject surface."""

    def test_unknown_query_object_key(self) -> None:
        with pytest.raises(ValidationError) as exc:
            QueryObject.model_validate(
                {
                    "select": {"dimensions": [], "measures": ["Revenue"]},
                    "where": [],
                    "limt": 10,  # typo
                }
            )
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_unknown_query_filter_key(self) -> None:
        with pytest.raises(ValidationError) as exc:
            QueryFilter.model_validate({"field": "x", "op": "=", "valeu": 1})
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_unknown_subquery_key(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Subquery.model_validate({"dataObject": "Orders", "pathNam": "alt"})
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_unknown_select_key(self) -> None:
        with pytest.raises(ValidationError) as exc:
            QuerySelect.model_validate({"dimensions": [], "measures": ["X"], "distict": True})
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_unknown_order_by_key(self) -> None:
        with pytest.raises(ValidationError) as exc:
            QueryOrderBy.model_validate({"field": "x", "directon": "asc"})
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_unknown_use_path_name_key(self) -> None:
        with pytest.raises(ValidationError) as exc:
            UsePathName.model_validate(
                {"source": "A", "target": "B", "pathName": "alt", "extra": True}
            )
        assert any(e["type"] == "extra_forbidden" for e in exc.value.errors())

    def test_valid_query_object_unchanged(self) -> None:
        q = QueryObject.model_validate(
            {
                "select": {"measures": ["Revenue"]},
                "where": [{"field": "Order ID", "op": "=", "value": "X"}],
                "limit": 10,
            }
        )
        assert q.limit == 10

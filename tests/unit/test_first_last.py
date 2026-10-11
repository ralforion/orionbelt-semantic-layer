"""``first`` / ``last`` measures: model, compile, dialects.

The answers are asserted on all eight engines in
``tests/integration/drift/vendor_exec/test_first_last_exec.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError

from orionbelt.ast.nodes import ColumnRef, FunctionCall, OrderByItem
from orionbelt.compiler.grain_dedup import MULTIPLICITY_SAFE_AGGREGATIONS
from orionbelt.compiler.pipeline import CompilationPipeline
from orionbelt.dialect.base import UnsupportedAggregationError
from orionbelt.dialect.registry import DialectRegistry
from orionbelt.models.query import QueryObject, QuerySelect
from orionbelt.models.semantic import Measure, SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schema" / "obml-schema.json").read_text()
)

MODEL_YAML = """\
version: 1.0
dataObjects:
  Trades:
    code: TRADES
    database: WAREHOUSE
    schema: PUBLIC
    columns:
      Ticker: {code: TICKER, abstractType: string}
      Price: {code: PRICE, abstractType: float}
      Traded At: {code: TRADED_AT, abstractType: timestamp}
dimensions:
  Ticker: {dataObject: Trades, column: Ticker, resultType: string}
measures:
"""

CLOSE = """\
  Close:
    columns: [{dataObject: Trades, column: Price}]
    aggregation: last
    withinGroup: {column: {dataObject: Trades, column: Traded At}}
"""


def _resolve(measures: str) -> tuple[SemanticModel, list[str]]:
    raw, source_map = TrackedLoader().load_string(MODEL_YAML + measures)
    model, result = ReferenceResolver().resolve(raw, source_map)
    return model, [e.message for e in result.errors]


def _sql(measures: str, measure: str = "Close", dialect: str = "postgres") -> str:
    model, errors = _resolve(measures)
    assert not errors, errors
    query = QueryObject(select=QuerySelect(dimensions=["Ticker"], measures=[measure]))
    return CompilationPipeline().compile(query, model, dialect).sql


def _measure(**fields: Any) -> Measure:
    base: dict[str, Any] = {
        "name": "M",
        "columns": [{"dataObject": "Trades", "column": "Price"}],
        "aggregation": "last",
        "withinGroup": {"column": {"dataObject": "Trades", "column": "Traded At"}},
    }
    return Measure.model_validate({**base, **fields})


class TestMeasureValidation:
    @pytest.mark.parametrize("aggregation", ["first", "last", "LAST"])
    def test_valid(self, aggregation: str) -> None:
        assert _measure(aggregation=aggregation).within_group is not None

    @pytest.mark.parametrize(
        ("fields", "message"),
        [
            ({"withinGroup": None}, "requires 'withinGroup'"),
            (
                {
                    "withinGroup": {
                        "column": {"dataObject": "Trades", "column": "Traded At"},
                        "order": "DESC",
                    }
                },
                "takes no 'withinGroup.order'",
            ),
            ({"columns": []}, "exactly 1 column"),
            ({"distinct": True}, "does not take 'distinct'"),
        ],
    )
    def test_refused(self, fields: dict[str, Any], message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            _measure(**fields)

    def test_expression_allowed(self) -> None:
        assert _measure(columns=[], expression="{[Trades].[Price]} * 2").expression

    def test_multiplicity_safe(self) -> None:
        """A repeated row repeats its (key, value) pair: as safe as min / max."""
        assert {"first", "last"} <= MULTIPLICITY_SAFE_AGGREGATIONS


class TestCompile:
    def test_last_orders_descending(self) -> None:
        sql = _sql(CLOSE)
        assert (
            '(ARRAY_AGG("Trades"."PRICE" ORDER BY "Trades"."TRADED_AT" DESC, "Trades"."PRICE" DESC)'
            in sql
        )

    def test_first_orders_ascending(self) -> None:
        sql = _sql(CLOSE.replace("aggregation: last", "aggregation: first"))
        assert 'ORDER BY "Trades"."TRADED_AT" ASC, "Trades"."PRICE" ASC' in sql

    def test_filter_wraps_the_value_not_the_key(self) -> None:
        filtered = CLOSE + (
            "    filters:\n"
            "      - column: {dataObject: Trades, column: Ticker}\n"
            "        operator: equals\n"
            "        values: [{dataType: string, valueString: ACME}]\n"
        )
        sql = _sql(filtered)
        assert 'ORDER BY "Trades"."TRADED_AT" DESC, CASE WHEN' in sql

    def test_expression_measure_keeps_the_key(self) -> None:
        expression = (
            "  Close:\n    expression: '{[Trades].[Price]} * 2'\n    aggregation: last\n"
            "    withinGroup: {column: {dataObject: Trades, column: Traded At}}\n"
        )
        assert 'ORDER BY "Trades"."TRADED_AT" DESC' in _sql(expression)

    def test_untyped_result(self) -> None:
        assert "AS DECIMAL" not in _sql(CLOSE)

    def test_total_refused(self) -> None:
        with pytest.raises(Exception, match="does not support total"):
            _sql(CLOSE + "    total: true\n")

    @pytest.mark.parametrize("dialect", ["mysql", "dremio"])
    def test_refused(self, dialect: str) -> None:
        """MySQL has no ordered aggregate; Dremio's answers an empty array for
        a NULL group key."""
        with pytest.raises(UnsupportedAggregationError, match="LAST"):
            _sql(CLOSE, dialect=dialect)


def _call(name: str) -> FunctionCall:
    return FunctionCall(
        name=name,
        args=[ColumnRef(name="v")],
        order_by=[OrderByItem(expr=ColumnRef(name="k"), desc=name == "LAST")],
    )


MULTI_FACT_YAML = """\
version: 1.0
settings:
  queryTimezone: Europe/Berlin
dataObjects:
  Tickers:
    code: TICKERS
    database: W
    schema: P
    columns:
      Ticker: {code: TICKER, abstractType: string}
  Trades:
    code: TRADES
    database: W
    schema: P
    columns:
      Trade Ticker: {code: TICKER, abstractType: string}
      Price: {code: PRICE, abstractType: float}
      Seq: {code: SEQ, abstractType: float, sqlPrecision: 20, sqlScale: 0}
      Traded At: {code: TRADED_AT, abstractType: timestamp_tz}
    joins:
      - {joinType: many-to-one, joinTo: Tickers, columnsFrom: [Trade Ticker], columnsTo: [Ticker]}
  Quotes:
    code: QUOTES
    database: W
    schema: P
    columns:
      Quote Ticker: {code: TICKER, abstractType: string}
      Bid: {code: BID, abstractType: float}
    joins:
      - {joinType: many-to-one, joinTo: Tickers, columnsFrom: [Quote Ticker], columnsTo: [Ticker]}
dimensions:
  Ticker: {dataObject: Tickers, column: Ticker, resultType: string}
measures:
  Bids:
    columns: [{dataObject: Quotes, column: Bid}]
    aggregation: sum
  Close:
    columns: [{dataObject: Trades, column: Price}]
    aggregation: last
    withinGroup: {column: {dataObject: Trades, column: Traded At}}
  Last By Seq:
    columns: [{dataObject: Trades, column: Price}]
    aggregation: last
    withinGroup: {column: {dataObject: Trades, column: Seq}}
"""


class TestMultiFactKeys:
    """The legs that pad a sort key cast it to the type the owning leg's key has."""

    def _sql(self, measure: str, dialect: str) -> str:
        raw, source_map = TrackedLoader().load_string(MULTI_FACT_YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        query = QueryObject(select=QuerySelect(dimensions=["Ticker"], measures=["Bids", measure]))
        return CompilationPipeline().compile(query, model, dialect).sql

    def test_decimal_key_keeps_its_declared_width(self) -> None:
        """As FLOAT, 9007199254740993 tied with 9007199254740992 on Postgres."""
        sql = self._sql("Last By Seq", "postgres")
        assert 'CAST(NULL AS DECIMAL(20, 0)) AS "Last By Seq__wg"' in sql
        assert "AS FLOAT" not in sql

    def test_instant_key_in_no_query_timezone(self) -> None:
        """Converted, a timestamp_tz key was a DATETIME on BigQuery beside TIMESTAMP pads,
        and ordered by wall-clock time, which repeats an hour where a zone falls back."""
        sql = self._sql("Close", "bigquery")
        assert "`Trades`.`TRADED_AT` AS `Close__wg`" in sql
        assert "DATETIME" not in sql

    # DuckDB and Snowflake union by name, with no pads at all.
    @pytest.mark.parametrize("dialect", ["bigquery", "clickhouse", "databricks"])
    def test_bare_pads_and_an_uncast_key_where_nulls_resolve(self, dialect: str) -> None:
        """A typed pad is a guess from the model: cast to it, a DateTime64 key lost
        its milliseconds on ClickHouse and a double of 1e-25 its value."""
        sql = self._sql("Last By Seq", dialect)
        quote = "`" if dialect in ("bigquery", "databricks") else '"'
        alias = f"{quote}Last By Seq__wg{quote}"
        assert f"NULL AS {alias}" in sql and "CAST(NULL AS" not in sql.split(alias)[0][-40:]
        assert f"{quote}Trades{quote}.{quote}SEQ{quote} AS {alias}" in sql


class TestListaggUnderAQueryTimezone:
    """A LISTAGG ordered by its own timestamp stays self-ordered in the query's time."""

    YAML = MULTI_FACT_YAML + (
        "  Trade Times:\n"
        "    columns: [{dataObject: Trades, column: Traded At}]\n"
        "    aggregation: listagg\n"
        "    withinGroup: {column: {dataObject: Trades, column: Traded At}}\n"
    )

    @pytest.mark.parametrize("dialect", ["clickhouse", "databricks"])
    def test_compiles(self, dialect: str) -> None:
        raw, source_map = TrackedLoader().load_string(self.YAML)
        model, result = ReferenceResolver().resolve(raw, source_map)
        assert result.valid, result.errors
        query = QueryObject(select=QuerySelect(dimensions=["Ticker"], measures=["Trade Times"]))
        assert CompilationPipeline().compile(query, model, dialect).sql


class TestDialects:
    @pytest.mark.parametrize(
        ("dialect", "last", "first"),
        [
            (
                "postgres",
                'ORDER BY "k" DESC, "v" DESC) FILTER',
                'ORDER BY "k" ASC, "v" ASC) FILTER',
            ),
            (
                "duckdb",
                'arg_max("v", struct_pack(k := "k", v := "v"))',
                'arg_min("v", struct_pack(k := "k", v := "v"))',
            ),
            (
                "snowflake",
                'MAX_BY("v", IFF("k" IS NULL OR "v" IS NULL, NULL, ARRAY_CONSTRUCT("k", "v")))',
                'MIN_BY("v", IFF(',
            ),
            ("databricks", "max_by(`v`, CASE WHEN", "min_by(`v`, CASE WHEN"),
            ("clickhouse", 'argMaxIf("v", ("k", "v"),', 'argMinIf("v", ("k", "v"),'),
            (
                "bigquery",
                "IGNORE NULLS ORDER BY `k` DESC, `v` DESC LIMIT 1)[SAFE_OFFSET(0)]",
                "IGNORE NULLS ORDER BY `k` ASC, `v` ASC LIMIT 1)[SAFE_OFFSET(0)]",
            ),
        ],
    )
    def test_rendering(self, dialect: str, last: str, first: str) -> None:
        compiler = DialectRegistry.get(dialect)
        assert last in compiler.compile_expr(_call("LAST"))
        assert first in compiler.compile_expr(_call("FIRST"))

    def test_without_a_key_refused(self) -> None:
        call = FunctionCall(name="LAST", args=[ColumnRef(name="v")])
        with pytest.raises(ValueError, match="key to order by"):
            DialectRegistry.get("postgres").compile_expr(call)


class TestSchema:
    def _errors(self, measure: dict[str, Any]) -> list[str]:
        doc = {
            "version": 1.0,
            "dataObjects": {
                "O": {
                    "code": "O",
                    "database": "d",
                    "schema": "s",
                    "columns": {"C": {"code": "C", "abstractType": "float"}},
                }
            },
            "measures": {"M": {"columns": [{"dataObject": "O", "column": "C"}], **measure}},
        }
        return [e.message for e in jsonschema.Draft7Validator(_SCHEMA).iter_errors(doc)]

    def test_valid(self) -> None:
        key = {"column": {"dataObject": "O", "column": "C"}}
        assert self._errors({"aggregation": "last", "withinGroup": key}) == []

    def test_key_required(self) -> None:
        assert self._errors({"aggregation": "first"})

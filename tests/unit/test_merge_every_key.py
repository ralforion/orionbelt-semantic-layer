"""Every top-level key of an extend fragment or inheriting child is merged or refused.

The merge used to read a fixed list of keys and drop the rest without a word.
A fragment's ``filters:`` vanished, so a tenant filter added through
``extends`` failed open; its ``settings:`` vanished, so ``weekStart: sunday``
in a fragment still bucketed weeks from Monday. An inheriting child lost its
``settings``, ``rules``, ``examples`` and ``name`` the same way.
"""

from __future__ import annotations

import pytest

from orionbelt.models.query import QueryObject
from orionbelt.parser.merger import ExtendsMerger, MergeError
from orionbelt.service.model_store import ModelStore, ModelValidationError

BASE_YAML = """
version: 1.0
dataObjects:
  Orders:
    code: orders
    schema: main
    columns:
      Order ID: {code: id, abstractType: int, primaryKey: true}
      Region: {code: region, abstractType: string}
      Order Date: {code: order_date, abstractType: date}
      Amount: {code: amount, abstractType: float}
dimensions:
  Region: {dataObject: Orders, column: Region, resultType: string}
  Order Date: {dataObject: Orders, column: Order Date, resultType: date}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
    resultType: float
"""


def _base() -> dict[str, object]:
    return {"version": 1.0, "dataObjects": {"Orders": {"code": "orders", "columns": {}}}}


def _merge(*fragments: str) -> tuple[dict[str, object], list[str]]:
    return ExtendsMerger().merge_from_strings(_base(), extend_yamls=list(fragments))


def _compile(query: dict[str, object], extends: list[str]) -> str:
    store = ModelStore()
    load = store.load_model(BASE_YAML, extends_yaml=extends)
    return store.compile_query(load.model_id, QueryObject(**query), "duckdb").sql


class TestExtendsMergesEveryKey:
    def test_filters_accumulate(self) -> None:
        merged, _ = _merge(
            "filters: [{dataObject: Orders, column: Region, operator: equals, value: North}]",
            "filters: [{dataObject: Orders, column: Amount, operator: gt, value: 0}]",
        )
        assert [f["column"] for f in merged["filters"]] == ["Region", "Amount"]

    def test_fragment_filter_reaches_the_sql(self) -> None:
        sql = _compile(
            {"select": {"measures": ["Revenue"]}},
            ["filters: [{dataObject: Orders, column: Region, operator: equals, value: North}]"],
        )
        assert 'WHERE "Orders"."region" = \'North\'' in sql

    def test_settings_merge_and_a_changed_value_warns(self) -> None:
        merged, warnings = _merge("settings: {weekStart: sunday}", "settings: {weekStart: monday}")
        assert merged["settings"] == {"weekStart": "monday"}
        assert any("weekStart" in w for w in warnings)

    def test_fragment_week_start_reaches_the_sql(self) -> None:
        query = {"select": {"dimensions": ["Order Date:week"], "measures": ["Revenue"]}}
        monday = _compile(query, [])
        sunday = _compile(query, ["settings: {weekStart: sunday}"])
        assert sunday != monday

    def test_rules_merge_by_name(self) -> None:
        merged, warnings = _merge("rules: {Big: {a: 1}}", "rules: {Big: {a: 2}, Small: {a: 3}}")
        assert merged["rules"] == {"Big": {"a": 2}, "Small": {"a": 3}}
        assert any("Big" in w for w in warnings)

    def test_examples_merge_by_name(self) -> None:
        merged, warnings = _merge(
            "examples: [{name: top, query: {}}, {name: one, query: {}}]",
            "examples: [{name: top, description: newer, query: {}}]",
        )
        assert [(e["name"], e.get("description")) for e in merged["examples"]] == [
            ("top", "newer"),
            ("one", None),
        ]
        assert any("top" in w for w in warnings)

    def test_count_knobs_merge(self) -> None:
        merged, warnings = _merge("exposeCounts: false", "countLabelPattern: 'N {object}'")
        assert merged["exposeCounts"] is False
        assert merged["countLabelPattern"] == "N {object}"
        assert warnings == []

    @pytest.mark.parametrize("name", ["[totals]", "{a: 1}", "7"])
    def test_an_example_name_that_is_not_a_string_is_left_to_the_resolver(self, name: str) -> None:
        """Only a string names an example. Indexing a list crashed the merge
        with a TypeError, which the API answered with a 500."""
        merged, warnings = _merge(
            "examples: [{name: top, query: {}}]",
            f"examples: [{{name: {name}, query: {{}}}}, {{name: top, query: {{}}}}]",
        )
        assert len(merged["examples"]) == 2
        assert warnings == ["Example 'top' overridden by 'extends[1]'"]

    def test_an_invalid_example_name_is_a_validation_error(self) -> None:
        store = ModelStore()
        with pytest.raises(ModelValidationError) as excinfo:
            store.load_model(
                BASE_YAML,
                extends_yaml=["examples: [{name: [totals], query: {select: {measures: []}}}]"],
            )
        assert [e.code for e in excinfo.value.errors] == ["EXAMPLES_PARSE_ERROR"]

    def test_a_malformed_target_examples_section_is_left_as_authored(self) -> None:
        raw = {**_base(), "examples": {"top": {}}}
        merged, _ = ExtendsMerger().merge_from_strings(
            raw, extend_yamls=["examples: [{name: one, query: {}}]"]
        )
        assert merged["examples"] == {"top": {}}

    @pytest.mark.parametrize(
        ("fragment", "code"),
        [
            ("name: other", "EXTENDS_CONTAINS_MODEL_NAME"),
            ("inherits: parent", "EXTENDS_CONTAINS_INHERITS"),
            ("extends: [x.yaml]", "EXTENDS_NESTED_INLINE"),
            ("filtres: []", "UNKNOWN_PROPERTY"),
            ("settings: [weekStart]", "INVALID_FRAGMENT_SECTION"),
            ("filters: {Region: North}", "INVALID_FRAGMENT_SECTION"),
        ],
    )
    def test_a_key_it_cannot_merge_is_refused(self, fragment: str, code: str) -> None:
        with pytest.raises(MergeError) as excinfo:
            _merge(fragment)
        assert excinfo.value.code == code


class TestInheritsMergesEveryKey:
    def test_child_keys_are_kept(self) -> None:
        parent = {
            "version": 1.0,
            "name": "parent",
            "dataObjects": {"Orders": {"code": "orders", "columns": {}}},
            "settings": {"weekStart": "monday", "queryTimezone": "UTC"},
            "rules": {"Big": {"a": 1}},
        }
        child = {
            "name": "child",
            "settings": {"weekStart": "sunday"},
            "rules": {"Small": {"a": 2}},
            "examples": [{"name": "top", "query": {}}],
            "exposeCounts": False,
        }
        merged, warnings = ExtendsMerger().merge_from_strings(child, inherits_raw=parent)
        assert merged["name"] == "child"
        assert merged["settings"] == {"weekStart": "sunday", "queryTimezone": "UTC"}
        assert merged["rules"] == {"Big": {"a": 1}, "Small": {"a": 2}}
        assert merged["examples"] == [{"name": "top", "query": {}}]
        assert merged["exposeCounts"] is False
        assert any("weekStart" in w for w in warnings)

    def test_unknown_child_key_is_refused(self) -> None:
        parent = {"version": 1.0, "dataObjects": {"Orders": {"code": "orders", "columns": {}}}}
        with pytest.raises(MergeError) as excinfo:
            ExtendsMerger().merge_from_strings({"filtres": []}, inherits_raw=parent)
        assert excinfo.value.code == "UNKNOWN_PROPERTY"

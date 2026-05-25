"""JSON-schema-side validation of the shipped ``query-schema.json``.

The Pydantic ``QueryFilter`` validator already enforces these constraints at
runtime, but the JSON schema is what external tooling consumes. They must
agree — otherwise generators / linters accept payloads the API rejects.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_PATH = Path(__file__).parent.parent.parent / "schema" / "query-schema.json"


@pytest.fixture(scope="module")
def validator() -> jsonschema.Draft202012Validator:
    with SCHEMA_PATH.open() as f:
        schema = json.load(f)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _wrap(filter_obj: dict) -> dict:
    return {"select": {"dimensions": ["x"]}, "where": [filter_obj]}


class TestExistsFilterSchema:
    def test_exists_with_subquery_is_valid(
        self, validator: jsonschema.Draft202012Validator
    ) -> None:
        payload = _wrap({"field": "a", "op": "exists", "subquery": {"dataObject": "Y"}})
        assert list(validator.iter_errors(payload)) == []

    def test_nonexists_with_subquery_is_valid(
        self, validator: jsonschema.Draft202012Validator
    ) -> None:
        payload = _wrap({"field": "a", "op": "nonexists", "subquery": {"dataObject": "Y"}})
        assert list(validator.iter_errors(payload)) == []

    def test_exists_without_subquery_rejected(
        self, validator: jsonschema.Draft202012Validator
    ) -> None:
        payload = _wrap({"field": "a", "op": "exists"})
        assert list(validator.iter_errors(payload)), "exists without subquery must fail"

    def test_nonexists_without_subquery_rejected(
        self, validator: jsonschema.Draft202012Validator
    ) -> None:
        payload = _wrap({"field": "a", "op": "nonexists"})
        assert list(validator.iter_errors(payload)), "nonexists without subquery must fail"

    def test_exists_with_value_rejected(self, validator: jsonschema.Draft202012Validator) -> None:
        payload = _wrap(
            {
                "field": "a",
                "op": "exists",
                "subquery": {"dataObject": "Y"},
                "value": 1,
            }
        )
        assert list(validator.iter_errors(payload)), "exists must not also accept value"

    def test_equals_with_subquery_rejected(
        self, validator: jsonschema.Draft202012Validator
    ) -> None:
        payload = _wrap({"field": "a", "op": "equals", "value": 1, "subquery": {"dataObject": "Y"}})
        assert list(validator.iter_errors(payload)), "equals must not accept subquery"

    def test_equals_without_subquery_is_valid(
        self, validator: jsonschema.Draft202012Validator
    ) -> None:
        payload = _wrap({"field": "a", "op": "equals", "value": 1})
        assert list(validator.iter_errors(payload)) == []

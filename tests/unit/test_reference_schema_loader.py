"""Tests for the reference JSON Schema loader.

Regression guard for the deployment bug where the schema files were not
bundled into the wheel and the loader resolved them via a source-only
``parents[4]`` path — so ``/v1/reference/schemas/{obml,query}`` returned 500
on PyPI / Docker installs while passing in editable dev.
"""

from __future__ import annotations

import json

import pytest

from orionbelt.api.routers import reference


@pytest.mark.parametrize("filename", ["obml-schema.json", "query-schema.json"])
def test_read_schema_text_resolves_each_file(filename: str) -> None:
    text = reference._read_schema_text(filename)
    assert text is not None, f"{filename} should resolve in any install layout"
    parsed = json.loads(text)
    assert parsed.get("$schema") or parsed.get("type")


def test_read_schema_text_unknown_file_returns_none() -> None:
    assert reference._read_schema_text("does-not-exist.json") is None


@pytest.mark.parametrize("name", ["obml", "query"])
def test_load_schema_returns_parsed_document(name: str) -> None:
    loaded = reference._load_schema(name)
    assert isinstance(loaded, dict)
    assert loaded.get("$schema") or loaded.get("type")

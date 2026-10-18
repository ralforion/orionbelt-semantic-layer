"""What ``validate_osi`` checks, with and without its JSON Schema layer.

Mirrors apache/ossie#429 and #524: a skipped schema check is reported as
skipped rather than passed, a non-object document is an error rather than a
crash, and a name repeated three times is one problem.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from osi_orionbelt.cli import _report_validation
from osi_orionbelt.validation import _OSI_SCHEMA_PATH, validate_osi


@pytest.fixture(params=["available", "missing_file", "missing_package"])
def schema_mode(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def schema_path(schema_mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if schema_mode == "available":
        pytest.importorskip("jsonschema")
    if schema_mode == "missing_file":
        return tmp_path / "missing-schema.json"
    if schema_mode == "missing_package":
        monkeypatch.setitem(sys.modules, "jsonschema", None)
    return _OSI_SCHEMA_PATH


def _doc(**model: Any) -> dict[str, Any]:
    return {"version": "0.2.0.dev0", "name": "m", **model}


_DATASET = {"name": "t", "source": "a.b.c"}


def test_valid_document_reports_whether_the_schema_ran(schema_path: Path, schema_mode: str) -> None:
    result = validate_osi(_doc(datasets=[_DATASET]), schema_path=schema_path)
    assert result.valid
    assert result.schema_validation_performed == (schema_mode == "available")
    status = "✓ valid" if schema_mode == "available" else "skipped"
    assert result.summary_lines()[0] == f"  JSON Schema: {status}"
    if schema_mode != "available":
        assert result.semantic_warnings


@pytest.mark.parametrize(
    "model",
    [
        {"datasets": "not a list"},
        {},
        {"datasets": [{"name": "t"}]},
        {"datasets": ["not an object"]},
    ],
    ids=["datasets_type", "datasets_missing", "source_missing", "dataset_type"],
)
def test_structural_problems_need_the_schema(
    schema_path: Path, schema_mode: str, model: dict[str, Any]
) -> None:
    result = validate_osi(_doc(**model), schema_path=schema_path)
    assert not result.semantic_errors
    if schema_mode == "available":
        assert not result.valid
        assert result.summary_lines()[0] == f"  JSON Schema: {len(result.schema_errors)} error(s)"
    else:
        # Valid only in the sense that the checks that ran found nothing.
        assert result.valid
        assert not result.schema_validation_performed
        assert result.summary_lines()[0] == "  JSON Schema: skipped"


@pytest.mark.parametrize("document", [None, [], "not a mapping", 42])
def test_non_object_document_is_an_error(schema_path: Path, document: Any) -> None:
    result = validate_osi(document, schema_path=schema_path)
    assert not result.valid
    assert any("[INVALID_DOCUMENT]" in e for e in result.semantic_errors)


def test_wrapper_is_reported_either_way(schema_path: Path, schema_mode: str) -> None:
    document = {"version": "0.2.0.dev0", "semantic_model": [_doc(datasets=[_DATASET])]}
    result = validate_osi(document, schema_path=schema_path)
    if schema_mode == "available":
        assert any("semantic_model" in e for e in result.schema_errors)
    else:
        assert any("[LEGACY_WRAPPER]" in w for w in result.semantic_warnings)


@pytest.mark.parametrize("code", ["DUPLICATE_DATASET", "UNKNOWN_DATASET_REF"])
def test_semantic_checks_run_without_the_schema(schema_path: Path, code: str) -> None:
    document = _doc(datasets=[_DATASET])
    if code == "DUPLICATE_DATASET":
        document["datasets"].append({"name": "t", "source": "a.b.d"})
    else:
        document["relationships"] = [
            {"name": "r", "from": "t", "to": "x", "from_columns": ["a"], "to_columns": ["b"]}
        ]
    result = validate_osi(document, schema_path=schema_path)
    assert any(code in e for e in result.semantic_errors)


_EXPR = {"dialects": [{"dialect": "ANSI_SQL", "expression": "x"}]}


@pytest.mark.parametrize(
    ("model", "code"),
    [
        ({"datasets": [_DATASET] * 3}, "DUPLICATE_DATASET"),
        (
            {"datasets": [{**_DATASET, "fields": [{"name": "f", "expression": _EXPR}] * 3}]},
            "DUPLICATE_FIELD",
        ),
        (
            {"datasets": [_DATASET], "metrics": [{"name": "m", "expression": _EXPR}] * 3},
            "DUPLICATE_METRIC",
        ),
        (
            {
                "datasets": [_DATASET],
                "relationships": [
                    {
                        "name": "r",
                        "from": "t",
                        "to": "t",
                        "from_columns": ["a"],
                        "to_columns": ["a"],
                    }
                ]
                * 3,
            },
            "DUPLICATE_RELATIONSHIP",
        ),
    ],
)
def test_a_name_repeated_three_times_is_reported_once(model: dict[str, Any], code: str) -> None:
    result = validate_osi(_doc(**model))
    assert sum(code in e for e in result.semantic_errors) == 1


def test_duplicates_are_reported_in_first_appearance_order() -> None:
    datasets = [{"name": n, "source": "a.b.c"} for n in ("b", "a", "b", "a")]
    result = validate_osi(_doc(datasets=datasets))
    assert [e.split("'")[1] for e in result.semantic_errors] == ["b", "a"]


def test_cli_does_not_call_a_skipped_schema_check_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing-schema.json"
    errors = _report_validation(
        "OSI output", _doc(datasets=[_DATASET]), lambda d: validate_osi(d, schema_path=missing)
    )
    assert errors is False
    err = capsys.readouterr().err
    assert "JSON Schema skipped" in err
    assert "OSI output is valid" not in err

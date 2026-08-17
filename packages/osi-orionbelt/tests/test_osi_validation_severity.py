"""The converter's own validation has to read severity, not just count results.

``SemanticValidator`` returns advisories alongside errors — a call to a function
outside the portable catalog, a query time zone that cannot reach an undeclared
column. The converter flattened every result into ``semantic_errors``, so a
model the semantic layer itself accepts came back invalid through this path.
That is the kind of drift a package with its own validation entry point invites,
and only a test on this side catches it.
"""

from __future__ import annotations

from typing import Any

import yaml

from osi_orionbelt.validation import validate_obml

_MODEL = """\
version: 1.0
{settings}dataObjects:
  Orders:
    code: o
    columns:
      Zip: {{code: zip, abstractType: string}}
      Zip 5:
        abstractType: string
        expression: "{expression}"

dimensions:
  Zip 5: {{dataObject: Orders, column: Zip 5, resultType: string}}
"""


def _validate(expression: str, settings: str = "") -> Any:
    return validate_obml(yaml.safe_load(_MODEL.format(settings=settings, expression=expression)))


def test_a_catalog_expression_is_clean() -> None:
    result = _validate("substring({Zip}, 1, 5)")
    assert result.valid
    assert result.semantic_errors == []
    assert result.semantic_warnings == []


def test_a_non_portable_call_is_a_warning_not_an_error() -> None:
    """Permissive is the default, so the model stays valid and says why."""
    result = _validate("regexp_extract({Zip}, '[0-9]')")
    assert result.valid
    assert result.semantic_errors == []
    assert len(result.semantic_warnings) == 1
    assert "NON_PORTABLE_FUNCTION" in result.semantic_warnings[0]
    assert "regexp_extract" in result.semantic_warnings[0]


def test_portable_mode_makes_it_an_error() -> None:
    result = _validate(
        "regexp_extract({Zip}, '[0-9]')",
        settings="settings:\n  expressionMode: portable\n",
    )
    assert not result.valid
    assert len(result.semantic_errors) == 1
    assert "NON_PORTABLE_FUNCTION" in result.semantic_errors[0]


def test_a_real_error_is_still_an_error() -> None:
    """Severity routing must not turn genuine failures into advisories."""
    result = _validate("substring({No Such Column}, 1, 5)")
    assert not result.valid
    assert any("UNKNOWN_COLUMN_IN_EXPRESSION" in e for e in result.semantic_errors)

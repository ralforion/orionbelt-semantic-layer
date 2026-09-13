"""The commerce demo carries synthetic business rules, and every one compiles."""

from __future__ import annotations

from pathlib import Path

import pytest

from orionbelt.compiler.rules import MATCHES, VIOLATIONS, RuleCompiler
from orionbelt.models.semantic import SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_MODEL = Path(__file__).resolve().parents[2] / "examples" / "orionbelt_1_commerce.yaml"


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    raw, sm = TrackedLoader().load(_MODEL)
    model, result = ReferenceResolver().resolve(raw, sm)
    assert result.errors == [], result.errors
    return model


def test_demo_declares_the_showcase_rules(model: SemanticModel) -> None:
    assert set(model.rules) == {
        "Electronics Sale",
        "High Return Rate",
        "High Value Client",
        "Non-Negative Margin",
        "Low Stock Product",
        "Healthy Category",
    }
    compiler = RuleCompiler(model)
    assert compiler.level(model.rules["Electronics Sale"]) == "row"
    assert compiler.findings(model.rules["Non-Negative Margin"]) == VIOLATIONS
    assert compiler.findings(model.rules["High Value Client"]) == MATCHES


@pytest.mark.parametrize(
    "name",
    [
        "Electronics Sale",
        "High Return Rate",
        "High Value Client",
        "Non-Negative Margin",
        "Low Stock Product",
        "Healthy Category",
    ],
)
def test_every_demo_rule_compiles_on_duckdb(model: SemanticModel, name: str) -> None:
    plan, result = RuleCompiler(model).compile(model.rules[name], "duckdb")
    assert "SELECT" in result.sql
    if plan.level == "aggregate":
        assert "HAVING" in result.sql

"""Business rules: the OBML ``rules`` block, its validation, and the rule compiler.

A rule reads dimensions (row-level, a WHERE predicate) or measures and
metrics (aggregate, evaluated at a declared grain, a HAVING predicate). It
never carries SQL. The compiler turns a rule into an ordinary QueryObject
whose rows are the rule's findings: members for classification and
eligibility, violations for validation and constraint.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orionbelt.compiler.rules import MATCHES, VIOLATIONS, RuleCompiler
from orionbelt.models.errors import SemanticError
from orionbelt.models.query import QueryFilter, QueryFilterGroup
from orionbelt.models.rules import condition_fields, condition_rule_refs
from orionbelt.models.semantic import Rule, RuleCondition, RuleType, SemanticModel
from orionbelt.parser.loader import TrackedLoader
from orionbelt.parser.resolver import ReferenceResolver

_BASE = """\
version: 1.0
dataObjects:
  Orders:
    code: ORDERS
    database: EDW
    schema: SALES
    columns:
      ID: {code: ID, abstractType: string}
      Category: {code: CATEGORY, abstractType: string}
      Amount: {code: AMOUNT, abstractType: float}
      Returned: {code: RETURNED, abstractType: float}
dimensions:
  Order ID: {dataObject: Orders, column: ID}
  Category: {dataObject: Orders, column: Category}
measures:
  Revenue:
    columns: [{dataObject: Orders, column: Amount}]
    aggregation: sum
  Returned Amount:
    columns: [{dataObject: Orders, column: Returned}]
    aggregation: sum
metrics:
  Return Rate:
    expression: "{[Returned Amount]} / NULLIF({[Revenue]}, 0)"
"""

_RULES = """\
rules:
  Electronics Sale:
    type: classification
    description: Rows in the Electronics category
    condition: {field: Category, op: "=", value: Electronics}
  High Return Rate:
    type: classification
    grain: [Category]
    condition: {field: Return Rate, op: ">", value: 0.1}
  Healthy Category:
    type: validation
    severity: error
    grain: [Category]
    condition:
      all:
        - {field: Revenue, op: ">", value: 0}
        - {not: {rule: High Return Rate}}
  Not Electronics:
    type: eligibility
    condition: {not: {rule: Electronics Sale}}
"""


def _resolve(yaml_text: str) -> tuple[SemanticModel, list[SemanticError]]:
    raw, sm = TrackedLoader().load_string(yaml_text)
    model, result = ReferenceResolver().resolve(raw, sm)
    return model, result.errors


def _codes(errors: list[SemanticError]) -> list[str]:
    return [e.code for e in errors]


# ───────────────────────────── the model ─────────────────────────────


class TestRuleCondition:
    def test_comparison(self) -> None:
        c = RuleCondition(field="Category", op="=", value="Electronics")
        assert c.kind == "comparison"

    def test_composition_and_reference(self) -> None:
        c = RuleCondition.model_validate(
            {"all": [{"field": "Revenue", "op": ">", "value": 0}, {"not": {"rule": "X"}}]}
        )
        assert c.kind == "all"
        assert condition_fields(c) == ["Revenue"]
        assert condition_rule_refs(c) == ["X"]

    @pytest.mark.parametrize(
        "payload, fragment",
        [
            ({}, "exactly one of"),
            ({"field": "A", "op": "=", "rule": "B"}, "exactly one of"),
            ({"field": "A"}, "needs an 'op'"),
            ({"field": "A", "op": "same_as"}, "unknown operator"),
            ({"field": "A", "op": "exists"}, "not allowed in a rule condition"),
            ({"rule": "B", "value": 1}, "belong to a comparison"),
            ({"all": []}, "at least one condition"),
        ],
    )
    def test_rejected_shapes(self, payload: dict, fragment: str) -> None:
        with pytest.raises(ValidationError) as exc:
            RuleCondition.model_validate(payload)
        assert fragment in str(exc.value)


# ───────────────────────────── the parser ────────────────────────────


class TestLoading:
    def test_rules_load_with_levels(self) -> None:
        model, errors = _resolve(_BASE + _RULES)
        assert errors == []
        assert set(model.rules) == {
            "Electronics Sale",
            "High Return Rate",
            "Healthy Category",
            "Not Electronics",
        }
        rule = model.rules["Healthy Category"]
        assert rule.type is RuleType.VALIDATION
        assert rule.severity is not None and rule.severity.value == "error"
        assert rule.grain == ["Category"]
        compiler = RuleCompiler(model)
        assert compiler.level(model.rules["Electronics Sale"]) == "row"
        assert compiler.level(model.rules["Not Electronics"]) == "row"
        assert compiler.level(model.rules["High Return Rate"]) == "aggregate"
        assert compiler.level(model.rules["Healthy Category"]) == "aggregate"

    def test_model_without_rules_is_unchanged(self) -> None:
        model, errors = _resolve(_BASE)
        assert errors == [] and model.rules == {}

    def test_rule_metadata_blocks_load(self) -> None:
        model, errors = _resolve(
            _BASE + "ontology:\n  prefixes:\n    corp: 'https://x.example/'\n"
            "rules:\n  R:\n    condition: {field: Category, op: '=', value: X}\n"
            "    owner: finance\n    synonyms: [x rule]\n"
            "    customExtensions: [{vendor: gov, data: '{}'}]\n"
            "    externalConceptMappings: [{concept: 'corp:XRule', relation: exact}]\n"
        )
        assert errors == []
        rule = model.rules["R"]
        assert rule.owner == "finance" and rule.synonyms == ["x rule"]
        assert rule.custom_extensions[0].vendor == "gov"
        assert rule.external_concept_mappings[0].expanded_iri == "https://x.example/XRule"

    def test_synthesized_count_is_a_valid_field(self) -> None:
        model, errors = _resolve(
            _BASE + "rules:\n  Busy:\n    grain: [Category]\n"
            "    condition: {field: Orders Count, op: '>', value: 100}\n"
        )
        assert errors == []
        assert RuleCompiler(model).plan(model.rules["Busy"]).measures == ["Orders Count"]


class TestValidation:
    @pytest.mark.parametrize(
        "block, code, path",
        [
            (
                "  R:\n    condition: {field: Nope, op: '=', value: 1}\n",
                "UNKNOWN_RULE_FIELD",
                "rules.R.condition",
            ),
            ("  R:\n    condition: {rule: Missing}\n", "UNKNOWN_RULE", "rules.R.condition"),
            ("  R:\n    condition: {rule: R}\n", "UNKNOWN_RULE", "rules.R.condition"),
            (
                "  R:\n    grain: [Nope]\n    condition: {field: Revenue, op: '>', value: 0}\n",
                "UNKNOWN_RULE_GRAIN",
                "rules.R.grain",
            ),
            (
                "  R:\n    condition: {field: Revenue, op: '>', value: 0}\n",
                "RULE_GRAIN_REQUIRED",
                "rules.R",
            ),
            (
                "  R:\n    grain: [Category]\n"
                "    condition: {field: Category, op: '=', value: X}\n",
                "RULE_GRAIN_NOT_ALLOWED",
                "rules.R.grain",
            ),
            (
                "  R:\n    severity: error\n    condition: {field: Category, op: '=', value: X}\n",
                "INVALID_RULE_SEVERITY",
                "rules.R.severity",
            ),
            (
                "  R:\n    type: sometimes\n    condition: {field: Category, op: '=', value: X}\n",
                "RULE_PARSE_ERROR",
                "rules.R.type",
            ),
            (
                "  R:\n    condition: {field: Category, op: 'same'}\n",
                "INVALID_RULE_CONDITION",
                "rules.R.condition",
            ),
            (
                "  R:\n    condition: {field: Category, op: '=', value: X}\n    grian: []\n",
                "UNKNOWN_PROPERTY",
                "rules.R",
            ),
            (
                "  R:\n    condition: {all: [{feild: Category, op: '=', value: X}]}\n",
                "UNKNOWN_PROPERTY",
                "rules.R.condition.all[0]",
            ),
            ("  R: just a string\n", "RULE_PARSE_ERROR", "rules.R"),
        ],
    )
    def test_each_error(self, block: str, code: str, path: str) -> None:
        _, errors = _resolve(_BASE + "rules:\n" + block)
        assert code in _codes(errors), errors
        err = next(e for e in errors if e.code == code)
        assert err.path == path
        assert err.span is not None or code == "UNKNOWN_PROPERTY"

    def test_aggregate_rule_may_compare_only_grain_dimensions(self) -> None:
        """A non-grain dimension in HAVING is neither grouped nor aggregated."""
        _, errors = _resolve(
            _BASE + "rules:\n  R:\n    grain: [Category]\n"
            "    condition:\n      all:\n"
            "        - {field: Revenue, op: '>', value: 0}\n"
            "        - {field: Order ID, op: '=', value: X}\n"
        )
        assert _codes(errors) == ["RULE_DIMENSION_OUTSIDE_GRAIN"]
        assert "Order ID" in errors[0].message
        # A grain dimension is fine: it is a grouped column.
        _, errors = _resolve(
            _BASE + "rules:\n  R:\n    grain: [Category]\n"
            "    condition:\n      all:\n"
            "        - {field: Revenue, op: '>', value: 0}\n"
            "        - {field: Category, op: '!=', value: X}\n"
        )
        assert errors == []

    def test_rules_block_must_be_a_mapping(self) -> None:
        _, errors = _resolve(_BASE + "rules: []\n")
        assert _codes(errors) == ["RULE_PARSE_ERROR"]

    def test_reference_across_levels_is_a_mismatch(self) -> None:
        _, errors = _resolve(
            _BASE + "rules:\n"
            "  Row: {condition: {field: Category, op: '=', value: X}}\n"
            "  Agg:\n    grain: [Category]\n"
            "    condition: {all: [{field: Revenue, op: '>', value: 0}, {rule: Row}]}\n"
        )
        assert "RULE_REFERENCE_MISMATCH" in _codes(errors)

    def test_reference_across_grains_is_a_mismatch(self) -> None:
        _, errors = _resolve(
            _BASE + "rules:\n"
            "  A:\n    grain: [Category]\n    condition: {field: Revenue, op: '>', value: 0}\n"
            "  B:\n    grain: [Order ID]\n"
            "    condition: {all: [{field: Revenue, op: '>', value: 0}, {rule: A}]}\n"
        )
        assert "RULE_REFERENCE_MISMATCH" in _codes(errors)

    def test_cycle_is_reported_once(self) -> None:
        _, errors = _resolve(
            _BASE + "rules:\n"
            "  A: {condition: {rule: B}}\n"
            "  B: {condition: {rule: C}}\n"
            "  C: {condition: {rule: A}}\n"
        )
        cyclic = [e for e in errors if e.code == "CYCLIC_RULE_REFERENCE"]
        assert len(cyclic) == 1
        assert "A -> B -> C -> A" in cyclic[0].message

    def test_bad_rule_does_not_hide_the_good_ones(self) -> None:
        model, errors = _resolve(
            _BASE + "rules:\n"
            "  Bad: {condition: {field: Nope, op: '=', value: 1}}\n"
            "  Good: {condition: {field: Category, op: '=', value: X}}\n"
        )
        assert _codes(errors) == ["UNKNOWN_RULE_FIELD"]
        assert set(model.rules) == {"Bad", "Good"}


# ───────────────────────────── the compiler ──────────────────────────


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    model, errors = _resolve(_BASE + _RULES)
    assert errors == []
    return model


class TestCompiler:
    def test_row_level_rule_is_a_where_query(self, model: SemanticModel) -> None:
        plan = RuleCompiler(model).plan(model.rules["Electronics Sale"])
        assert plan.level == "row" and plan.findings == MATCHES
        assert plan.query.select.dimensions == ["Category"]
        assert plan.query.select.measures == []
        [where] = plan.query.where
        assert isinstance(where, QueryFilter)
        assert (where.field, where.op.value, where.value) == ("Category", "=", "Electronics")
        assert plan.query.having == []

    def test_aggregate_rule_is_a_having_query_at_its_grain(self, model: SemanticModel) -> None:
        plan = RuleCompiler(model).plan(model.rules["High Return Rate"])
        assert plan.level == "aggregate" and plan.findings == MATCHES
        assert plan.query.select.dimensions == ["Category"]
        assert plan.query.select.measures == ["Return Rate"]
        [having] = plan.query.having
        assert isinstance(having, QueryFilter) and having.field == "Return Rate"

    def test_validation_rule_reports_violations(self, model: SemanticModel) -> None:
        plan = RuleCompiler(model).plan(model.rules["Healthy Category"])
        assert plan.findings == VIOLATIONS
        assert plan.measures == ["Revenue", "Return Rate"]
        assert plan.depends_on == ["High Return Rate"]
        [having] = plan.query.having
        # NOT (Revenue > 0 AND NOT (Return Rate > 0.1)), the reference inlined.
        assert isinstance(having, QueryFilterGroup) and having.negated
        [inner] = having.filters
        assert isinstance(inner, QueryFilterGroup) and inner.logic.value == "and"
        assert isinstance(inner.filters[1], QueryFilterGroup) and inner.filters[1].negated
        [ref] = inner.filters[1].filters
        assert isinstance(ref, QueryFilter) and ref.field == "Return Rate"

    def test_inlined_reference_in_a_row_level_rule(self, model: SemanticModel) -> None:
        plan = RuleCompiler(model).plan(model.rules["Not Electronics"])
        assert plan.query.select.dimensions == ["Category"]
        [where] = plan.query.where
        assert isinstance(where, QueryFilterGroup) and where.negated

    @pytest.mark.parametrize("name", ["Electronics Sale", "High Return Rate", "Healthy Category"])
    @pytest.mark.parametrize("dialect", ["postgres", "duckdb"])
    def test_every_rule_compiles_to_sql(
        self, model: SemanticModel, name: str, dialect: str
    ) -> None:
        plan, result = RuleCompiler(model).compile(model.rules[name], dialect)
        assert "SELECT" in result.sql
        if plan.level == "aggregate":
            assert "GROUP BY" in result.sql and "HAVING" in result.sql
        else:
            assert "WHERE" in result.sql and "HAVING" not in result.sql

    def test_violation_sql_negates_the_condition(self, model: SemanticModel) -> None:
        _, result = RuleCompiler(model).compile(model.rules["Healthy Category"], "postgres")
        assert "NOT (" in result.sql


def test_rule_model_constructs_with_python_names() -> None:
    rule = Rule(name="R", condition=RuleCondition(field="A", op="=", value=1), grain=["A"])
    assert rule.type is RuleType.CLASSIFICATION and rule.grain == ["A"]

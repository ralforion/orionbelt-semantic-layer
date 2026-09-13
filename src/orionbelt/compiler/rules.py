"""Rule compiler: a business rule to the query that reports its findings.

A rule is declarative; this turns it into an ordinary :class:`QueryObject`
so everything downstream (resolution, fan-out detection, CFL planning, the
dialects, the result cache) is reused unchanged:

* a **row-level** rule (dimensions only) selects the dimensions it reads,
  with the condition as WHERE;
* an **aggregate** rule selects its ``grain`` dimensions plus the measures
  and metrics it reads, with the condition as HAVING.

What the query reports depends on the rule's type. ``classification`` and
``eligibility`` rules describe members, so their query returns the rows or
groups the condition holds for. ``validation`` and ``constraint`` rules
state an invariant, so their query returns the *violations*: the condition
is negated. Rule references are inlined.
"""

from __future__ import annotations

from dataclasses import dataclass

from orionbelt.compiler.pipeline import CompilationPipeline, CompilationResult
from orionbelt.models.query import (
    FilterOperator,
    QueryFilter,
    QueryFilterGroup,
    QueryFilterItem,
    QueryObject,
    QuerySelect,
)
from orionbelt.models.rules import (
    AGGREGATE,
    condition_rule_refs,
    rule_level,
    transitive_fields,
)
from orionbelt.models.semantic import FilterLogic, Rule, RuleCondition, RuleType, SemanticModel

MATCHES = "matches"
VIOLATIONS = "violations"

_MEMBER_TYPES = frozenset({RuleType.CLASSIFICATION, RuleType.ELIGIBILITY})


@dataclass(frozen=True)
class RulePlan:
    """A rule turned into a query, with what the query means."""

    name: str
    level: str
    findings: str
    query: QueryObject
    dimensions: list[str]
    measures: list[str]
    depends_on: list[str]


class RuleCompiler:
    """Builds and compiles the query behind each rule of one model."""

    def __init__(self, model: SemanticModel) -> None:
        self.model = model
        self._aggregate_names = set(model.effective_measures) | set(model.metrics)

    def level(self, rule: Rule) -> str:
        return rule_level(rule, self.model.rules, self._aggregate_names)

    @staticmethod
    def findings(rule: Rule) -> str:
        return MATCHES if rule.type in _MEMBER_TYPES else VIOLATIONS

    def to_filter(self, condition: RuleCondition) -> QueryFilterItem:
        """The query filter a condition tree denotes, rule references inlined."""
        if condition.field is not None:
            # The model validator guarantees an operator on a comparison.
            return QueryFilter(
                field=condition.field, op=FilterOperator(str(condition.op)), value=condition.value
            )
        if condition.rule is not None:
            return self.to_filter(self.model.rules[condition.rule].condition)
        if condition.not_ is not None:
            return QueryFilterGroup(filters=[self.to_filter(condition.not_)], negated=True)
        logic = FilterLogic.AND if condition.all_ is not None else FilterLogic.OR
        children = condition.all_ if condition.all_ is not None else condition.any_
        return QueryFilterGroup(logic=logic, filters=[self.to_filter(c) for c in children or []])

    def plan(self, rule: Rule) -> RulePlan:
        """The query whose rows are the rule's findings."""
        level = self.level(rule)
        fields = transitive_fields(rule, self.model.rules)
        measures = [f for f in fields if f in self._aggregate_names]
        dimensions = [f for f in fields if f not in self._aggregate_names]
        predicate = self.to_filter(rule.condition)
        if self.findings(rule) == VIOLATIONS:
            predicate = QueryFilterGroup(filters=[predicate], negated=True)
        if level == AGGREGATE:
            query = QueryObject(
                select=QuerySelect(dimensions=list(rule.grain), measures=measures),
                having=[predicate],
            )
        else:
            query = QueryObject(select=QuerySelect(dimensions=dimensions), where=[predicate])
        return RulePlan(
            name=rule.name,
            level=level,
            findings=self.findings(rule),
            query=query,
            dimensions=list(rule.grain) if level == AGGREGATE else dimensions,
            measures=measures,
            depends_on=condition_rule_refs(rule.condition),
        )

    def compile(self, rule: Rule, dialect: str) -> tuple[RulePlan, CompilationResult]:
        """Plan the rule and compile its query for *dialect*."""
        plan = self.plan(rule)
        result = CompilationPipeline().compile(plan.query, self.model, dialect)
        return plan, result

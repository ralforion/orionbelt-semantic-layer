"""Walks over rule condition trees.

The resolver needs to know what a rule reads (to check the references and
decide whether it is row-level or aggregate) and which rules it depends on
(to refuse cycles); the compiler needs the same answers to build a query.
Both come from here so they cannot disagree.
"""

from __future__ import annotations

from collections.abc import Iterator

from orionbelt.models.semantic import Rule, RuleCondition

ROW_LEVEL = "row"
AGGREGATE = "aggregate"


def iter_conditions(condition: RuleCondition) -> Iterator[RuleCondition]:
    """Every node of a condition tree, parents before children."""
    yield condition
    for child in condition.all_ or ():
        yield from iter_conditions(child)
    for child in condition.any_ or ():
        yield from iter_conditions(child)
    if condition.not_ is not None:
        yield from iter_conditions(condition.not_)


def condition_fields(condition: RuleCondition) -> list[str]:
    """The ``field`` names a condition compares, in order, without repeats."""
    out: list[str] = []
    for node in iter_conditions(condition):
        if node.field is not None and node.field not in out:
            out.append(node.field)
    return out


def condition_rule_refs(condition: RuleCondition) -> list[str]:
    """The rules a condition references directly, in order, without repeats."""
    out: list[str] = []
    for node in iter_conditions(condition):
        if node.rule is not None and node.rule not in out:
            out.append(node.rule)
    return out


def transitive_fields(rule: Rule, rules: dict[str, Rule]) -> list[str]:
    """Fields the rule reads, following rule references (cycles are cut)."""
    out: list[str] = []
    seen: set[str] = set()
    stack = [rule]
    while stack:
        current = stack.pop()
        if current.name in seen:
            continue
        seen.add(current.name)
        for name in condition_fields(current.condition):
            if name not in out:
                out.append(name)
        for ref in condition_rule_refs(current.condition):
            if ref in rules:
                stack.append(rules[ref])
    return out


def rule_level(rule: Rule, rules: dict[str, Rule], aggregate_names: set[str]) -> str:
    """``row`` when the rule reads dimensions only, ``aggregate`` otherwise."""
    if any(name in aggregate_names for name in transitive_fields(rule, rules)):
        return AGGREGATE
    return ROW_LEVEL

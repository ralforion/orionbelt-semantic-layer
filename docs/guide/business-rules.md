---
description: "Declare business rules in OBML as conditions over dimensions, measures and metrics, without SQL, and compile them to the query that reports their findings."
---

# Business Rules

A business rule is a Boolean condition over the model's dimensions, measures and metrics, declared once in OBML and compiled by OBSL into the query that reports its findings. No SQL, no code: a rule is data, which is what lets it be listed, explained, exported to the RDF graph and tested.

## Two kinds of rule

| | Row-level | Aggregate |
|---|---|---|
| Reads | dimensions only | at least one measure or metric |
| `grain` | not allowed | required: the dimensions the rule is evaluated at |
| Compiles to | a `WHERE` predicate over the dimensions it reads | a query selecting the grain plus the measures it reads, with the condition as `HAVING` |
| Example | "this sale is an Electronics sale" | "this category returns more than a tenth of what it sells" |

The level is derived from the condition, not declared. Referencing another rule pulls in what that rule reads.

## What a rule is for

| `type` | Says | An evaluation returns |
|---|---|---|
| `classification` | members of a class | the rows or groups the condition holds for |
| `eligibility` | who qualifies | the rows or groups the condition holds for |
| `validation` | an invariant that should hold | the **violations**: where the condition does not hold |
| `constraint` | an invariant that must hold | the **violations** |

`validation` and `constraint` rules may carry a `severity` (`info`, `warning`, `error`). On the other two types a severity is rejected with `INVALID_RULE_SEVERITY`, since there is nothing to be severe about.

## Syntax

```yaml
rules:
  Electronics Sale:                         # row-level: dimensions only
    type: classification
    description: Sales rows in the Electronics category
    condition: {field: Product Category, op: "=", value: Electronics}

  High Return Rate:                         # aggregate: reads a metric
    type: classification
    grain: [Product Category]
    condition: {field: Return Rate, op: ">", value: 0.1}

  Healthy Category:
    type: validation
    severity: warning
    grain: [Product Category]
    condition:
      all:
        - {field: Total Sales, op: ">", value: 0}
        - {not: {rule: High Return Rate}}   # a reference: the other rule's condition is inlined
```

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `type` | enum | No | `classification` (default), `validation`, `constraint`, `eligibility` |
| `condition` | object | Yes | The condition tree, see below |
| `grain` | list | Aggregate rules | Dimensions the rule is evaluated at |
| `severity` | enum | No | `info`, `warning`, `error`; validation and constraint rules only |
| `description` | string | No | Business description |
| `owner` | string | No | Responsible team or person |
| `synonyms` | list | No | Alternative names (LLM hints) |
| `customExtensions` | list | No | Vendor-keyed metadata |
| `externalConceptMappings` | list | No | Links to concepts in an external ontology, see [External Concept Mappings](concept-mappings.md) |

### The condition tree

Every node is exactly one of:

| Node | Shape | Meaning |
|------|-------|---------|
| comparison | `{field, op, value}` | the same shape as a query filter: `field` names a dimension, measure or metric; `op` is any query filter operator except `exists` / `nonexists`; `value` is a scalar, a list, or a relative-date object |
| `all` | `{all: [node, ...]}` | every child holds |
| `any` | `{any: [node, ...]}` | at least one child holds |
| `not` | `{not: node}` | the child does not hold |
| `rule` | `{rule: Name}` | another rule's condition, inlined |

A referenced rule must be of the same level and, for aggregate rules, declare the same grain; anything else is `RULE_REFERENCE_MISMATCH`. References form a DAG: a cycle is `CYCLIC_RULE_REFERENCE`.

### Validation

| Error code | Cause |
|------------|-------|
| `RULE_PARSE_ERROR` | `rules` is not a mapping, a rule is not a mapping, or a property has the wrong type or an unknown enum value |
| `INVALID_RULE_CONDITION` | a condition node is not exactly one form, lacks `op`, uses an unknown or disallowed operator, or has an empty `all` / `any` |
| `UNKNOWN_RULE_FIELD` | a comparison names something that is not a dimension, measure or metric |
| `UNKNOWN_RULE` | a reference names a rule that does not exist, or the rule itself |
| `UNKNOWN_RULE_GRAIN` | `grain` names an unknown dimension |
| `RULE_GRAIN_REQUIRED` | an aggregate rule has no `grain` |
| `RULE_GRAIN_NOT_ALLOWED` | a row-level rule has a `grain` |
| `RULE_REFERENCE_MISMATCH` | referenced rule has a different level or grain |
| `CYCLIC_RULE_REFERENCE` | rules reference each other in a cycle |
| `INVALID_RULE_SEVERITY` | `severity` on a classification or eligibility rule |

All carry the YAML source span. A rule with a problem is reported and dropped; the others still load, so every problem surfaces at once.

## What a rule compiles to

The compiler turns a rule into an ordinary query, so resolution, fan-out detection, multi-fact planning, every dialect and the result cache are reused unchanged:

```yaml
# High Return Rate -> the groups the condition holds for
select: {dimensions: [Product Category], measures: [Return Rate]}
having:
  - {field: Return Rate, op: ">", value: 0.1}

# Healthy Category (validation) -> the violations: the condition negated
select: {dimensions: [Product Category], measures: [Total Sales, Return Rate]}
having:
  - negated: true
    filters:
      - logic: and
        filters:
          - {field: Total Sales, op: ">", value: 0}
          - {negated: true, filters: [{field: Return Rate, op: ">", value: 0.1}]}
```

## API

Session-scoped under `/v1/sessions/{sid}/models/{mid}/`, each with a top-level shortcut:

| Endpoint | Returns |
|----------|---------|
| `GET rules` | every rule with level, findings, grain, what it reads, dependencies, whether it compiles (and why not), plus statistics by type, level, severity, executable |
| `GET rules/{name}` | one rule with its authored condition and the query behind it |
| `POST rules/{name}/compile` | the SQL whose rows are the rule's findings (`{"dialect": ...}` optional; defaults like `query/sql`) |
| `POST rules/compile` | every rule's SQL or the reason it failed, never hiding a failure |

See the [endpoint reference](../api/endpoints.md#business-rules).

## In the graph

Each rule is an `obsl:Rule` in the [OBSL RDF graph](obsl.md): its type, severity, derived level, grain dimensions, serialized condition, what it reads (`obsl:ruleReads`) and the rules it depends on (`obsl:dependsOnRule`). Definitions only; evaluation results are never part of the model graph. Through OSI, rules ride whole in the ORIONBELT vendor extension.

## What is next

Evaluation endpoints (run a rule or all rules against the warehouse and return findings as a report), rule references in query filters, and the Business Rules tab in the Gradio UI.

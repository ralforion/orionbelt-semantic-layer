---
description: "FinOps cloud cost analysis on the FOCUS specification: a multi-fact OBML model over charges, invoices and commitments, with invoice reconciliation through the composite fact layer."
---

# FinOps on FOCUS

[FOCUS](https://focus.finops.org) — the FinOps Open Cost and Usage Specification — is the FinOps Foundation's vendor-neutral schema for technology cost and usage data. AWS, Microsoft Azure, Google Cloud and OCI all publish FOCUS exports, so one model can read any of their bills.

FOCUS standardises the **physical** schema: column names, data types, allowed values, and requirement levels (MUST / SHOULD / MAY). It deliberately stops there. It does not define:

- metrics — no effective savings rate, no unit cost, no amortisation rule
- dimensional hierarchies — no provider → service → SKU
- how its datasets join

That is the semantic layer's job, and it is what this example supplies.

## Why this is a multi-fact model

Since version 1.4 FOCUS is a multi-dataset specification. This example reproduces the shape that matters:

```
                Billing Periods        Providers
                   ^      ^             ^     ^
                   |      |             |     |
              Charges     +--- Invoice Details
                 |
            Commitments
```

**Charges** (daily usage) and **Invoice Details** (invoice lines) are independent facts. Nothing joins one to the other — they meet only at the conformed dimensions. So asking for cost and invoiced amount side by side is a genuine multi-fact query, and OrionBelt answers it through the [composite fact layer](../guide/compilation.md) rather than by joining the two.

That question has a name in FinOps: **invoice reconciliation**, the headline use case for FOCUS 1.4. "Does what we were charged match what we were invoiced?"

## Build the data

The example ships a generator rather than a checked-in database file:

```bash
uv run python scripts/build_finops_duckdb.py
```

This writes `examples/finops.duckdb` with five tables in a `focus` schema — `charges`, `invoice_details`, `commitments`, `providers`, `billing_periods` — holding six months of synthetic multi-cloud billing that conforms to FOCUS column names and allowed values. The database is named for the domain, the schema for the specification its tables conform to.

```bash
export DUCKDB_DATABASE=$PWD/examples/finops.duckdb
export DB_VENDOR=duckdb
```

## Invoice reconciliation

```yaml title="reconciliation.yml"
select:
  dimensions: [Billing Period, Provider]
  measures: [Billed Cost, Invoiced Amount, Invoice Variance]
orderBy:
  - field: Billing Period
    direction: asc
```

```bash
uv run obsl execute examples/finops.obml.yml -q reconciliation.yml
```

| Billing Period | Provider | Billed Cost | Invoiced Amount | Invoice Variance |
|---|---|---|---|---|
| March 2026 | Amazon Web Services | 34,875.23 | 34,908.06 | -32.83 |
| March 2026 | Google Cloud | 24,395.02 | 24,400.78 | -5.76 |
| March 2026 | Microsoft Azure | 30,173.82 | 30,139.68 | 34.14 |

`Invoice Variance` is a metric spanning two measures that live on different facts:

```yaml
Invoice Variance:
  expression: "{[Billed Cost]} - {[Invoiced Amount]}"
```

The compiled SQL unions the two legs and pads the missing columns, so each fact is aggregated at its own grain:

```sql
WITH "composite_01" AS (
  SELECT
    "Billing Periods"."BillingPeriodLabel" AS "Billing Period",
    "Providers"."ProviderName" AS "Provider",
    CAST("Charges"."BilledCost" AS DECIMAL(18, 2)) AS "Billed Cost"
  FROM "focus"."charges" AS "Charges"
  LEFT JOIN "focus"."billing_periods" AS "Billing Periods" ON ...
  LEFT JOIN "focus"."providers" AS "Providers" ON ...
  UNION ALL BY NAME
  SELECT
    "Billing Periods"."BillingPeriodLabel" AS "Billing Period",
    "Providers"."ProviderName" AS "Provider",
    CAST("Invoice Details"."InvoicedAmount" AS DECIMAL(18, 2)) AS "Invoiced Amount"
  FROM "focus"."invoice_details" AS "Invoice Details"
  LEFT JOIN ...
)
SELECT
  "Billing Period",
  "Provider",
  SUM("composite_01"."Billed Cost")     AS "Billed Cost",
  SUM("composite_01"."Invoiced Amount") AS "Invoiced Amount",
  SUM("composite_01"."Billed Cost") - SUM("composite_01"."Invoiced Amount")
    AS "Invoice Variance"
FROM "composite_01"
GROUP BY ALL
```

### What the naive join does instead

Joining both facts to the billing period in one query is the obvious move, and it is wrong. Against this dataset:

| Approach | Billed Cost | Invoiced Amount |
|---|---|---|
| Truth | 518,167.94 | 517,944.37 |
| Composite fact layer | 518,167.94 | 517,944.37 |
| `charges JOIN invoice_details` | 1,730,132.52 | 435,865,817.48 |

Every one of the 14,979 charge rows is multiplied by every invoice line for its provider and period. Invoiced amount inflates roughly **842x**. Nothing errors; the dashboard just quietly reports a number that is three orders of magnitude wrong.

## Conformed dimensions matter

`Provider` is a separate data object rather than a column on each fact, and that is not a stylistic choice. A dimension used in a cross-fact query has to be reachable from **both** facts. Left as a plain column on `Charges`, the `Provider` value would populate on the charges leg and come back `NULL` on the invoice leg, splitting every reconciliation row in two.

The same applies to `Billing Periods`. These two objects are the entire reason a common root exists for the union.

## Rate optimisation

FOCUS separates three costs, and the gaps between them are where FinOps savings live:

| Measure | Meaning |
|---|---|
| `List Cost` | What the usage would cost at published on-demand rates |
| `Contracted Cost` | Cost at negotiated rates |
| `Effective Cost` | Cost with commitment purchases amortised over the period they cover |

```yaml
select:
  dimensions: [Provider, Service Category]
  measures: [Effective Cost, List Cost, Effective Savings Rate, Negotiated Discount Rate]
orderBy: [{field: Effective Cost, direction: desc}]
```

| Provider | Service Category | Effective Cost | List Cost | Effective Savings Rate | Negotiated Discount Rate |
|---|---|---|---|---|---|
| Amazon Web Services | Compute | 62,043.78 | 76,623.62 | 19.03% | 5.88% |
| Microsoft Azure | Compute | 62,038.48 | 74,547.62 | 16.78% | 6.19% |
| Microsoft Azure | Databases | 61,375.00 | 74,522.11 | 17.64% | 6.09% |

Splitting the two rates answers a question a single "savings" number cannot: how much came from negotiating, and how much from committing.

## Commitment utilisation

```yaml
select:
  dimensions: [Commitment, Commitment Type]
  measures: [Effective Cost, Committed Amount, Commitment Utilization]
```

| Commitment | Commitment Type | Effective Cost | Committed Amount | Commitment Utilization |
|---|---|---|---|---|
| Amazon Web Services Reserved Instance 2 | Reserved Instance | 17,389.32 | 21,650.10 | 80.32% |
| Amazon Web Services Savings Plan 1 | Savings Plan | 30,341.10 | 46,993.56 | 64.56% |
| Microsoft Azure Savings Plan 4 | Savings Plan | 25,541.75 | 28,336.98 | 90.14% |

This query emits a warning:

```
warning: Measure(s) 'Committed Amount' are sourced from an object whose rows
this query's joins replicate. Each was aggregated over rows deduplicated on
that object's key, so per-group values are correct - but one row can belong to
several groups, so the values do not add up to that object's grand total.
```

That is the intended behaviour, not a defect. `Committed Amount` lives on the *one* side of a many-to-one join: one commitment covers thousands of charge rows. Summing it naively across the joined result multiplies the contract value by the number of charges it covered. OrionBelt [deduplicates on the commitment key](../guide/compilation.md) so each group is right, and tells you the column will not cross-foot.

## Trend and anomaly detection

Cumulative and period-over-period metrics come from the same model:

```yaml
select:
  dimensions: [Charge Date]
  measures: [Effective Cost, Running Effective Cost, Effective Cost MoM Growth]
```

| Charge Date | Effective Cost | Running Effective Cost | Effective Cost MoM Growth |
|---|---|---|---|
| 2026-03-01 | 79,379.49 | 79,379.49 | |
| 2026-04-01 | 70,085.54 | 149,465.03 | -11.71% |
| 2026-05-01 | 70,058.03 | 219,523.06 | -0.04% |
| 2026-06-01 | 89,843.52 | 309,366.57 | 28.24% |
| 2026-07-01 | 75,150.27 | 384,516.85 | -16.35% |

`Effective Cost MoM Growth` is the metric a cost-anomaly alert fires on.

## Using a real cloud export

The model targets standard FOCUS columns only, so it ports to a real export by changing `code` and `schema` on the data objects and the `defaultDialect` setting.

One caveat for Google Cloud. Its FOCUS export puts every standard FOCUS column in a scalar type, but carries labels, credits, tags and project ancestry as `REPEATED RECORD` extension columns prefixed `x_`:

| Column | Type |
|---|---|
| `x_Credits`, `x_Labels`, `x_Tags`, `x_SystemLabels`, `x_ProjectLabels` | `REPEATED RECORD` |
| `x_Project` | `RECORD` (with a repeated `Ancestors`) |

OBML models relational columns, so reach those through a flattening BigQuery view and point `code` at the view. Note that `x_Credits` being repeated means credits are a *second fact at a different grain* inside the same table, not a labels convenience — model it as its own data object.

## Files

| File | Purpose |
|---|---|
| `examples/finops.obml.yml` | The semantic model |
| `scripts/build_finops_duckdb.py` | Generates `examples/finops.duckdb` |

## See also

- [FOCUS specification](https://focus.finops.org/focus-specification/)
- [Multi-fact queries and the composite fact layer](../guide/compilation.md)
- [TPC-DS Benchmark](tpcds.md) — the other multi-fact example

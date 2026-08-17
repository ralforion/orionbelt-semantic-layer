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

The example ships a generator rather than a checked-in database:

```bash
uv run python scripts/build_finops_duckdb.py
```

It writes the **raw export first**, one JSON object per line under `examples/finops_data/`, and only then loads DuckDB from those files. That ordering is deliberate: a FOCUS export is a file you receive, not a table someone hands you, and the files stay on disk so they can be opened, grepped and diffed before anything is modelled.

```json
{
  "ChargePeriodStart": "2026-03-01 00:00:00",
  "ProviderName": "Amazon Web Services",
  "ServiceName": "Amazon EC2",
  "SubAccountName": "data-analytics",
  "ListCost": 38.19217,
  "ContractedCost": 34.510305,
  "BilledCost": 34.510305,
  "EffectiveCost": 28.317563,
  "Tags": { "team": "data", "env": "prod", "cost_center": "cc-1310" }
}
```

`Tags` is a **nested object** in the file, as a real export carries it. The load turns it back into JSON *text*, which is what the model's `json_value` calls read; keeping it a DuckDB `STRUCT` would need nested-column support that does not exist yet.

`charges.jsonl` is about 20 MB and is gitignored, so a three-record excerpt is committed at `examples/finops_charges_sample.json` for anyone who has not run the generator. The result is five tables in a `focus` schema - `charges`, `invoice_details`, `commitments`, `providers`, `billing_periods` - holding six months of synthetic multi-cloud billing that conforms to FOCUS column names and allowed values, with a slice of rows deliberately left untagged. The database is named for the domain, the schema for the specification its tables conform to.

```bash
export DUCKDB_DATABASE=$PWD/examples/finops.duckdb
export DB_VENDOR=duckdb
```

For a runnable walkthrough of everything below, see the notebook at `examples/finops.ipynb`.

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
| March 2026 | Amazon Web Services | 22,802.97 | 22,753.66 | 49.31 |
| March 2026 | Google Cloud | 22,344.64 | 22,365.62 | -20.98 |
| March 2026 | Microsoft Azure | 21,055.83 | 21,032.69 | 23.14 |

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
| Truth | 510,642.55 | 510,606.74 |
| Composite fact layer | 510,642.55 | 510,606.74 |
| `charges JOIN invoice_details` | 1,647,888.36 | 438,798,976.70 |

Every one of the 15,224 charge rows is multiplied by every invoice line for its provider and period. Invoiced amount inflates roughly **859x**. Nothing errors; the dashboard just quietly reports a number that is three orders of magnitude wrong.

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
| Google Cloud | Compute | 71,254.14 | 87,116.86 | 18.21% | 5.99% |
| Microsoft Azure | Compute | 66,193.68 | 83,229.55 | 20.47% | 5.71% |
| Amazon Web Services | Compute | 57,978.35 | 71,802.66 | 19.25% | 6.05% |

Splitting the two rates answers a question a single "savings" number cannot: how much came from negotiating, and how much from committing.

## Commitment utilisation

```yaml
select:
  dimensions: [Commitment, Commitment Type]
  measures: [Effective Cost, Committed Amount, Commitment Utilization]
```

| Commitment | Commitment Type | Effective Cost | Committed Amount | Commitment Utilization |
|---|---|---|---|---|
| Amazon Web Services Savings Plan 1 | Savings Plan | 28,366.73 | 30,866.90 | 91.90% |
| Amazon Web Services Reserved Instance 2 | Reserved Instance | 25,998.99 | 29,497.48 | 88.14% |
| Microsoft Azure Reservation 3 | Reservation | 36,995.92 | 52,073.08 | 71.05% |

This query emits a warning:

```
warning: Measure(s) 'Committed Amount' are sourced from an object whose rows
this query's joins replicate. Each was aggregated over rows deduplicated on
that object's key, so per-group values are correct - but one row can belong to
several groups, so the values do not add up to that object's grand total.
```

That is the intended behaviour, not a defect. `Committed Amount` lives on the *one* side of a many-to-one join: one commitment covers thousands of charge rows. Summing it naively across the joined result multiplies the contract value by the number of charges it covered. OrionBelt [deduplicates on the commitment key](../guide/compilation.md) so each group is right, and tells you the column will not cross-foot.

## Cost allocation by tag

This is the first question anyone asks of a billing model: what does each team spend? FOCUS answers it with a standard `Tags` column, and real exports carry it as semi-structured data - JSON on Azure, a `MAP` on Databricks, an `ARRAY<STRUCT>` on Google Cloud.

The model reads it with `json_value`, a portable catalog function, in a computed column:

```yaml
columns:
  Tags:
    code: Tags
    abstractType: json
  Team Tag:
    expression: "json_value({Tags}, '$.team')"
    abstractType: string

dimensions:
  Team:
    dataObject: Charges
    column: Team Tag
    resultType: string
```

```yaml title="by-team.yml"
select:
  dimensions: [Team]
  measures: [Effective Cost, Pct of Total Spend]
orderBy: [{field: Effective Cost, direction: desc}]
```

| Team | Effective Cost | Pct of Total Spend |
|---|---|---|
| platform | 144,350.67 | 31.53% |
| ml | 84,650.11 | 18.49% |
| shared | 83,734.58 | 18.29% |
| *(none)* | **73,076.28** | **15.96%** |
| data | 72,020.24 | 15.73% |

The fourth row is the point. **16% of spend carries no tags at all** and cannot be charged to anyone. Untagged spend is the number a FinOps practitioner actually chases, which is why the generator leaves a slice of rows untagged rather than tagging everything.

### The same model, seven dialects

The expression is written once. Each dialect renders it into its own JSON access, and these are emphatically not spelling variants (identifier quoting elided for width):

| Dialect | Generated |
|---|---|
| BigQuery | `JSON_VALUE(Tags, '$.team')` |
| ClickHouse | `nullIf(JSON_VALUE(Tags, '$.team'), '')` |
| Databricks | `try_variant_get(parse_json(Tags), '$.team', 'string')` |
| DuckDB | `CASE WHEN json_type(Tags, '$.team') IN ('OBJECT','ARRAY') THEN NULL ELSE json_extract_string(Tags, '$.team') END` |
| Postgres | `CASE WHEN json_typeof(json_extract_path(Tags::json, 'team')) IN ('object','array') THEN NULL ELSE json_extract_path_text(Tags::json, 'team') END` |
| Snowflake | `CASE WHEN TYPEOF(GET_PATH(PARSE_JSON(Tags), 'team')) IN ('OBJECT','ARRAY') THEN NULL ELSE JSON_EXTRACT_PATH_TEXT(Tags, 'team') END` |
| Dremio | *unsupported* |

Four things differ, and each was measured rather than assumed:

- **Postgres** takes the path segments as *separate arguments*; **Snowflake** takes them dotted without the `$`, and bracketed for array subscripts — it rejects `arr.0` outright.
- **ClickHouse** returns the *empty string* rather than NULL for an absent path, so it is wrapped in `nullIf`.
- **DuckDB, Postgres, Snowflake and MySQL** return the *serialized JSON* for a path landing on an object or array, so each carries a type guard to honour the catalog's NULL rule. That is where the `CASE` comes from.
- **Databricks** is the only engine that gets the contract for free: `try_variant_get(…, 'string')` answers NULL when the value will not cast, which is the object/array rule and the absent-path rule at once.

That spread is why the path must be a literal, and why `json_value` earns its place in the catalog rather than being hand-written per model.

Dremio is the exception: no JSONPath scalar function, so it reports the call unsupported at compile time rather than mis-rendering it. A model that allocates by tag is pinned to the other seven engines, and says so.

The model sets `expressionMode: portable`, so an uncatalogued function would be an error rather than a silent engine dependency. It validates, which is the assertion that every expression here is portable.

## Trend and anomaly detection

Cumulative and period-over-period metrics come from the same model:

```yaml
select:
  dimensions: [Charge Date]
  measures: [Effective Cost, Running Effective Cost, Effective Cost MoM Growth]
```

| Charge Date | Effective Cost | Running Effective Cost | Effective Cost MoM Growth |
|---|---|---|---|
| 2026-03-01 | 60,932.72 | 60,932.72 | |
| 2026-04-01 | 67,420.74 | 128,353.46 | 10.65% |
| 2026-05-01 | 81,289.65 | 209,643.11 | 20.57% |
| 2026-06-01 | 80,015.45 | 289,658.57 | -1.57% |
| 2026-07-01 | 67,038.03 | 356,696.60 | -16.22% |

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
| `examples/finops.ipynb` | Runnable end-to-end notebook |
| `examples/finops_charges_sample.json` | Three raw export records, committed |
| `scripts/build_finops_duckdb.py` | Generates `examples/finops.duckdb` |

## See also

- [FOCUS specification](https://focus.finops.org/focus-specification/)
- [Multi-fact queries and the composite fact layer](../guide/compilation.md)
- [TPC-DS Benchmark](tpcds.md) — the other multi-fact example

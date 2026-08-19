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

`Tags` is a **nested object** in the file, as a real export carries it. The load turns it back into JSON *text*, which is what the model's `json_value` calls read. The four repeated columns beside it - `Labels`, `Credits`, `ResourceTags` and `Project` - are loaded as real DuckDB `STRUCT` arrays instead, and read as [nested data objects](#repeated-columns-labels-credits-and-folders). The demo carries both shapes on purpose.

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

### The same model, eight dialects

The expression is written once. All eight dialects render it into their own JSON access, and these are emphatically not spelling variants (identifier quoting elided for width):

| Dialect | Generated |
|---|---|
| BigQuery | `JSON_VALUE(Tags, '$.team')` |
| ClickHouse | `nullIf(JSON_VALUE(Tags, '$.team'), '')` |
| Databricks | `CASE WHEN schema_of_variant(try_variant_get(parse_json(Tags), '$.team')) LIKE 'OBJECT%' OR schema_of_variant(try_variant_get(parse_json(Tags), '$.team')) LIKE 'ARRAY%' THEN NULL ELSE try_variant_get(parse_json(Tags), '$.team', 'string') END` |
| DuckDB | `CASE WHEN json_type(Tags, '$.team') IN ('OBJECT','ARRAY') THEN NULL ELSE json_extract_string(Tags, '$.team') END` |
| MySQL | `CASE WHEN JSON_TYPE(JSON_EXTRACT(Tags, '$.team')) IN ('OBJECT','ARRAY') THEN NULL ELSE JSON_UNQUOTE(JSON_EXTRACT(Tags, '$.team')) END` |
| Postgres | `CASE WHEN json_typeof(json_extract_path(Tags::json, 'team')) IN ('object','array') THEN NULL ELSE json_extract_path_text(Tags::json, 'team') END` |
| Snowflake | `CASE WHEN TYPEOF(GET_PATH(PARSE_JSON(Tags), 'team')) IN ('OBJECT','ARRAY') THEN NULL ELSE JSON_EXTRACT_PATH_TEXT(Tags, 'team') END` |
| Dremio | `(TRY_CONVERT_FROM(Tags AS ROW("team" VARCHAR))."team")` |

Four things differ, and each was measured rather than assumed:

- **Postgres** takes the path segments as *separate arguments*; **Snowflake** takes them dotted without the `$`, and bracketed for array subscripts — it rejects `arr.0` outright.
- **ClickHouse** returns the *empty string* rather than NULL for an absent path, so it is wrapped in `nullIf`.
- **DuckDB, Postgres, Snowflake, MySQL and Databricks** return the *serialized JSON* for a path landing on an object or array, so each carries a type guard to honour the catalog's NULL rule. That is where the `CASE` comes from. Databricks was believed not to need one - `try_variant_get(…, 'string')` was expected to decline a non-scalar - and was measured returning the JSON like the rest, once its warehouse became reachable again.
- **Dremio** alone gets the contract from a cast that declines rather than fails, `TRY_CONVERT_FROM(x AS ROW(… VARCHAR))`, whose innermost `VARCHAR` will not accept an object or an array.
- **Dremio** is also the only one that puts the path in *identifier* position rather than inside a string literal, so its member names are quoted and its row type is built from the literal path at compile time.

That spread is why the path must be a literal, and why `json_value` earns its place in the catalog rather than being hand-written per model.

All eight engines answer it, so a model that allocates by tag is not pinned to any subset. That took measuring rather than reading: Dremio's obvious route, `CONVERT_FROM` with field access, raises "Unable to find the referenced field" for a tag a charge does not carry, which is the common case here rather than an edge one.

The model sets `expressionMode: portable`, so an uncatalogued function would be an error rather than a silent engine dependency. It validates, which is the assertion that every expression here is portable.

## Repeated columns: labels, credits and folders

The section above reads tags out of a JSON string, which is what the FOCUS spec
defines. Google Cloud does not do that. It leaves `Tags` empty and puts the data
in **repeated records** - `ARRAY<STRUCT>` columns - and AWS CUR via Athena does
the same. The demo dataset carries both shapes side by side.

A repeated column is a table: one small table per charge. So it is declared as a
data object whose rows come from unnesting it, rather than reached with an
accessor:

```yaml
Charge Labels:
  nestedIn: {dataObject: Charges, column: Labels}
  columns:
    Label Key:   {code: Key,   abstractType: string}
    Label Value: {code: Value, abstractType: string}
```

Nothing above that changes: dimensions, measures and queries treat it like any
other data object. See [Nested data objects](../guide/model-format.md#nested-data-objects-nestedin)
for the full rules.

### The keys are data, not columns

This is what a flattened model cannot offer. `Label Key` is a dimension whose
*values* are the label keys, so the coverage question is an ordinary group-by -
and it answers for keys nobody declared in advance:

```yaml
select:
  dimensions: [Label Key]
  measures: [Billed Cost]
```

| Label Key | Billed Cost |
|---|---|
| team | 436,883.52 |
| env | 436,883.52 |
| cost_center | 370,756.96 |
| owner | 239,731.56 |
| service-tier | 191,473.47 |
| component | 175,488.09 |

Against 512,917.22 of total spend, that is a governance answer: every resource is
supposed to carry a cost centre, and 72% of spend does. A model with one declared
column per tag key cannot ask this, because the keys it does not know about are
the ones worth finding.

### Where unnesting goes wrong

An unnest multiplies the row that *contains* the array. A charge carrying two
labels appears twice, so summing the **charge's** cost under a label dimension
counts it twice. In this dataset `component` and `app` often agree, so the same
value really does appear twice on one charge - 2,575 of them.

OBSL deduplicates on the charge's `primaryKey` before aggregating. Measured
against ground truth computed directly in SQL:

| | Spend tagged `worker` |
|---|---|
| Truth | **46,343.91** |
| Naive unnest, no deduplication | 70,022.28 |
| OBSL | **46,343.91** |

The naive figure overstates by 51%. It is also the figure you get from a
hand-written flattening view, which is why the deduplication is the point rather
than the unnest.

That deduplication needs a key, so `Charges` declares `ChargeKey` as its
`primaryKey`. FOCUS defines no row identifier and no combination of its columns
is unique here, so the demo generator mints one. Without it, OBSL refuses the
query rather than answering 70,022.28.

Per-group values are exact; the groups overlap, because a charge carrying two
different labels belongs to both. The result carries a `FAN_TRAP_RISK` warning
saying so.

### A measure on the array itself

`Credits` carries an amount, so it is a second fact at its own grain - four
fields (`Id`, `Type`, `Name`, `Amount`) and none of them a key, which is exactly
what a key/value accessor cannot read. Google's own `x_Credits` carries five,
adding `FullName`. Each credit line counts once, including two identical lines
on one charge:

```yaml
select:
  dimensions: [Credit Type]
  measures: [Credit Amount, Credit Line Count, Billed Cost]
```

| Credit Type | Credit Amount | Credit Line Count | Billed Cost |
|---|---|---|---|
| COMMITTED_USAGE_DISCOUNT | -51,586.84 | 6,423 | 209,339.28 |
| SUSTAINED_USAGE_DISCOUNT | -7,227.07 | 2,780 | 91,346.11 |
| PROMOTION | -3,754.04 | 927 | 30,315.76 |
| FREE_TIER | -1,203.74 | 1,398 | 48,065.14 |

Two grains in one result: `Credit Amount` over the credit lines, `Billed Cost`
over the charges that carry them. They are computed over different row sets and
joined at the query grain, because no single flat query produces both correctly -
the naive one inflates the gross by counting a charge once per credit it has.

### An array inside a struct

`Project.Ancestors` is an array nested in a record. The dotted path reaches it
with no further declaration:

```yaml
Project Ancestors:
  nestedIn: {dataObject: Charges, column: Project.Ancestors}
```

| Org Folder | Billed Cost |
|---|---|
| Contoso | 492,149.20 |
| Engineering | 305,949.12 |
| Platform | 191,960.02 |
| Data | 113,989.10 |
| Corporate | 97,888.17 |
| Research | 88,311.92 |

Contoso is the root, so it carries everything with a project; the 20,768.02
difference from total spend is the tax lines, which have no project at all.

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

Declare each of those as a nested data object and OBSL unnests it directly - no flattening view, and the dotted path handles `x_Project.Ancestors`:

```yaml
Charge Labels:
  nestedIn: {dataObject: Charges, column: x_Labels}
  columns:
    Label Key:   {code: Key,   abstractType: string}
    Label Value: {code: Value, abstractType: string}
```

`x_Credits` being repeated means credits are a *second fact at a different grain* inside the same table rather than a labels convenience, and modelling it as its own object is what puts it at that grain. On Dremio, which has no FROM-clause unnest, declare `code` alongside `nestedIn` to read a flattening view there and OBSL will say which source it used.

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

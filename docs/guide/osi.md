---
description: "OSI (Open Semantic Interchange) is an open standard for portable semantic models, and OrionBelt reads and writes it, so metric and dimension definitions move between tools."
---

# OSI Interoperability

**OSI (Open Semantic Interchange)** is an open standard for portable semantic models, founded with the goal of letting metric and dimension definitions move between BI tools, semantic layers, and data platforms without rewriting. See [open-semantic-interchange.org](https://open-semantic-interchange.org/) for the specification and contributor list.

!!! note "Now at the Apache Incubator"
    OSI has been contributed to the Apache Software Foundation and is now developed as **[Apache Ossie (incubating)](https://github.com/apache/ossie)**. The specification and its converters (including the OrionBelt converter) live there going forward. Existing `github.com/open-semantic-interchange/OSI` links redirect to the new repository.

OrionBelt includes a bidirectional converter between OBML and the [OSI specification](https://github.com/apache/ossie) format. The converter handles structural differences between the two formats — including metric decomposition, relationship restructuring, and lossless `ai_context` preservation via `customExtensions` — with built-in validation for both directions.

The converter ships as a standalone `osi-orionbelt` package. It is an **optional** dependency: install it with `pip install 'orionbelt-semantic-layer[osi]'` (or standalone via `pip install osi-orionbelt`). The published API Docker images bundle it, so the `/convert`, `/models/from-osi`, and `/osi` endpoints work out of the box; on a bare install without the extra, those endpoints return a `503`.

## Spec version

OBSL writes **Apache Ossie 0.3.0.dev0**, the version upstream `core-spec/` develops, and reads 0.3.0.dev0, 0.2.0.dev0 and 0.1.x documents. The vendored schema lives at `packages/osi-orionbelt/src/osi_orionbelt/schemas/osi-schema.json` (a copy of upstream `core-spec/ossie-schema.json`), beside `osi-schema-0.2.0.dev0.json`, which checks a document that declares 0.2.0.dev0. A weekly CI job compares the copy with upstream and opens an issue when they differ.

What 0.3.0.dev0 changed, and how OBSL reads and writes it:

| Ossie 0.3.0.dev0 | Import | Export |
|---|---|---|
| `expression: {sql: ...}`, the portable `OSSIE_SQL_2026` form, authoritative over an `OSSIE_SQL_2026` entry in `dialects` | Read as that form | `dialects` with `ANSI_SQL`, plus `OSSIE_SQL_2026` where the expression is in that language (fields too) |
| Metrics declared on a dataset, naming its fields unqualified and its source columns as `source.column` | A measure like any other, its references qualified: `SUM(amount)` on `orders` is `SUM(orders.amount)`, `COUNT(*)` counts the dataset's primary key. A name another metric takes becomes `"orders total_revenue"`, with a warning. A `COUNT(*)` on a dataset without a single-column primary key is not imported; query the data object's own count, `"orders Count"` | Model-level metrics |
| `AGG(dataset.metric)` in a model-level metric | Reads the measure the dataset metric became | Not written |
| `nonportable_extensions`, data a JSON object (was `custom_extensions`, data a JSON string) | Read; `custom_extensions` still read | Written; data that is not a JSON object is wrapped as `{"value": ...}` with a warning |

A field computed by an expression (`amount_cents / 100.0`, a `CASE`) becomes an OBML computed column, `{amount_cents} / 100.0`, since OBML's `code` is a column name. Each column the expression reads is a sibling whose `code` is that column: a field that computes exactly that column, or else a column the import adds for it (with a warning; the export leaves it out unless the model uses it). A SQL `CAST(x AS DOUBLE)` becomes `cast(x, 'double')`. An expression OBML's grammar does not read (`||`, `EXTRACT(... FROM ...)`, a subquery) is kept as the column's `code` with a `LOSSY` warning, and a query reading that column fails. The export writes the field's SQL back as written, or, after the computed column was edited, the computed column with each sibling written as its column.

A relationship from a dataset to itself (an employee's manager) has no OBML join, since OBML refuses a join cycle. It is left out with a `LOSSY` warning and written back on export. To query it, add a second data object over the same table and join to that.

**Breaking change vs. OBSL v2.5** — the previous release emitted OSI v0.1.1. Downstream consumers pinning to v0.1 will reject v2.6 output. The converter still **reads** v0.1.x inputs via the legacy shim `_normalize_legacy_v01()`, which promotes pre-v0.2 `custom_extensions` payloads (`obml_primary_key`, `obml_unique_keys`) into the v0.2 first-class fields before parsing.

What's new in v0.2 that OBSL now round-trips:

| Surface | OBML side | OSI side |
|---|---|---|
| `primary_key` | Per-column `primaryKey: true` flag | First-class `primary_key: [col, ...]` array (composite supported, declaration order preserved) |
| `unique_keys` | OBSL custom extension `obml_unique_keys: [[col], [col1, col2], ...]` | First-class `unique_keys: [[...], ...]` array |
| Field `label` | OBSL custom extension `obml_field_label` | First-class `field.label` string |
| `MAQL` dialect | n/a (we don't generate MAQL) | Accepted on read, surfaced via warning if it's the only available dialect |

## REST API

```bash
# Convert OSI -> OBML
curl -X POST http://127.0.0.1:8000/v1/convert/osi-to-obml \
  -H "Content-Type: application/json" \
  -d '{"input_yaml": "version: \"0.3.0.dev0\"\nname: my_model\ndatasets:\n  ..."}' | jq

# Convert OBML -> OSI
curl -X POST http://127.0.0.1:8000/v1/convert/obml-to-osi \
  -H "Content-Type: application/json" \
  -d '{"input_yaml": "version: 1.0\ndataObjects:\n  ..."}' | jq
```

Both endpoints are stateless — no session required.

The converter targets the OSI **core-spec** semantic model (datasets, fields, relationships, metrics) in both directions. OSI's separate ontology layer is out of scope.

## What other OSI tools read

Each exported measure or metric carries its expression in `ANSI_SQL` and, where it has a form in Apache Ossie's own expression language, also in `OSSIE_SQL_2026`. That form uses the language's functions where they differ from ANSI: `CONTAINS`, `STARTSWITH` and `ENDSWITH` instead of `LIKE ... ESCAPE`, native `ILIKE`, and a `CAST` for a timestamp literal. It is checked against `core-spec/expression_language.md` both as written (every function called is one the spec defines at the REQUIRED or RECOMMENDED level, every `CAST` type and typed literal is a listed one, no foreign operator such as `::`) and as parsed (no subquery or other construct the spec does not list); a measure using `MODE`, `ANY_VALUE` or `LISTAGG`, a pattern that needs `ESCAPE`, or an authored expression outside the subset keeps `ANSI_SQL` only.

Another OSI tool reads a metric's SQL expression, not OrionBelt's vendor extension, so the export writes expressions that compute what OrionBelt computes:

| OBML | Exported expression |
|---|---|
| Column | A field named by the column name, computed by the column `code`; `primary_key` and relationship columns list field names |
| Column reference | `<dataset>.<field>`: the OBML data object and column names, following the Ossie expression language's identifier rules, which are ANSI SQL's: a regular (bare) identifier is normalized to upper case, a quoted one is taken as written, so a quoted `"amount"` would not match a field declared `amount`, while `"AMOUNT"` would. A name is bare where it can be a regular identifier. A name that cannot (a space, a leading digit, a SQL:2023 reserved word such as `Order` or `value`) is double-quoted as written. A regular name DuckDB or sqlglot will not read bare, such as `Qualify`, is quoted in its normalized form, `"QUALIFY"`. Names that differ only by case, such as `Amount` beside `amount`, are one identifier to the language; they are quoted as written, which the import tells apart, and the export warns. The import resolves a reference by the same rules (a bare one regardless of case, a quoted one exactly, else by its normalized form) and keeps a metric whose reference matches no field, or more than one, verbatim |
| Measure `filters` | `SUM(CASE WHEN <condition> THEN <arg> END)`, the spec's portable filtered aggregation |
| Measure `total: true` | The grand-total window, e.g. `SUM(SUM(x)) OVER ()` |
| Measure `defaultValue` | `COALESCE(<aggregate>, <value>)` |
| Synthesized count (`"Orders Count"`) | `COUNT(<dataset>.<primary key>)` |
| Metric referencing measures or metrics | The referenced SQL, inlined |
| Cumulative and window metrics | The window over the aggregated measure, ordered by the time dimension exactly as the query groups it: truncated to its `timeGrain`, and cast back to a temporal `resultType` (`CAST(DATE_TRUNC('month', ...) AS DATE)`) |

Some OBML definitions depend on the query and have no faithful single expression: period-over-period metrics, reaggregate metrics (they need a second query layer), measures with `grain`, `filterContext` or `anchor`, and cumulative or window metrics over something that is already a window (a `total: true` measure, or another cumulative or window metric), since window calls cannot nest. The same goes for anything that references them. The export leaves these out of the OSI metrics, warns about each, and keeps them whole in the model-level `ORIONBELT` extension, so OBML → OSI → OBML still restores them.

On import, an exact percentile written as `PERCENTILE_CONT(p) WITHIN GROUP (ORDER BY x)` or `PERCENTILE_DISC(...)` (ANSI, Snowflake, Databricks), the two-argument `PERCENTILE_CONT(x, p)`, or Spark's exact `percentile(x, p)` becomes a measure with that aggregation and `percentile: p`, alone or inside a formula. A descending `percentile_cont` imports as the ascending one at `1 - p`, the same point counted from the other end, and a fraction cast to `DOUBLE`, to a decimal whose scale holds it, or to single-precision `FLOAT` / `REAL` when single precision holds it exactly (0.25, but not 0.1), is read through the cast. A descending `percentile_disc` (it steps at different places in each direction), a multi-key order, a fraction that is not a literal between 0 and 1 with at most 9 decimal places or is cast to a type that may round it (a bare `DECIMAL` has scale 0 on Snowflake and Databricks), a weighted `percentile(x, p, frequency)`, and `DISTINCT` have no OBML equivalent and are preserved verbatim like any other undecomposable metric.

An aggregate with `FILTER (WHERE p)` imports as the aggregate of `CASE WHEN p THEN x END`, the rewrite Apache Ossie's expression language gives for an aggregate that ignores NULLs (`COUNT(*) FILTER (WHERE p)` counts `CASE WHEN p THEN 1 END`). A row the predicate rejects is left out of the aggregate, never out of the query's groups: a group with no such row shows an empty sum and a count of 0. A FILTER over an aggregate not relied on to skip NULLs (`ANY_VALUE`, `MODE`, `LISTAGG`), with several arguments or under `OVER (...)` is preserved verbatim. So is any metric whose aggregated expression OBML's expression parser does not read, with or without FILTER, rather than a measure that would stop the model loading: an operator it lacks (`ILIKE`, `IS DISTINCT FROM`, `%`, `||`), SQL syntax inside a call (`EXTRACT(DAYOFWEEK FROM d)`, `TRIM(BOTH ' ' FROM s)`), a keyword such as `INTERVAL`, a clock read with an argument (`CURRENT_TIMESTAMP(3)`), or a call to an OBML catalog function with an argument count or unit it does not take. `EXTRACT(YEAR FROM d)` (and QUARTER, MONTH, DAY, HOUR, MINUTE, SECOND) imports as OBML's `extract('year', d)`, and `CURRENT_DATE`, `CURRENT_TIMESTAMP` and `CURRENT_TIME` as [`current_date()`, `current_timestamp()` and `current_time()`](functions.md): the session's clock, as running the source's SQL reads it, unless the model sets `queryTimezone`. WEEK, DAYOFWEEK and DAYOFYEAR are numbered differently from engine to engine, and the spec does not say which numbering it means, so those are preserved.

A model's and a metric's `ai_context` come back on export: synonyms become the OBML synonyms, and the rest (`instructions`, `examples`, other keys) is kept in an `OSI` custom extension on the OBML model, measure or metric, as it already was for datasets and fields. An `ai_context` given as plain text is read as its `instructions`.

An approximate percentile imports as the exact one it estimates, with an `APPROXIMATE` warning: Snowflake's `APPROX_PERCENTILE(x, p)`, a t-digest estimate that interpolates, as `percentile_cont`, and Databricks' `approx_percentile(x, p[, accuracy])` / `percentile_approx`, which returns a value of the column, as `percentile_disc`. The exact value is what the approximation estimates, so the measure gets more accurate rather than less; the cost is that values can differ from the source tool's in the last digits, and the exact aggregate can be slower on large tables. An `ANSI_SQL` expression has no approximate percentile, and a list of fractions or a weighted one has no single measure: those are preserved verbatim.

A data object with `nestedIn` and no `code` has no table: its rows are the elements of an array column on its parent. Its dataset `source` is therefore the unnest as a query, which the Ossie schema allows, selecting each column's `code` from the element: `SELECT "Charge Labels"."Key" AS "Key", ... FROM WH.PUBLIC.charges AS "Charges", UNNEST("Charges"."Labels") AS "Charge Labels"`. A struct path is reached as composite field access (`("Charges"."Project")."Ancestors"`), and a nested parent's subquery also selects the field its children unnest from. It runs as written on Postgres; DuckDB, Snowflake, MySQL and engines that quote with backticks spell the unnest differently. The import restores `nestedIn` from the ORIONBELT extension rather than from this text.

An Apache Ossie document holds one semantic model, its properties (`name`, `datasets`, `relationships`, `metrics`, ...) at the document root ([apache/ossie#383](https://github.com/apache/ossie/pull/383)). The export writes that shape. The import also reads the earlier `semantic_model` array, which osi-orionbelt wrote before 0.5.0, converting its first model with a warning; the input schema check reports the wrapper as an error, but the conversion still runs.

## Querying an Ossie model

Apache Ossie drafts two ways to query a model: SQL with `AGG()` (the [relational interface](https://github.com/apache/ossie/blob/main/core-spec/relational_semantics.md), "Layer 2") and a request naming metrics, dimensions and a filter (the [wide-table interface](https://github.com/apache/ossie/blob/main/core-spec/wide_table_semantics.md), "Layer 3"). OBSL implements neither yet. An imported Ossie model is queried like any OBML model, with dimensions, measures and `where`.

Every field of the model is a dimension, named as the field: `region`, `deptno`. Fields marked `dimension` are named first. A field whose name a field of another dataset already has is named with its dataset, `returns amount`, and so is a field named like a metric, since a dimension and a measure cannot share a name in OBML. The import lists each renamed field in its warnings. A field OBML cannot read (reported `LOSSY`) is a dimension only when marked.

Most answers agree with the Ossie compliance cases. Where Ossie refuses a query, or returns other rows, OBSL answers as follows:

| Query | Ossie | OBSL | If you want Ossie's answer |
|---|---|---|---|
| A filter reading two datasets: `orders.amount > 100 AND customers.region = 'EAST'` | Refused, `E_CROSS_DATASET_FILTER` | Applies both conditions | Nothing to do. An OBML filter may read any data object the query reaches |
| A metric grouped by a field its dataset cannot reach: customer count by order ID | Refused, `E_NO_PATH` | Counts each customer once per order, plus a row with an empty order ID for customers without orders. Warns `FAN_TRAP_RISK` | Group the metric by a field of its own dataset |
| Metrics whose datasets do not all reach a dimension: revenue and ticket count by region, where tickets join nothing | Refused, `E_PARTIAL_DIMENSION_REACHABILITY` | Every ticket is counted in the row where region is empty; the other regions show 0. Warns `DIMENSION_NOT_APPLIED` | Query ticket count without region, or join tickets to customers in the model |
| A filter one metric's dataset cannot reach: revenue and returns by region, where order amount > 100 | Refused, `E_UNSUPPORTED_FILTER_PATH` | Filters revenue; returns are not filtered. Warns `FILTER_NOT_APPLIED` | Query the two metrics separately |
| One aggregate over two datasets: `SUM(orders.amount - returns.amount)` | Refused, `E_AMBIGUOUS_MEASURE_GRAIN` | Sums each dataset per customer, the data object both join to, and evaluates per customer. A customer with orders and no returns counts their orders. Warns `CONFORMED_GRAIN_ASSUMED` | Set `anchor:` on the measure to choose the grain |
| Rows on the one side that no fact reaches: departments counted by employee job, and one department has no employees | Left out, unless the query sets `includeUnmatched` | Kept, in a row where job is empty | Filter that row out: `where: [{field: job, op: set}]` |
| `ROLLUP` (`grouping: rollup` in OBML) | The grouped rows and the total rows | The same rows, plus one 0/1 column per dimension that marks the total rows | Leave out the flag columns |

See [Cross-fact measure expressions](compilation.md#cross-fact-measure-expressions) for how a measure over two facts is evaluated.

## Gradio UI

The Gradio UI provides **Import OSI** / **Export to OSI** buttons that use these API endpoints, with validation feedback for both directions.

## Mapping Reference

See the [OSI - OBML Mapping Analysis](https://github.com/ralforion/orionbelt-semantic-layer/blob/main/packages/osi-orionbelt/osi_obml_mapping_analysis.md) for the core-spec mapping.

## External concept mappings

OSI has no slot for [`ontology.prefixes` or `externalConceptMappings`](concept-mappings.md), so the converter carries both inside the `ORIONBELT` vendor `custom_extensions` of the OSI entity each OBML artefact becomes: the semantic model (prefixes and model-level mappings), the dataset, the field (for a dimension, including the extra-dimension descriptors when several dimensions share one column) and the metric (for every exported measure and metric; a left-out one keeps its links in the model-level extension). The reverse direction restores them verbatim, so OBML → OSI → OBML is lossless and a mapping always comes back together with the prefix its compact IRI needs. Other OSI tools see the payload as opaque vendor data. Importing an OSI document that names concepts natively (`maps_to_concept` or Ossie ontology mappings) is not implemented yet.
